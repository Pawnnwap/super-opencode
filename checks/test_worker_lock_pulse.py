"""Worker liveness vs. poll-loop stalls: the 2026-11-11 silent-abandon bug.

The watchdog tree-kills the worker when ``worker.lock`` ages past its
threshold (30 s) and the successor marks every RUNNING job ABANDONED. The
lock was touched only at the top of the poll loop, so ONE blocked poll step
aged the lock while job threads were perfectly healthy - on 2026-11-11 that
cost every live run four times (16:55, 17:51, 20:15, 22:23), each time
"abandoned silently at some stage".

These tests drive the REAL poll loop (``worker_service.main``) with one
monkeypatched slow step and assert the exact user-visible symptom:

  - the lock must stay fresh while the loop body is stalled (the watchdog
    must NOT see a dead worker and kill healthy runs);
  - the stall must leave evidence naming the blocked step (fatal log);
  - the ABANDONED record must say where the run stopped and why.
"""

import threading
import time

import pytest

from services.jobs import job_manager as jm
from services.jobs import worker_service as ws
from services.jobs.state_store import JobStateStore


@pytest.fixture(autouse=True)
def _stub_loops(monkeypatch):
    class Noop:
        def __init__(self, config):
            self.runner = None

        def run_streaming(self):
            yield {"level": "success", "msg": "ok"}

    monkeypatch.setattr(jm, "SupervisorLoop", Noop)


def _start_worker_main(monkeypatch, tmp_path):
    """Run the real daemon ``main`` in a thread against a tmp store.

    One ``code_fingerprint`` call (the poll loop's freshness step) is made to
    block - a stand-in for any slow/blocked poll step (open() stalled behind
    a filesystem filter, retry-sleep storm, ...). Nothing else is faked: the
    loop, the lock file, and the pulse all run for real.
    """
    store_dir = tmp_path / ".job_store"
    monkeypatch.setattr(ws, "JOB_STORE_DIR", str(store_dir))
    monkeypatch.setattr(ws, "_POLL_SECONDS", 0.02)
    monkeypatch.setattr(ws, "_CODE_CHECK_SECONDS", 0)
    # raising=False: these knobs exist only after the fix; the red run of the
    # first test must still execute the pre-fix loop.
    monkeypatch.setattr(ws, "_STALL_DUMP_SECONDS", 0.4, raising=False)
    monkeypatch.setattr(ws, "_PULSE_SECONDS", 0.05, raising=False)
    monkeypatch.chdir(tmp_path)  # death_capture writes the fatal log to CWD
    monkeypatch.setattr(ws.death_capture, "_handles", [])
    # Never let the freshness mismatch re-exec the TEST process image.
    # Direct assignment, not monkeypatch: the main() thread outlives the test,
    # and after monkeypatch undo it would call the REAL _reexec_worker - a
    # real os.execv replacing pytest with a worker daemon.
    ws._reexec_worker = lambda: None

    real_fingerprint = ws.code_fingerprint
    stall = threading.Event()
    stalled_once = False

    def slow_fingerprint():
        nonlocal stalled_once
        if not stalled_once:
            stalled_once = True
            stall.set()
            time.sleep(1.2)  # the mystery poll-loop stall
        return real_fingerprint()

    monkeypatch.setattr(ws, "code_fingerprint", slow_fingerprint)
    monkeypatch.setattr(ws, "_read_settings", lambda _d: {})

    # _LOADED_CODE_FINGERPRINT is captured at import; force the freshness
    # step to run every cycle so the slow call actually happens.
    monkeypatch.setattr(ws, "_LOADED_CODE_FINGERPRINT", "different")

    t = threading.Thread(target=ws.main, daemon=True)
    t.start()
    assert stall.wait(10), "poll loop never reached the instrumented step"
    return tmp_path


def _lock_age(tmp_path) -> float:
    lock = tmp_path / ".job_store" / "worker.lock"
    return time.time() - lock.stat().st_mtime


def test_lock_stays_fresh_while_poll_loop_stalls(monkeypatch, tmp_path):
    """A blocked poll step must not age the lock past the watchdog threshold.

    Before the fix the lock was only touched per poll cycle: a 1.2 s stall
    aged it past any tight threshold and a 30-85 s stall (as on 2026-11-11)
    sent the watchdog tree-killer after healthy runs.
    """
    _start_worker_main(monkeypatch, tmp_path)
    time.sleep(0.6)  # mid-stall: the loop is blocked inside the slow step
    assert _lock_age(tmp_path) < 0.4, (
        "lock aged during a poll-loop stall - the watchdog would kill the tree"
    )


def test_stall_leaves_forensics_naming_the_step(monkeypatch, tmp_path):
    """A stalled loop must dump thread stacks + the blocked step (fatal log)."""
    _start_worker_main(monkeypatch, tmp_path)
    deadline = time.time() + 10
    fatal = tmp_path / "job_worker.fatal.log"
    while time.time() < deadline:
        if fatal.exists() and "poll loop stalled" in fatal.read_text(encoding="utf-8"):
            break
        time.sleep(0.1)
    text = fatal.read_text(encoding="utf-8") if fatal.exists() else ""
    assert "poll loop stalled" in text, "no stall forensics were written"
    assert "step '" in text, "stall forensics do not name the blocked step"


def test_abandoned_record_carries_stage_and_reason(tmp_path):
    """ABANDONED must not be a silent tombstone: say where and why it stopped."""
    store = JobStateStore(str(tmp_path / ".job_store"))
    store.save_job_state("run_x", {"type": "run", "state": "RUNNING"})
    store.append_log("run_x", {
        "level": "tool",
        "msg": "[shell] python build_indicator.py --stage screen",
    })

    class _M:
        pass

    manager = _M()
    manager.store = store
    flagged = ws._mark_orphans(manager)

    assert flagged == ["run_x"]
    state = store.get_job_state("run_x")
    assert state["state"] == "ABANDONED"
    assert state.get("abandon_reason"), "no abandon_reason recorded"
    assert "screen" in (state.get("last_activity") or ""), (
        "last_activity does not show the stage the run stopped at"
    )
    logs = store.get_logs("run_x")
    warn = [e for e in logs if e.get("level") == "warn"]
    assert warn and "screen" in warn[-1]["msg"], (
        "the abandon log line does not show where the run stopped"
    )
