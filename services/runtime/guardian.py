"""Cross-platform stack guardian (replaces the 2-minute PowerShell watchdog).

The OS bootstrap (a Task Scheduler task on Windows, run under ``pythonw``)
starts this process outside any agent session; the monitoring logic itself is
plain Python + stdlib so it runs on any OS. Only the launcher command and the
tree-kill are platform-specific.

What it does, once per health interval (default 20 s):

* app / capability proxy: TCP + ``/healthz`` probe (localhost, ~1 ms);
* job worker: lock freshness plus process identity (pid + creation token, so
  a recycled pid is never mistaken for the worker and never killed);
* job queue (every ``--queue-interval``): PENDING work with free slots and an
  idle worker means a claim stall.

Repairs go through the platform launcher (``start_suprun.ps1 -Detached`` on
Windows). They are conservative: a worker with RUNNING jobs is never killed;
an app that is still booting (ui.lock pid alive) is left alone; repeated
repairs are rate-limited per piece.

``--once`` is the backstop mode used by a sparse scheduled task: it checks
the heartbeat and starts the guardian if it is gone.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JOB_STORE = ROOT / ".job_store"
LOG_PATH = ROOT / "guardian.log"
HEARTBEAT = JOB_STORE / "guardian.heartbeat"
QUEUE_STATE = JOB_STORE / "guardian_queue.state"

APP_PORT = 8501
PROXY_PORT = 8321
YOUNG_WORKER_SEC = 300.0
APP_REPAIR_BACKOFF_SEC = 120.0
PIECE_REPAIR_BACKOFF_SEC = 30.0
LOG_MAX_BYTES = 1_000_000

_CREATE_NO_WINDOW = 0x08000000
_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------- #
# State snapshots + the pure decision plan (unit-tested)
# --------------------------------------------------------------------------- #


@dataclass
class Snapshot:
    app_port_up: bool
    app_http_ok: bool
    app_process_alive: bool
    app_lock_age: float | None
    proxy_ok: bool
    worker_pid: int | None
    worker_alive: bool
    worker_token: float | None
    worker_age: float | None
    worker_lock_age: float | None
    pending: int
    running: int
    max_parallel: int


@dataclass
class GuardianState:
    app_http_failures: int = 0
    last_repair: dict[str, float] = field(default_factory=dict)
    queue: dict = field(default_factory=dict)


@dataclass
class Action:
    kind: str  # start_stack | restart_app | restart_proxy | start_worker | restart_worker | note
    detail: str = ""
    pid: int | None = None
    token: float | None = None


def plan(
    snap: Snapshot,
    state: GuardianState,
    now: float,
    *,
    queue_due: bool,
    lock_max_age: float,
    stall_seconds: float,
    backoff_seconds: float,
) -> list[Action]:
    """Decide repairs for one probe (pure: no I/O, no clock reads)."""
    actions: list[Action] = []

    def cooled(key: str, seconds: float) -> bool:
        return now - state.last_repair.get(key, 0.0) >= seconds

    # -- app ------------------------------------------------------------- #
    if not snap.app_port_up:
        if snap.app_process_alive and (
            snap.app_lock_age is None or snap.app_lock_age <= 60
        ):
            actions.append(Action("note", "app is booting (process alive, port not up yet)"))
        elif snap.app_process_alive:
            if cooled("app", APP_REPAIR_BACKOFF_SEC):
                state.last_repair["app"] = now
                actions.append(
                    Action("restart_app", f"app process wedged (lock {snap.app_lock_age:.0f}s) - restarting"),
                )
        elif cooled("stack", APP_REPAIR_BACKOFF_SEC):
            state.last_repair["stack"] = now
            actions.append(Action("start_stack", "app not running - starting the stack"))
        state.app_http_failures = 0
    elif not snap.app_http_ok:
        state.app_http_failures += 1
        failures = state.app_http_failures
        if failures >= 2 and cooled("app", APP_REPAIR_BACKOFF_SEC):
            state.last_repair["app"] = now
            state.app_http_failures = 0
            actions.append(
                Action("restart_app", f"/healthz failed {failures}x with the port up"),
            )
    else:
        state.app_http_failures = 0

    # -- capability proxy ------------------------------------------------ #
    if not snap.proxy_ok and cooled("proxy", PIECE_REPAIR_BACKOFF_SEC):
        state.last_repair["proxy"] = now
        actions.append(Action("restart_proxy", "proxy /healthz failed"))

    # -- job worker ------------------------------------------------------ #
    if not snap.worker_alive:
        if cooled("worker", PIECE_REPAIR_BACKOFF_SEC):
            state.last_repair["worker"] = now
            actions.append(Action("start_worker", "worker not running"))
    elif snap.worker_lock_age is not None and snap.worker_lock_age > lock_max_age:
        if snap.running > 0:
            actions.append(
                Action(
                    "note",
                    f"worker lock stale {snap.worker_lock_age:.0f}s with {snap.running} "
                    "RUNNING job(s) - not touching it",
                ),
            )
        elif snap.worker_age is not None and snap.worker_age < YOUNG_WORKER_SEC:
            actions.append(
                Action("note", f"worker still starting ({snap.worker_age:.0f}s old)"),
            )
        elif cooled("worker_restart", 60.0):
            state.last_repair["worker_restart"] = now
            actions.append(
                Action(
                    "restart_worker",
                    f"worker lock stale {snap.worker_lock_age:.0f}s and idle",
                    pid=snap.worker_pid,
                    token=snap.worker_token,
                ),
            )

    # -- queue claim liveness -------------------------------------------- #
    if queue_due and snap.worker_alive:
        q = state.queue
        if (
            q.get("pid") != snap.worker_pid
            or q.get("pending") != snap.pending
            or q.get("running") != snap.running
        ):
            q.update(since=now, pid=snap.worker_pid, pending=snap.pending, running=snap.running)
        if snap.pending > 0 and snap.running < snap.max_parallel:
            stalled = now - float(q.get("since", now))
            last_repair = float(q.get("last_repair", 0.0) or 0.0)
            if stalled >= stall_seconds:
                if snap.running > 0:
                    actions.append(
                        Action("note", "queue stalled with jobs in flight - never kills"),
                    )
                elif last_repair > 0 and now - last_repair < backoff_seconds:
                    actions.append(Action("note", "queue stall - repair backoff in effect"))
                else:
                    q["last_repair"] = now
                    actions.append(
                        Action(
                            "restart_worker",
                            "idle worker not claiming PENDING work",
                            pid=snap.worker_pid,
                            token=snap.worker_token,
                        ),
                    )
    return actions


# --------------------------------------------------------------------------- #
# Platform helpers
# --------------------------------------------------------------------------- #


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if _WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_token(pid: int | None) -> float | None:
    """A value that changes when a pid is recycled (creation identity)."""
    if not pid:
        return None
    if _WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        try:
            creation, _exit, _kernel, _user = (wintypes.FILETIME() for _ in range(4))
            if not kernel32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(_exit),
                ctypes.byref(_kernel), ctypes.byref(_user),
            ):
                return None
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return float(ticks)
        finally:
            kernel32.CloseHandle(handle)
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8").split()
        return float(fields[21])
    except (OSError, IndexError, ValueError):
        return None


def process_age(pid: int | None) -> float | None:
    if not pid or not pid_alive(pid):
        return None
    if _WINDOWS:
        from ctypes import wintypes

        # Creation FILETIME and current FILETIME share the same 100 ns epoch.
        creation = process_token(pid)
        if creation is None:
            return None
        now_ft = wintypes.FILETIME()
        ctypes.windll.kernel32.GetSystemTimeAsFileTime(ctypes.byref(now_ft))
        now_ticks = (now_ft.dwHighDateTime << 32) | now_ft.dwLowDateTime
        return max(0.0, (float(now_ticks) - creation) / 1e7)
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8").split()
        start_ticks = float(stat[21])
        hz = os.sysconf("SC_CLK_TCK")
        btime = 0.0
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                btime = float(line.split()[1])
                break
        return max(0.0, time.time() - (btime + start_ticks / hz))
    except (OSError, ValueError, IndexError):
        return None


def kill_tree(pid: int) -> None:
    """Retired: the guardian never signals a process it did not start.

    The worker is owned by the Task Scheduler, not by this process, so a kill
    here would violate the ownership rule (and did: it once took four healthy
    runs down). Repairs now write an intent file the worker honours itself;
    see :func:`request_worker_stop`.
    """
    log(f"kill_tree({pid}) refused: the guardian does not own that process")
    raise RuntimeError("guardian must not kill processes it did not spawn")


def request_worker_stop(reason: str) -> None:
    """Ask the worker daemon to exit on its own (it owns its own lifetime).

    The daemon's lock pulse checks this file every second and exits cleanly
    when it is idle; with RUNNING jobs it keeps working, which is exactly the
    conservative behaviour the old tree-kill could not express.
    """
    path = JOB_STORE / "worker.stop"
    try:
        JOB_STORE.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"ts": time.time(), "reason": reason}),
            encoding="utf-8",
        )
        log(f"worker stop requested: {reason}")
    except OSError as exc:
        log(f"could not write worker stop request: {exc}")


def wait_for_worker_exit(timeout: float = 20.0) -> bool:
    """Wait until no worker daemon process is alive; True when it is gone.

    The launcher decides "already running" from ``worker.lock`` freshness
    (15 s), not from the process. A worker that just honoured a stop request
    leaves a lock that is still fresh, so launching immediately would be
    skipped and the worker would stay down until the lock aged out. Waiting
    for the process to actually disappear — and then dropping the stale lock —
    lets the launcher start a fresh daemon right away.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not find_worker_pids():
            lock = JOB_STORE / "worker.lock"
            try:
                lock.unlink(missing_ok=True)
            except OSError as exc:
                log(f"could not clear the stale worker lock: {exc}")
            return True
        time.sleep(1.0)
    return False


