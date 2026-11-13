"""Process identity + the runner-facing kill entry point.

The actual signalling lives in :mod:`supervisor.runtime.process_control`; this
module keeps the runner-facing names (``remember_process_identity`` /
``kill_process_tree``) and nothing else. There is deliberately no
keyword/name based cleanup here: killing by process name or command-line
substring can reach processes this run never started, which the ownership
rule forbids outright.
"""

from __future__ import annotations

import logging

from supervisor.runtime.process_control import terminate_tree

logger = logging.getLogger(__name__)

# Windows process create_time is a float; a reused PID has a different value.
_CREATE_TIME_TOLERANCE = 0.01


def remember_process_identity(proc, run_id: str | None = None) -> None:
    """Record the spawned child's create time (and run id) on *proc*.

    A later tree-kill verifies this before signalling, so a PID that exited
    and was reused by an unrelated process is never killed. The run id is
    stashed on the process object too, so callers that only hold the ``proc``
    (stream driver, base runner) still reach the ownership gate.
    """
    try:
        import psutil

        proc._suprun_create_time = psutil.Process(proc.pid).create_time()
    except Exception:  # noqa: BLE001 - identity recording must never block a turn
        try:
            proc._suprun_create_time = None
        except Exception:  # noqa: BLE001, S110 - unknown proc object
            pass
    if run_id is not None:
        try:
            proc._suprun_run_id = str(run_id)
        except Exception:  # noqa: BLE001, S110 - unknown proc object
            pass


def kill_process_tree(proc, run_id: str | None = None) -> None:
    """Kill *proc* and its contained tree — never anything else.

    Scope rules (enforced in :func:`process_control.terminate_tree`):

    - an exited direct child is never signalled (its PID may belong to an
      unrelated process now);
    - the target must be a ledgered member of the run, a member of the run's
      job, or a direct child whose create time still matches — otherwise the
      kill is refused and logged;
    - only the run's own containment scope is in scope. A process that was
      detached and reparented is outside it and is left untouched.

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
        # A fake/partial process object: it has no OS identity to prove, so
        # only its own no-op kill hook may run.
        try:
            proc.kill()
        except Exception:  # noqa: BLE001, S110 - best effort on a fake proc
            pass
        return

    owner = run_id if run_id is not None else getattr(proc, "_suprun_run_id", None)
    expected = getattr(proc, "_suprun_create_time", None)
    terminate_tree(
        owner,
        int(pid),
        reason="runner tree cleanup",
        expected_create_time=expected,
    )

