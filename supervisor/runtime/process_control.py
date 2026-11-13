"""The only module allowed to signal a process.

Hard rule enforced here: a kill is legal only when the target's ownership can
be proved for the calling run — either

* the run's ledger holds a ``live`` record whose ``create_time`` matches the
  process right now (spawn-time registration), or
* the target is a member of that run's containment scope (job object), which
  the kernel guarantees means "descended from this run's root", or
* the target is a direct child of this very process and its create time
  matches the value captured at spawn (the pre-ledger compatibility path).

Everything else is refused and appended to ``<run>.kill_refusals.jsonl``.
Name matching, keyword matching, ``/IM``, ``-Name``, ``pkill -f`` and
wildcard/parent-tree kills have no code path here at all.

Ownership is revalidated immediately before signalling, so a pid that exited
and got recycled between the decision and the kill is never touched.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field

from supervisor.runtime.job_object import JobScope
from supervisor.runtime.process_registry import (
    CREATE_TIME_TOLERANCE,
    ProcessRegistry,
    ProcRecord,
)

logger = logging.getLogger(__name__)

_WINDOWS = os.name == "nt"

# Pids 0-4 are the idle/system processes; no run ever owns one.
_MIN_OWNED_PID = 5


class KillRefused(PermissionError):
    """Raised when a kill target's ownership cannot be proved for the run."""


@dataclass
class SpawnedProcess:
    """A child plus the containment scope and ledger record that own it."""

    popen: subprocess.Popen
    job: JobScope | None
    record: ProcRecord
    extra: dict = field(default_factory=dict)

    @property
    def pid(self) -> int | None:
        return getattr(self.popen, "pid", None)


# run_id -> the live job scope owning that run's tree (in THIS process).
_SCOPES: dict[str, JobScope] = {}


# --------------------------------------------------------------------------- #
# Scope plumbing
# --------------------------------------------------------------------------- #


def scope_for_run(run_id: str, *, create: bool = True) -> JobScope | None:
    """The job scope of *run_id* in this process (created on first use)."""
    key = str(run_id or "")
    if not key:
        return None
    existing = _SCOPES.get(key)
    if existing is not None and existing.active:
        return existing
    if not create:
        return None
    scope = JobScope.create(kill_on_close=False)
    _SCOPES[key] = scope
    return scope


def close_scope(run_id: str) -> None:
    """Close (but never terminate) the run's scope; members keep running."""
    scope = _SCOPES.pop(str(run_id or ""), None)
    if scope is not None:
        scope.close()


# --------------------------------------------------------------------------- #
# Spawning
# --------------------------------------------------------------------------- #


def spawn_tracked(
    *,
    run_id: str,
    role: str,
    argv: list[str],
    cwd: str | os.PathLike | None = None,
    env: dict[str, str] | None = None,
    job: JobScope | None = None,
    kill_on_close: bool = False,
    **popen_kwargs,
) -> SpawnedProcess:
    """Start *argv* inside the run's scope and register it in the ledger.

    The child is assigned to the scope right after ``Popen`` returns; the
    sub-millisecond window is accepted because a process that has not been
    assigned yet also has not been registered, so no ownership proof can
    exist for it (fail-closed rather than unsafe).
    """
    scope = job
    if scope is None and run_id:
        scope = scope_for_run(run_id)
    if scope is not None and not scope.active:
        scope = None
    if kill_on_close and scope is None:
        scope = JobScope.create(kill_on_close=True)

    popen_kwargs.setdefault("stdin", subprocess.DEVNULL)
    if not _WINDOWS:
        # POSIX containment: the child leads its own session/process group, so
        # the scope's pgid is real and killpg reaches exactly this tree.
        popen_kwargs.setdefault("start_new_session", True)
    child = subprocess.Popen(argv, cwd=cwd, env=env, **popen_kwargs)

    pid = getattr(child, "pid", None)
    if pid is None:
        # A fake/partial process object (test doubles): there is no OS
        # identity to contain or ledger, so nothing is registered and no
        # ownership proof can ever exist for it.
        return SpawnedProcess(
            popen=child,
            job=scope,
            record=ProcRecord(
                run_id=str(run_id),
                pid=0,
                create_time=0.0,
                role=str(role or "child"),
                exe=str(argv[0] if argv else ""),
                cmdline=_join(argv),
                parent_pid=os.getpid(),
                job_id=None,
                ts=time.time(),
                state="live",
            ),
        )

    assigned = False
    if scope is not None:
        assigned = scope.assign(int(pid))

    record = ProcRecord(
        run_id=str(run_id),
        pid=int(pid),
        create_time=_create_time(int(pid)),
        role=str(role or "child"),
        exe=str(argv[0] if argv else ""),
        cmdline=_join(argv),
        parent_pid=os.getpid(),
        job_id=(scope.job_id if (scope is not None and assigned) else None),
        ts=time.time(),
        state="live",
    )
    if run_id:
        ProcessRegistry(run_id).register(record)

    return SpawnedProcess(popen=child, job=scope, record=record)


