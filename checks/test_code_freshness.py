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


def test_freshness_action_holds_when_busy_and_restarts_when_idle():
    assert freshness_action("a", "a", 4) == "ok"
    assert freshness_action("a", "b", 0) == "restart"
    assert freshness_action("a", "b", 2) == "hold"


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
