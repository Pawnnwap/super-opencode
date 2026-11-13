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


def test_stop_request_is_ignored_while_a_job_is_claimed_but_not_yet_running(
    tmp_path,
):
    """A just-claimed job must not be killed by a guardian stop request.

    The store only knows RUNNING records, and a job thread writes RUNNING
    after it starts, so the disk view alone can call a busy worker idle. The
    pulse therefore also consults the in-process live-job count.
    """
    store_dir = tmp_path / ".job_store"
    store_dir.mkdir()
    (store_dir / ws._STOP_FILE).write_text("{}", encoding="utf-8")

    pulse = ws._LoopPulse(store_dir, active_jobs=lambda: 1)
    assert pulse._stop_requested() is False
    assert (store_dir / ws._STOP_FILE).exists(), (
        "the stop request was consumed while a job was in flight"
    )

    idle = ws._LoopPulse(store_dir, active_jobs=lambda: 0)
    assert idle._stop_requested() is True
    assert not (store_dir / ws._STOP_FILE).exists()


def test_stop_request_exits_from_the_main_loop_not_os_exit(monkeypatch, tmp_path):
    """The pulse must not os._exit: job threads need their finally cleanup."""
    store_dir = tmp_path / ".job_store"
    store_dir.mkdir()
    (store_dir / ws._STOP_FILE).write_text("{}", encoding="utf-8")

    monkeypatch.setattr(ws, "JOB_STORE_DIR", str(store_dir))
    monkeypatch.setattr(ws, "_POLL_SECONDS", 0.02)
    monkeypatch.setattr(ws, "_PULSE_SECONDS", 0.02)
    monkeypatch.setattr(ws, "_CODE_CHECK_SECONDS", 3600.0)
    monkeypatch.setattr(ws, "_PRUNE_SECONDS", 3600.0)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ws.death_capture, "_handles", [])
    ws._reexec_worker = lambda: None
    monkeypatch.setattr(ws, "_read_settings", lambda _d: {})

    exited = {"hard": False}
    monkeypatch.setattr(ws.os, "_exit", lambda _code: exited.__setitem__("hard", True))

    result: dict = {}

    def run_main():
        result["code"] = ws.main()

    thread = threading.Thread(target=run_main, daemon=True)
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive(), "the worker did not exit on the stop request"
    assert exited["hard"] is False, "the worker exited via os._exit"
    assert result.get("code") == 0


def test_pending_jobs_do_not_rehash_the_tree_every_poll(monkeypatch, tmp_path):
    """The freshness throttle must hold even while a PENDING job waits.

    check_freshness() used to reset the throttle clock itself, and the claim
    path called it unconditionally - so one queued job turned the 10 s
    throttle into a full-tree SHA-256 per poll (1 s).
    """
    store_dir = tmp_path / ".job_store"
    monkeypatch.setattr(ws, "JOB_STORE_DIR", str(store_dir))
    monkeypatch.setattr(ws, "_POLL_SECONDS", 0.02)
    monkeypatch.setattr(ws, "_CODE_CHECK_SECONDS", 3600.0)
    monkeypatch.setattr(ws, "_PRUNE_SECONDS", 3600.0)
    monkeypatch.setattr(ws, "_PULSE_SECONDS", 0.05, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ws.death_capture, "_handles", [])
    ws._reexec_worker = lambda: None

    calls = {"n": 0}

    def counting_fingerprint():
        calls["n"] += 1
        return "same"

    monkeypatch.setattr(ws, "code_fingerprint", counting_fingerprint)
    monkeypatch.setattr(ws, "_LOADED_CODE_FINGERPRINT", "same")
    monkeypatch.setattr(ws, "_read_settings", lambda _d: {})

    # A PENDING record that can never be claimed (no free slot is not the
    # point here - the claim path is what used to re-hash).
    store = JobStateStore(str(store_dir))
    store.save_job_state("run_waiting", {
        "type": "run", "state": "PENDING", "created_at": 1.0,
        "config": {"workspace": str(tmp_path / "ws"), "protocol_path": "p.md"},
    })

    t = threading.Thread(target=ws.main, daemon=True)
    t.start()
    time.sleep(1.0)
    # One startup check at most; the 3600 s throttle must suppress the rest.
    assert calls["n"] <= 1, (
        f"code_fingerprint ran {calls['n']}x in 1 s with a PENDING job - "
        "the throttle was bypassed"
    )
