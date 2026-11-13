"""The job worker must not run a job on code the deployment already replaced.

Regression cover for 2026-11-05: the worker daemon had been up since the
previous afternoon, so a lane launched after the attainment-based BLOCKED
exit landed still ran the old change-based rule and looped for 100+ turns.
"""
import os

from services.jobs.code_freshness import code_fingerprint, freshness_action


def test_fingerprint_tracks_content_not_mtime(tmp_path):
    (tmp_path / "supervisor").mkdir()
    (tmp_path / "services").mkdir()
    path = tmp_path / "supervisor" / "loop.py"
    path.write_text("x = 1\n", encoding="utf-8")
    first = code_fingerprint(tmp_path)

    os.utime(path, (1, 1))
    assert code_fingerprint(tmp_path) == first

    path.write_text("x = 2\n", encoding="utf-8")
    assert code_fingerprint(tmp_path) != first


def test_freshness_action_keeps_claiming_when_busy_and_restarts_when_idle():
    assert freshness_action("a", "a", 4) == "ok"
    assert freshness_action("a", "b", 0) == "restart"
    # Live jobs keep their code, but the queue must keep filling: the cap is
    # the only limit on total parallelism (2026-11-13: two free slots sat idle
    # for a whole batch because the gate parked the queue).
    assert freshness_action("a", "b", 2) == "stale"


def test_worker_start_keeps_queued_pending_jobs(tmp_path):
    """A worker restart must not throw away jobs enqueued while it was down."""
    from services.jobs.job_manager import JobManager
    from services.jobs.worker_service import _mark_orphans

    manager = JobManager(str(tmp_path / ".job_store"))
    manager.store.save_job_state(
        "job_pending", {"type": "run", "state": "PENDING", "config": {}},
    )
    manager.store.save_job_state(
        "job_running", {"type": "run", "state": "RUNNING", "config": {}},
    )
    flagged = _mark_orphans(manager)
    assert flagged == ["job_running"]
    assert manager.store.get_job_state("job_pending")["state"] == "PENDING"
    assert manager.store.get_job_state("job_running")["state"] == "ABANDONED"


def test_stale_code_does_not_park_the_queue(monkeypatch, tmp_path):
    """A changed tree must not leave free slots idle behind a PENDING job.

    Measured 2026-11-13: two runs were live, the tree changed, and the next
    batch's two replicas sat PENDING for the whole drain even though the cap
    (4) had two free slots. The cap is the only limit on total parallelism.
    """
    import threading
    import time

    from services.jobs import job_manager as jm
    from services.jobs import worker_service as ws
    from services.jobs.state_store import JobStateStore

    release = threading.Event()

    class BlockingLoop:
        """Stays live until released, so the worker is never idle."""

        def __init__(self, config):
            self.runner = None

        def request_stop(self) -> None:
            release.set()

        def run_streaming(self):
            yield {"level": "info", "msg": "start"}
            while not release.is_set():
                time.sleep(0.02)
                yield {"level": "info", "msg": "ping"}
            yield {"level": "success", "msg": "done"}

    monkeypatch.setattr(jm, "SupervisorLoop", BlockingLoop)
    store_dir = tmp_path / ".job_store"
    monkeypatch.setattr(ws, "JOB_STORE_DIR", str(store_dir))
    monkeypatch.setattr(ws, "_POLL_SECONDS", 0.02)
    monkeypatch.setattr(ws, "_CODE_CHECK_SECONDS", 0.0)
    monkeypatch.setattr(ws, "_PRUNE_SECONDS", 3600.0)
    monkeypatch.setattr(ws, "_PULSE_SECONDS", 0.05, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ws.death_capture, "_handles", [])
    ws._reexec_worker = lambda: None
    # The tree reads as changed for the whole test; with a live job the
    # freshness action is "stale" on every poll.
    monkeypatch.setattr(ws, "code_fingerprint", lambda: "disk")
    monkeypatch.setattr(ws, "_LOADED_CODE_FINGERPRINT", "loaded")
    monkeypatch.setattr(ws, "_read_settings", lambda _d: {})

    store = JobStateStore(str(store_dir))

    def enqueue(job_id: str, created: float) -> None:
        ws_dir = tmp_path / job_id
        ws_dir.mkdir()
        store.save_job_state(job_id, {
            "type": "run", "state": "PENDING", "created_at": created,
            "config": {
                "workspace": str(ws_dir),
                "protocol_path": str(ws_dir / "protocol.md"),
                "engine": "opencode",
            },
        })

    def wait_state(job_id: str, wanted: tuple[str, ...], timeout: float = 10.0) -> str:
        deadline = time.time() + timeout
        state = ""
        while time.time() < deadline:
            state = str((store.get_job_state(job_id) or {}).get("state", ""))
            if state in wanted:
                return state
            time.sleep(0.05)
        return state

    enqueue("run_first", 1.0)
    threading.Thread(target=ws.main, daemon=True).start()
    assert wait_state("run_first", ("RUNNING",)) == "RUNNING"

    # The worker is now busy on stale code: the next job must still be claimed.
    enqueue("run_second", 2.0)
    state = wait_state("run_second", ("RUNNING", "SUCCESS"))
    assert state in ("RUNNING", "SUCCESS"), (
        f"PENDING job stayed {state!r} while the code fingerprint was stale"
    )
    release.set()
