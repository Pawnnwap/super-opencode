from __future__ import annotations

import logging
import platform
import subprocess

logger = logging.getLogger(__name__)

# Windows process create_time is a float; a reused PID has a different value.
_CREATE_TIME_TOLERANCE = 0.01


def remember_process_identity(proc) -> None:
    """Record the spawned child's create time on *proc*.

    A later tree-kill verifies this before signalling, so a PID that exited
    and was reused by an unrelated process is never killed. Best effort: when
    psutil is unavailable the attribute stays None and the fallback checks
    liveness immediately before the kill.
    """
    try:
        import psutil

        proc._suprun_create_time = psutil.Process(proc.pid).create_time()
    except Exception:  # noqa: BLE001 - identity recording must never block a turn
        try:
            proc._suprun_create_time = None
        except Exception:  # noqa: BLE001, S110 - unknown proc object
            pass


def _psutil_module():
    try:
        import psutil

        return psutil
    except Exception:  # noqa: BLE001 - optional dependency at call time
        return None


def _kill_verified(psutil, pid: int, created: float | None) -> bool:
    """Kill *pid* only when it is still the process *created* identifies."""
    try:
        target = psutil.Process(pid)
        if (
            created is not None
            and abs(target.create_time() - created) > _CREATE_TIME_TOLERANCE
        ):
            logger.debug("PID %s was reused; not killing it", pid)
            return False
        target.kill()
        return True
    except Exception:  # noqa: BLE001 - already gone/denied is a no-op
        return False


def kill_process_tree(proc) -> None:
    """Kill *proc* and its CURRENT descendants, without touching anything else.

    Precision rules:
    - an already-exited direct child is never signalled (its PID may belong
      to an unrelated process now);
    - every kill is validated against the create time recorded at spawn
      (:func:`remember_process_identity`) or captured just before killing a
      descendant;
    - only the direct child's current descendant tree is in scope. A process
      that was detached and reparented (a study script started with
      ``Start-Process`` and still running) is no longer in that tree and is
      left untouched.

    The tree-kill exists because on Windows the direct child is often a
    cmd.exe shim: killing only it orphans the real CLI grandchild, which keeps
    streaming and holds file locks. Prompt cancellation is owned by the
    runner's cancel event in the stream driver; this function is only the OS
    cleanup of the engine's own process tree.
    """
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:  # noqa: BLE001, S110 - fake/partial process objects
        pass

    pid = getattr(proc, "pid", None)
    if pid is None:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001, S110 - best effort on a fake proc
            pass
        return

    psutil = _psutil_module()
    if psutil is not None:
        try:
            root = psutil.Process(pid)
            actual = root.create_time()
            expected = getattr(proc, "_suprun_create_time", None)
            if (
                expected is not None
                and abs(actual - expected) > _CREATE_TIME_TOLERANCE
            ):
                logger.warning("PID %s was reused; skipping tree kill", pid)
                return
            descendants = root.children(recursive=True)
        except Exception:  # noqa: BLE001 - gone or denied; try the fallback
            root = None
            descendants = []
        if root is not None:
            targets: list[tuple[int, float]] = []
            for child in descendants:
                try:
                    targets.append((child.pid, child.create_time()))
                except Exception:  # noqa: BLE001, S112 - child vanished mid-snapshot
                    continue
            for child_pid, created in targets:
                _kill_verified(psutil, child_pid, created)
            if _kill_verified(psutil, root.pid, actual):
                return
            try:
                if proc.poll() is not None:
                    return
            except Exception:  # noqa: BLE001, S110 - fake/partial process objects
                pass

    # Fallback without psutil: only while the direct child is still alive.
    if platform.system() == "Windows":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error killing process tree: %s", exc)
    try:
        proc.kill()
    except Exception:  # noqa: BLE001, S110 - fallback on an already-dead child
        pass


def kill_processes_by_keywords(keywords: list[str]) -> None:
    try:
        system = platform.system()
        if system == "Windows":
            try:
                result = subprocess.run(
                    ["tasklist", "/fo", "csv"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                found_processes = False
                for line in result.stdout.splitlines()[1:]:
                    if not line.strip():
                        continue
                    parts = line.split('","')
                    if len(parts) < 2:
                        continue
                    process_name = parts[0].strip('"').lower()
                    if any(keyword in process_name for keyword in keywords):
                        pid = parts[1].strip('"')
                        try:
                            subprocess.run(
                                ["taskkill", "/PID", pid, "/F"],
                                capture_output=True,
                                text=True,
                                timeout=2,
                                check=False,
                            )
                            found_processes = True
                        except Exception as exc:  # noqa: BLE001 - best effort
                            logger.warning("Error killing process %s: %s", pid, exc)
                if not found_processes:
                    logger.debug("No matching processes found to kill")
                return
            except subprocess.TimeoutExpired:
                logger.debug("Primary process scan timed out, using fallback method")
            except Exception as exc:  # noqa: BLE001 - fallback follows
                logger.warning("Primary process scan failed: %s", exc)

            try:
                result = subprocess.run(
                    ["tasklist"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                found_processes = False
                for line in result.stdout.splitlines()[3:]:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    process_name = parts[0].lower()
                    if any(keyword in process_name for keyword in keywords):
                        pid = parts[1]
                        try:
                            subprocess.run(
                                ["taskkill", "/PID", pid, "/F"],
                                capture_output=True,
                                text=True,
                                timeout=2,
                                check=False,
                            )
                            found_processes = True
                        except Exception as exc:  # noqa: BLE001 - best effort
                            logger.warning("Error killing process %s: %s", pid, exc)
                if not found_processes:
                    logger.debug("No matching processes found to kill (fallback)")
            except Exception as exc:  # noqa: BLE001 - best effort
                logger.warning("Fallback process scan failed: %s", exc)
            return

        try:
            subprocess.run(
                ["pkill", "-f", "|".join(keywords)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except FileNotFoundError:
            logger.debug("pkill not available on this system")
        except subprocess.TimeoutExpired:
            logger.debug("Unix pkill command timed out")
        except Exception as exc:  # noqa: BLE001 - best effort
            logger.warning("Unix pkill failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - never raise from cleanup
        logger.warning("Error while killing processes: %s", exc)
