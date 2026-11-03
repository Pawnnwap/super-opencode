"""Crash/exit forensics for the serving and worker processes.

Every recent UI death (2026-11-01, 11-02, 11-03) was SILENT: the process
vanished with empty stderr, nothing in the Windows event log, no WER report.
That made "loop frozen", "serving dead", "hard-killed from outside" and
"crashed" indistinguishable. This module makes the next death leave evidence:

- ``faulthandler`` dumps all thread tracebacks on fatal faults (segfault,
  stack overflow) into the process's fatal log;
- ``sys.excepthook`` / ``threading.excepthook`` record unhandled exceptions
  that would otherwise vanish;
- one identity line at startup (pid, parent chain, argv) pins WHO was
  running and WHO spawned it - if a launcher job object took the tree down,
  the parent chain is the fingerprint.

The companion signal is the launcher's exit-code sentinel
(``<name>.exitcode``, see start_suprun.ps1): its presence proves a clean
exit (with code), its absence after a death proves the tree was hard-killed.
"""

from __future__ import annotations

import faulthandler
import json
import sys
import threading
import time
import traceback

# Keep the fatal-log handle alive for the process lifetime: faulthandler
# writes into it directly at crash time, and a closed/GC'd file object would
# lose the dump.
_handles: list = []


def install(log_name: str) -> None:
    """Arm crash capture for this process, logging to ``log_name``."""
    fh = open(log_name, "a", encoding="utf-8")  # noqa: SIM115 - lifetime = process
    _handles.append(fh)
    faulthandler.enable(file=fh, all_threads=True)

    def _dump(header: str, tb_text: str) -> None:
        fh.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {header}\n{tb_text}")
        fh.flush()

    def _sys_excepthook(exc_type, exc, tb) -> None:
        _dump(
            f"unhandled exception in {threading.current_thread().name}",
            "".join(traceback.format_exception(exc_type, exc, tb)),
        )
        sys.__excepthook__(exc_type, exc, tb)

    def _thread_excepthook(args) -> None:
        _dump(
            f"unhandled exception in thread {args.thread.name!r}",
            "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback,
            )),
        )

    sys.excepthook = _sys_excepthook
    threading.excepthook = _thread_excepthook
    fh.write(_identity_line() + "\n")
    fh.flush()


def _identity_line() -> str:
    """One JSON line: who we are and the chain of processes that spawned us."""
    parents = []
    try:
        import psutil

        proc = psutil.Process()
        parents.append(_proc_info(proc))
        parent = proc.parent()
        # Walk the launcher chain (venv shim -> cmd -> powershell -> ...): a
        # mass death that sweeps exactly this chain points at a job object or
        # console teardown upstream, not at a crash in our code.
        for _ in range(4):
            if parent is None:
                break
            parents.append(_proc_info(parent))
            parent = parent.parent()
    except Exception as exc:  # noqa: BLE001 - forensics must never crash startup
        parents.append({"error": repr(exc)})
    return json.dumps({
        "ts": time.time(),
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "argv": sys.argv,
        "chain": parents,
    }, ensure_ascii=False)


def _proc_info(proc) -> dict:
    with proc.oneshot():
        return {
            "pid": proc.pid,
            "name": proc.name(),
            "create_time": proc.create_time(),
            "cmdline": " ".join(proc.cmdline())[:200],
        }