# --------------------------------------------------------------------------- #
# Ownership proofs
# --------------------------------------------------------------------------- #


def verify_owned(run_id: str, pid: int) -> bool:
    """True only when *pid* provably belongs to *run_id* right now."""
    if not _plausible_pid(pid) or not run_id:
        return False
    if _protected(pid):
        return False
    registry = ProcessRegistry(run_id)
    record = registry.find(int(pid))
    if record is not None and record.state == "live":
        live = _create_time(int(pid))
        if live is not None and abs(live - record.create_time) <= CREATE_TIME_TOLERANCE:
            return True
        # A recycled pid: the record is dead evidence from now on.
        registry.mark_exited(int(pid))
        return False
    # Not registered explicitly: the kernel can still prove descent when the
    # target sits in this run's own job (the engine root's scope).
    scope = _SCOPES.get(str(run_id))
    if scope is not None and scope.active and scope.contains(int(pid)):
        created = _create_time(int(pid))
        if created is not None:
            registry.register(
                ProcRecord(
                    run_id=str(run_id),
                    pid=int(pid),
                    create_time=created,
                    role="adopted",
                    exe="",
                    cmdline="",
                    parent_pid=None,
                    job_id=scope.job_id,
                    ts=time.time(),
                    state="live",
                ),
            )
            return True
    return False


def _plausible_pid(pid: int) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    return value >= _MIN_OWNED_PID


def _protected(pid: int) -> bool:
    """Never killable, whoever asks: us, our parent chain, the OS."""
    if not _plausible_pid(pid):
        return True
    value = int(pid)
    if value == os.getpid():
        return True
    try:
        import psutil
    except Exception:  # noqa: BLE001 - without psutil, refuse unknown targets
        return True
    try:
        seen = {os.getpid()}
        parent = psutil.Process().parent()
        for _ in range(8):
            if parent is None:
                break
            if parent.pid == value:
                return True
            if parent.pid in seen:
                break
            seen.add(parent.pid)
            parent = parent.parent()
    except Exception:  # noqa: BLE001 - protection check must never fail open
        return True
    return False


def _is_direct_child(pid: int, expected_create_time: float | None) -> bool:
    """Kernel-provable: *pid* is this process's own child and not a reused pid."""
    psutil = _psutil()
    if psutil is None:
        return False
    try:
        proc = psutil.Process(int(pid))
        parent = proc.parent()
        if parent is None or parent.pid != os.getpid():
            return False
        return not (
            expected_create_time is not None
            and abs(proc.create_time() - float(expected_create_time)) > CREATE_TIME_TOLERANCE
        )
    except Exception:  # noqa: BLE001 - gone or denied means no proof
        return False


# --------------------------------------------------------------------------- #
# Termination
# --------------------------------------------------------------------------- #


