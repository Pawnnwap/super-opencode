""";job_store retention: terminal payloads pruned past the cap, summaries kept."""

import json
import time

from services.jobs.job_manager import JobManager
from services.jobs.state_store import JobStateStore


def _seed(store: JobStateStore, n: int):
    for i in range(n):
        jid = f"run_{i:03d}"
        store.save_job_state(jid, {
            "type": "run",
            "state": "SUCCESS",
            "updated_at": time.time() + i,
        })
        store.append_log(jid, {"level": "info", "msg": "x" * 500})
        store.append_full(jid, {"level": "opencode_output", "body": "y" * 500})


def test_prune_drops_oldest_payloads_keeps_summaries(tmp_path):
    manager = JobManager(
        str(tmp_path / ".job_store"), max_parallel_jobs=2, max_stored_jobs=5,
    )
    _seed(manager.store, 8)

    pruned = manager.prune_stored_jobs()
    assert len(pruned) == 3
    # Oldest three lost their payloads but kept their state json.
    for jid in pruned:
        assert not manager.store._get_logs_path(jid).exists()
        assert manager.store.get_job_state(jid)["state"] == "SUCCESS"
    # The newest five keep everything.
    for i in range(3, 8):
        jid = f"run_{i:03d}"
        assert manager.store._get_logs_path(jid).exists()
        assert manager.store.get_job_state(jid) is not None


def test_prune_never_touches_active_jobs(tmp_path):
    manager = JobManager(
        str(tmp_path / ".job_store"), max_parallel_jobs=2, max_stored_jobs=2,
    )
    _seed(manager.store, 4)
    manager.store.save_job_state("run_active", {
        "type": "run", "state": "RUNNING", "updated_at": 1.0,
    })
    manager.store.append_log("run_active", {"level": "info", "msg": "live"})

    pruned = manager.prune_stored_jobs()
    # 4 terminal jobs, cap 2 -> 2 pruned; the RUNNING job is untouched.
    assert len(pruned) == 2
    assert "run_active" not in pruned
    assert manager.store._get_logs_path("run_active").exists()


def test_abandoned_jobs_are_terminal_and_prunable(tmp_path):
    """ABANDONED is written by the worker daemon, so it must be terminal.

    It used to be missing from TERMINAL_JOB_STATES: abandoned records kept
    their multi-MB payloads forever, had no delete button on the board and
    were counted in no metrics bucket.
    """
    from services.jobs.job_states import (
        ATTENTION_JOB_STATES,
        TERMINAL_JOB_STATES,
    )

    assert "ABANDONED" in TERMINAL_JOB_STATES
    assert "ABANDONED" in ATTENTION_JOB_STATES

    manager = JobManager(
        str(tmp_path / ".job_store"), max_parallel_jobs=2, max_stored_jobs=1,
    )
    _seed(manager.store, 2)
    manager.store.save_job_state("run_abandoned", {
        "type": "run", "state": "ABANDONED",
    })
    manager.store.append_log("run_abandoned", {"level": "info", "msg": "x"})
    # save_job_state stamps updated_at with "now"; make this the OLDEST record
    # so the retention cap has to pick it.
    path = manager.store._get_job_path("run_abandoned")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["updated_at"] = 1.0
    path.write_text(json.dumps(record), encoding="utf-8")

    pruned = manager.prune_stored_jobs()
    assert "run_abandoned" in pruned
    assert not manager.store._get_logs_path("run_abandoned").exists()
    # The summary survives so the record stays visible in history.
    assert manager.store.get_job_state("run_abandoned")["state"] == "ABANDONED"


def test_list_jobs_skips_store_control_files(tmp_path):
    """ports.json / worker_settings.json / ui_panels.json are not jobs.

    The store is also the IPC channel for ports and settings, and the UI keeps
    its panel state there; a bare *.json glob handed all three to every scan.
    """
    store = JobStateStore(str(tmp_path))
    store.save_job_state("run_real", {"type": "run", "state": "SUCCESS"})
    for name in ("ports.json", "worker_settings.json", "ui_panels.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")

    assert store.list_jobs() == ["run_real"]


def test_enqueue_triggers_prune(tmp_path, monkeypatch):
    from services.jobs import job_manager as jm
    from supervisor.utils.config import SupervisorConfig

    class QuickLoop:
        def __init__(self, config):
            self.runner = None

        def run_streaming(self):
            yield {"level": "success", "msg": "ok"}

    monkeypatch.setattr(jm, "SupervisorLoop", QuickLoop)

    manager = JobManager(
        str(tmp_path / ".job_store"), max_parallel_jobs=2, max_stored_jobs=3,
    )
    _seed(manager.store, 6)
    ws = tmp_path / "fresh"
    ws.mkdir()
    manager.enqueue_job(
        "run",
        SupervisorConfig(protocol_path=ws / "protocol.md", workspace=ws),
    )
    remaining_payloads = [
        j for j in manager.store.list_jobs()
        if manager.store._get_logs_path(j).exists()
    ]
    # The 6 seeded terminal jobs pruned down to 3; the new job's payload
    # may still be there too.
    assert len(remaining_payloads) <= 4
    assert all(manager.store.get_job_state(j) is not None for j in manager.store.list_jobs())