def _powershell() -> str:
    """Absolute PowerShell path: task environments may not carry it on PATH."""
    if not _WINDOWS:
        return "powershell"
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate) if candidate.exists() else "powershell"


def find_worker_pids() -> list[int]:
    """Live worker daemon pids, matched by command line (never by lock pid).

    ``worker.lock`` carries the pid of the daemon that FIRST acquired it; a
    code-change re-exec keeps touching the file but runs under a new pid, so
    the stored pid is stale by design. This lookup runs only on a stale-lock
    probe or a restart decision, never on the healthy path.
    """
    if _WINDOWS:
        script = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -eq 'python.exe' -and "
            "$_.CommandLine -like '*-m services.jobs.worker_service*' } | "
            "ForEach-Object { $_.ProcessId }"
        )
        try:
            completed = subprocess.run(
                [_powershell(), "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=_CREATE_NO_WINDOW,
                check=False,
            )
            return [
                int(line.strip())
                for line in completed.stdout.splitlines()
                if line.strip().isdigit()
            ]
        except (OSError, subprocess.TimeoutExpired):
            return []
    pids: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return pids
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", "ignore",
            )
        except OSError:
            continue
        if "-m services.jobs.worker_service" in cmdline:
            pids.append(int(entry.name))
    return pids


def launcher_argv(flags: list[str]) -> list[str]:
    override = (os.environ.get("SOP_GUARDIAN_LAUNCHER") or "").strip()
    if override:
        return shlex.split(override) + flags
    if _WINDOWS:
        return [
            _powershell(),
            "-NoProfile",
            "-File",
            str(ROOT / "start_suprun.ps1"),
            "-Detached",
            *flags,
        ]
    return []


