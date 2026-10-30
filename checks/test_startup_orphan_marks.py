"""Startup orphan-marking checks: non-terminal states of dead processes.

At app startup no worker thread exists yet, so every PENDING/RUNNING entry
in the store belongs to a previous (dead) process — typically a restart
mid-run. They must be flagged ABANDONED, not left as ghost queue entries;
terminal states are untouched and replay stays possible.
"""

import pytest

from services.jobs import job_manager as jm
from services.jobs.job_manager import JobManager
from services.webui.startup import mark_abandoned_jobs


@pytest.fixture(autouse=True)
def _stub_loops(monkeypatch):
    class Noop:
        def __init__(self, config):
            self.runner = None

        def run_streaming(self):
            yield {"level": "success", "msg": "ok"}
            yield {"level": "info", "msg": "unreachable"}

    monkeypatch.setattr(jm, "SupervisorLoop", Noop)
    monkeypatch.setattr(jm, "SelfEvolutionLoop", Noop)


def _manager(tmp_path):
    return JobManager(str(tmp_path / ".job_store"), max_parallel_jobs=2)


def test_active_states_marked_abandoned(tmp_path):
    manager = _manager(tmp_path)
    store = manager.store
    store.save_job_state("run_a", {"type": "run", "state": "RUNNING"})
    store.save_job_state("run_b", {"type": "run", "state": "PENDING"})
    store.save_job_state("run_c", {"type": "run", "state": "SUCCESS"})
    store.save_job_state("run_d", {"type": "run", "state": "FAILED"})

    summary = mark_abandoned_jobs(manager)
    assert "2 abandoned" in summary
    assert store.get_job_state("run_a")["state"] == "ABANDONED"
    assert store.get_job_state("run_b")["state"] == "ABANDONED"
    assert store.get_job_state("run_c")["state"] == "SUCCESS"
    assert store.get_job_state("run_d")["state"] == "FAILED"
    # A log breadcrumb explains the marker and how to replay.
    logs = store.get_logs("run_a")
    assert any("ABANDONED" in (e.get("msg") or "") for e in logs)


def test_no_active_states_is_a_noop(tmp_path):
    manager = _manager(tmp_path)
    manager.store.save_job_state("run_ok", {"type": "run", "state": "SUCCESS"})
    assert "no abandoned" in mark_abandoned_jobs(manager)
    assert manager.store.get_job_state("run_ok")["state"] == "SUCCESS"