def terminate_pid(run_id: str, pid: int, *, reason: str = "") -> bool:
    """Kill one process, but only after proving the run owns it."""
    if _protected(pid):
        _refuse(run_id, f"pid {pid}", _with_reason("protected process", reason))
        raise KillRefused(f"refusing to kill protected pid {pid}")
    if not verify_owned(run_id, pid):
        if _gone(pid):
            # Nothing left to signal: the process exited on its own between
            # the ownership proof and this call (a venv ``python.exe``
            # redirector dies with the interpreter it started). The kill's
            # goal is met, and a refusal would be false audit evidence.
            if run_id:
                ProcessRegistry(run_id).mark_exited(int(pid))
            return True
        _refuse(run_id, f"pid {pid}", _with_reason("not owned by this run", reason))
        raise KillRefused(
            f"refusing to kill pid {pid}: not owned by run {run_id!r}",
        )
    psutil = _psutil()
    if psutil is None:
        return False
    try:
        proc = psutil.Process(int(pid))
        # Re-check identity at the last possible moment: the proof above and
        # this kill are two syscalls apart, and a pid can be reused in between.
        record = ProcessRegistry(run_id).find(int(pid))
        if (
            record is not None
            and record.state == "live"
            and abs(proc.create_time() - record.create_time) > CREATE_TIME_TOLERANCE
        ):
            _refuse(run_id, f"pid {pid}", "pid was reused")
            return False
        proc.kill()
        ProcessRegistry(run_id).mark_exited(int(pid))
        logger.info("killed pid %s of run %s (%s)", pid, run_id, reason or "no reason")
        return True
    except Exception:  # noqa: BLE001 - already gone is a success
        return False


def terminate_tree(
    run_id: str | None,
    root_pid: int,
    *,
    reason: str = "",
    expected_create_time: float | None = None,
) -> bool:
    """Terminate ONE spawned root and its current descendant tree.

    The run's job object is deliberately NOT terminated here. It contains
    every child of the run, so ``TerminateJobObject`` would take the engine
    and all sibling processes down whenever a single helper timed out (a
    judge ``run_tests`` pytest call, a MATLAB probe). The job stays the
    run-level backstop (:func:`terminate_run`); this function is the
    per-child operation and only ever reaches *root_pid*'s own subtree.

    The root's ownership must be proved (ledger record, or a direct child
    whose create time matches), and only THEN is its *current* descendant
    tree swept, each link verified against a create time captured just
    before the kill. An unproved root is refused and logged; nothing is ever
    swept by name or keyword.
    """
    key = str(run_id or "")
    owned = bool(key) and verify_owned(key, int(root_pid))
    if not owned and not _is_direct_child(int(root_pid), expected_create_time):
        _refuse(key or "<no-run>", f"pid {root_pid}", reason or "tree root not owned")
        return False

    # The root is ours; its current descendants descend from it. Verify each
    # child's identity right before signalling (a child may exit and its pid
    # be reused while we walk the tree).
    _kill_descendants(int(root_pid))
    # Killing the descendants can take the root with them: a venv
    # ``python.exe`` is a redirector that exits once the interpreter it
    # started is gone. A root that has already vanished is a completed kill,
    # not a failure — and it must not be re-verified, or the audit trail gets
    # a false "not owned" refusal for a process that simply exited.
    if _psutil() is not None and _create_time(int(root_pid)) is None:
        if key:
            ProcessRegistry(key).mark_exited(int(root_pid))
        logger.info("tree root %s already gone (%s)", root_pid, reason or "tree root")
        return True
    if owned:
        try:
            if terminate_pid(key, int(root_pid), reason=reason or "tree root"):
                return True
        except KillRefused:
            pass
    expected = (
        expected_create_time
        if expected_create_time is not None
        else _create_time(int(root_pid))
    )
    if expected is None:
        return False
    killed = _kill_now(int(root_pid), expected)
    if killed and key:
        ProcessRegistry(key).mark_exited(int(root_pid))
    if killed:
        logger.info("killed direct child %s (%s)", root_pid, reason or "tree root")
    return killed