def run_launcher(flags: list[str]) -> None:
    argv = launcher_argv(flags)
    if not argv:
        log("no launcher configured on this OS (set SOP_GUARDIAN_LAUNCHER); repair skipped")
        return
    kwargs: dict = {"cwd": str(ROOT), "stdin": subprocess.DEVNULL}
    kwargs["stdout"] = subprocess.DEVNULL
    kwargs["stderr"] = subprocess.DEVNULL
    if _WINDOWS:
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    # Fire and forget: the launcher can wait minutes for imports, and the
    # next probes observe the result. Backoff prevents repair storms.
    subprocess.Popen(argv, **kwargs)
    log("launched " + " ".join(flags or ["(stack)"]))


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except Exception:  # noqa: BLE001 - any failure means "not healthy"
        return False


def _port_open(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _mtime_age(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def job_counts(limit: int = 40) -> tuple[int, int, int]:
    """(pending, running, max_parallel) from the newest job records only."""
    files = sorted(
        JOB_STORE.glob("*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:limit]
    pending = running = 0
    for path in files:
        state = str(_read_json(path).get("state", "")).upper()
        if state == "PENDING":
            pending += 1
        elif state == "RUNNING":
            running += 1
    settings = _read_json(JOB_STORE / "worker_settings.json")
    try:
        max_parallel = int(settings.get("max_parallel_jobs") or 4)
    except (TypeError, ValueError):
        max_parallel = 4
    return pending, running, max(1, max_parallel)


def probe(args, counts: tuple[int, int, int], lock_max_age: float) -> Snapshot:
    pending, running, max_parallel = counts
    app_port_up = _port_open(args.app_port)
    app_http_ok = app_port_up and _http_ok(f"http://127.0.0.1:{args.app_port}/healthz")
    ui_lock = _read_json(JOB_STORE / "ui.lock")
    app_pid = ui_lock.get("pid")
    app_process_alive = pid_alive(app_pid if isinstance(app_pid, int) else None)
    app_lock_age = _mtime_age(JOB_STORE / "ui.lock")

    proxy_ok = _http_ok(f"http://127.0.0.1:{args.proxy_port}/healthz")

    lock_path = JOB_STORE / "worker.lock"
    worker_lock_age = _mtime_age(lock_path)
    # Liveness is the touch cadence, not the stored pid (which survives a
    # code-change re-exec while the pid changes). Identity is resolved only
    # on the stale path, where a kill decision may be needed.
    worker_alive = worker_lock_age is not None and worker_lock_age <= lock_max_age
    worker_pid = None
    if not worker_alive:
        pids = find_worker_pids()
        if pids:
            worker_alive = True
            worker_pid = pids[0]

    return Snapshot(
        app_port_up=app_port_up,
        app_http_ok=app_http_ok,
        app_process_alive=app_process_alive,
        app_lock_age=app_lock_age,
        proxy_ok=proxy_ok,
        worker_pid=worker_pid,
        worker_alive=worker_alive,
        worker_token=process_token(worker_pid) if worker_alive else None,
        worker_age=process_age(worker_pid) if worker_alive else None,
        worker_lock_age=worker_lock_age,
        pending=pending,
        running=running,
        max_parallel=max_parallel,
    )


# --------------------------------------------------------------------------- #
# Logging / heartbeat
# --------------------------------------------------------------------------- #


def log(message: str) -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            LOG_PATH.replace(LOG_PATH.with_name(LOG_PATH.name + ".1"))
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except OSError:
        pass


def write_heartbeat(tick: int) -> None:
    try:
        JOB_STORE.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(
            json.dumps({"pid": os.getpid(), "ts": time.time(), "tick": tick}),
            encoding="utf-8",
        )
    except OSError:
        pass


def read_heartbeat() -> dict:
    return _read_json(HEARTBEAT)


def load_queue_state() -> dict:
    return _read_json(QUEUE_STATE)


def _load_ports() -> dict:
    """Effective ports written by start_suprun.ps1 (custom ports included)."""
    data = _read_json(JOB_STORE / "ports.json")
    ports: dict = {}
    for key in ("app", "proxy"):
        try:
            ports[key] = int(data[key])
        except (KeyError, TypeError, ValueError):
            continue
    return ports


def save_queue_state(state: dict) -> None:
    try:
        QUEUE_STATE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Guardian loop / backstop
# --------------------------------------------------------------------------- #


def execute(action: Action) -> None:
    if action.kind == "note":
        log("note: " + action.detail)
        return
    log("repair: " + action.detail)
    if action.kind == "start_stack":
        run_launcher([])
    elif action.kind == "restart_app":
        run_launcher(["-Restart", "-AppOnly"])
    elif action.kind == "restart_proxy":
        run_launcher(["-ProxyOnly"])
    elif action.kind == "start_worker":
        run_launcher(["-AppOnly"])
    elif action.kind == "restart_worker":
        # Defense in depth: the plan saw no RUNNING jobs, but a claim may
        # have raced the decision. Never ask for a stop while work is in flight.
        if job_counts()[1] > 0:
            log("note: worker restart skipped - a RUNNING job appeared")
            return
        # The worker is Task-Scheduler-owned, so the guardian cannot kill it.
        # It writes an intent file instead; the daemon's own pulse thread
        # exits cleanly when idle. Wait for the process to actually go away
        # (and drop its still-fresh lock) before launching, otherwise the
        # launcher's lock-age liveness check skips the restart.
        request_worker_stop(action.detail or "guardian repair")
        if not wait_for_worker_exit():
            log("note: worker did not exit after the stop request - leaving it alone")
            return
        run_launcher(["-AppOnly"])


def _spawn_guardian() -> bool:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if _WINDOWS and pythonw.exists():
        exe = pythonw
    argv = [str(exe), str(Path(__file__).resolve())]
    kwargs: dict = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if _WINDOWS:
        kwargs["creationflags"] = _CREATE_NO_WINDOW | 0x00000008  # + DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kwargs)
        return True
    except OSError as exc:
        log(f"could not start the guardian: {exc}")
        return False


def backstop(args) -> int:
    heartbeat = read_heartbeat()
    age = time.time() - float(heartbeat.get("ts", 0) or 0)
    if heartbeat and age <= max(60.0, args.health_interval * 3):
        return 0
    log(f"backstop: guardian heartbeat stale ({age:.0f}s) - starting it")
    _spawn_guardian()
    return 0


def run_guardian(args) -> int:
    heartbeat = read_heartbeat()
    if (
        heartbeat
        and heartbeat.get("pid") != os.getpid()
        and pid_alive(heartbeat.get("pid"))
        and time.time() - float(heartbeat.get("ts", 0) or 0) <= max(60.0, args.health_interval * 3)
    ):
        log("another guardian is alive - exiting")
        return 0

    JOB_STORE.mkdir(parents=True, exist_ok=True)
    state = GuardianState(queue=load_queue_state())
    tick = 0
    next_queue_at = 0.0
    counts: tuple[int, int, int] = (0, 0, 4)
    log(
        f"guardian started (pid {os.getpid()}); health {args.health_interval:.0f}s, "
        f"queue {args.queue_interval:.0f}s",
    )
    while True:
        tick += 1
        now = time.time()
        try:
            queue_due = now >= next_queue_at
            if queue_due:
                counts = job_counts()
                next_queue_at = now + args.queue_interval
            snap = probe(args, counts, args.lock_max_age)
            actions = plan(
                snap,
                state,
                now,
                queue_due=queue_due,
                lock_max_age=args.lock_max_age,
                stall_seconds=args.stall_minutes * 60.0,
                backoff_seconds=args.backoff_minutes * 60.0,
            )
            for action in actions:
                execute(action)
            save_queue_state(state.queue)
            write_heartbeat(tick)
        except Exception:  # noqa: BLE001 - the guardian must never die on one probe
            log("probe failed:\n" + traceback.format_exc())
            write_heartbeat(tick)
        time.sleep(max(5.0, args.health_interval))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="backstop: ensure the guardian is alive")
    parser.add_argument("--health-interval", type=float, default=20.0)
    parser.add_argument("--queue-interval", type=float, default=180.0)
    parser.add_argument("--lock-max-age", type=float, default=30.0)
    parser.add_argument("--stall-minutes", type=float, default=5.0)
    parser.add_argument("--backoff-minutes", type=float, default=30.0)
    parser.add_argument(
        "--app-port", type=int, default=None,
        help=f"default: .job_store/ports.json, else {APP_PORT}",
    )
    parser.add_argument(
        "--proxy-port", type=int, default=None,
        help=f"default: .job_store/ports.json, else {PROXY_PORT}",
    )
    args = parser.parse_args(argv)
    ports = _load_ports()
    args.app_port = args.app_port or ports.get("app") or APP_PORT
    args.proxy_port = args.proxy_port or ports.get("proxy") or PROXY_PORT
    return backstop(args) if args.once else run_guardian(args)


if __name__ == "__main__":
    raise SystemExit(main())