def _kill_descendants(root_pid: int) -> int:
    """Kill the root's CURRENT descendant tree, verifying every identity."""
    psutil = _psutil()
    if psutil is None:
        return 0
    try:
        children = psutil.Process(int(root_pid)).children(recursive=True)
    except Exception:  # noqa: BLE001 - gone or denied
        return 0
    targets: list[tuple[int, float]] = []
    for child in children:
        try:
            targets.append((int(child.pid), float(child.create_time())))
        except Exception:  # noqa: BLE001, S112 - vanished mid-snapshot
            continue
    killed = 0
    for pid, created in targets:
        if _protected(pid):
            continue
        if _kill_now(pid, created):
            killed += 1
    return killed


def terminate_run(run_id: str, *, reason: str = "") -> int:
    """Terminate every ledgered live pid of *run_id*; returns the kill count."""
    key = str(run_id or "")
    if not key:
        return 0
    killed = 0
    scope = _SCOPES.get(key)
    if scope is not None and scope.active and scope.terminate():
        _mark_members_exited(key)
        killed += 1
    for record in ProcessRegistry(key).live_records():
        if _protected(record.pid) or record.state != "live":
            continue
        try:
            if verify_owned(key, record.pid) and _kill_now(record.pid, record.create_time):
                killed += 1
                ProcessRegistry(key).mark_exited(record.pid)
        except KillRefused:
            continue
    logger.info("terminated run %s (%d target(s), %s)", key, killed, reason or "no reason")
    close_scope(key)
    return killed


def _kill_now(pid: int, expected_create_time: float) -> bool:
    psutil = _psutil()
    if psutil is None:
        return False
    try:
        proc = psutil.Process(int(pid))
        if abs(proc.create_time() - expected_create_time) > CREATE_TIME_TOLERANCE:
            return False
        proc.kill()
        return True
    except Exception:  # noqa: BLE001 - already gone
        return False


def _mark_members_exited(run_id: str) -> None:
    """Close ledger records whose process is really gone (never guesses)."""
    registry = ProcessRegistry(run_id)
    for record in registry.live_records():
        live = _create_time(record.pid)
        if live is None or abs(live - record.create_time) > CREATE_TIME_TOLERANCE:
            registry.mark_exited(record.pid)


def _refuse(run_id: str, target: str, reason: str) -> None:
    key = str(run_id or "")
    logger.warning("kill refused for run %r: %s (%s)", key, target, reason)
    if key and key != "<no-run>":
        ProcessRegistry(key).record_refusal("terminate", target, reason)


def _with_reason(verdict: str, context: str) -> str:
    """Refusal text keeps the verdict first and the caller's context after it."""
    return f"{verdict} ({context})" if context else verdict


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #


def _create_time(pid: int) -> float | None:
    psutil = _psutil()
    if psutil is None:
        return None
    try:
        return float(psutil.Process(int(pid)).create_time())
    except Exception:  # noqa: BLE001 - gone
        return None


def _gone(pid: int) -> bool:
    """True only when psutil positively reports the pid as absent.

    Without psutil nothing can be proven, so this stays False and callers keep
    their fail-closed behaviour.
    """
    psutil = _psutil()
    if psutil is None:
        return False
    try:
        return not psutil.pid_exists(int(pid))
    except Exception:  # noqa: BLE001 - unknown means "not proven gone"
        return False


def _join(argv: list[str]) -> str:
    return " ".join(str(item) for item in argv)[:600]


def _psutil():
    try:
        import psutil

        return psutil
    except Exception:  # noqa: BLE001 - optional at call time
        return None


__all__ = [
    "KillRefused",
    "SpawnedProcess",
    "close_scope",
    "scope_for_run",
    "spawn_tracked",
    "terminate_pid",
    "terminate_run",
    "terminate_tree",
    "verify_owned",
]
