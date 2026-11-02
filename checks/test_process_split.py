"""Process split: UI enqueues through the store, daemon claims and runs.

Two serving deaths (2026-11-01 IOCP, 2026-11-02 selector) happened in the
shared UI+jobs process. Contract under test:

- StoreJobClient (UI side) only writes store records: enqueue -> PENDING,
  cancel -> CANCELLED (terminal jobs untouched), settings file written;
- JobManager.run_stored_job (daemon side) claims a PENDING record and
  executes it in-process, preserving batch metadata;
- a UI-side CANCELLED on a live job is honoured by the daemon manager
  (cancel_if_live -> request_stop) without touching foreign jobs.
"""

import os
import threading
import time
from pathlib import Path

import pytest

from services.jobs import job_manager as jm
from services.jobs.job_manager import JobManager
from services.jobs.store_client import StoreJobClient
from services.jobs.worker_service import _acquire_lock, _mark_orphans
from supervisor.utils.config import SupervisorConfig


class StubLoop:
    release = threading.Event()
    stop_flag = threading.Event()

    def __init__(self, config):
        self.config = config
        self.runner = None

    def request_stop(self) -> None:
        self.stop_flag.set()

    def run_streaming(self):
        yield {"level": "info", "msg": "stub start"}
        while not self.stop_flag.is_set():
            if self.release.is_set():
                yield {"level": "success", "msg": "stub done"}
                return
            time.sleep(0.02)
            yield {"level": "info", "msg": "ping"}


@pytest.fixture(autouse=True)
def stub_loops(monkeypatch):
    StubLoop.release.clear()
    StubLoop.stop_flag.clear()
    monkeypatch.setattr(jm, "SupervisorLoop", StubLoop)
    monkeypatch.setattr(jm, "SelfEvolutionLoop", StubLoop)


def _config(ws: Path) -> SupervisorConfig:
    ws.mkdir(parents=True, exist_ok=True)
    protocol = ws / "protocol.md"
    protocol.write_text("# PROTOCOL\n\n## TARGET\n\nx\n", encoding="utf-8")
    return SupervisorConfig(protocol_path=protocol, workspace=ws)


def _make(tmp_path: Path):
    store_dir = tmp_path / "store"
    client = StoreJobClient(store_dir=str(store_dir))
    manager = JobManager(store_dir=str(store_dir), max_parallel_jobs=2)
    return client, manager


def test_ui_enqueue_writes_pending_record_only(tmp_path):
    client, manager = _make(tmp_path)
    job_id = client.enqueue_job("run", _config(tmp_path / "ws_a"))
    state = manager.store.get_job_state(job_id)
    assert state["state"] == "PENDING"
    assert state["config"]["workspace"]
    # The UI process spawned no worker: nothing is live in any manager.
    assert manager._active_jobs == {}


def test_ui_enqueue_rejects_active_workspace_collision(tmp_path):
    client, _ = _make(tmp_path)
    ws = tmp_path / "ws_a"
    client.enqueue_job("run", _config(ws))
    with pytest.raises(ValueError, match="already active"):
        client.enqueue_job("run", _config(ws))


def test_ui_cancel_writes_state_and_spares_terminal_jobs(tmp_path):
    client, manager = _make(tmp_path)
    job_id = client.enqueue_job("run", _config(tmp_path / "ws_a"))
    client.cancel_job(job_id)
    assert manager.store.get_job_state(job_id)["state"] == "CANCELLED"

    manager.store.save_job_state(job_id, {
        **(manager.store.get_job_state(job_id) or {}), "state": "SUCCESS",
    })
    client.cancel_job(job_id)
    assert manager.store.get_job_state(job_id)["state"] == "SUCCESS"


def test_daemon_claims_pending_and_runs_it(tmp_path):
    client, manager = _make(tmp_path)
    job_id = client.enqueue_job(
        "run", _config(tmp_path / "ws_a"),
        meta={"batch_id": "b1", "replica_index": 1, "replica_count": 2},
    )
    assert manager.run_stored_job(job_id)
    deadline = time.time() + 10
    while time.time() < deadline:
        state = manager.store.get_job_state(job_id) or {}
        if state.get("state") == "RUNNING":
            break
        time.sleep(0.05)
    assert state["state"] == "RUNNING"
    assert state["batch_id"] == "b1"
    StubLoop.release.set()
    deadline = time.time() + 10
    while time.time() < deadline:
        state = manager.store.get_job_state(job_id) or {}
        if state.get("state") == "SUCCESS":
            break
        time.sleep(0.05)
    assert state["state"] == "SUCCESS"
    assert not manager.run_stored_job(job_id)  # terminal: not claimable


def test_daemon_honours_ui_cancel_of_live_job(tmp_path):
    client, manager = _make(tmp_path)
    job_id = client.enqueue_job("run", _config(tmp_path / "ws_a"))
    manager.run_stored_job(job_id)
    deadline = time.time() + 10
    while time.time() < deadline:
        if (manager.store.get_job_state(job_id) or {}).get("state") == "RUNNING":
            break
        time.sleep(0.05)
    client.cancel_job(job_id)
    assert manager.cancel_if_live(job_id)
    assert StubLoop.stop_flag.is_set()
    # Foreign/unknown ids are ignored, not fatal.
    assert not manager.cancel_if_live("run_unknown00")
    StubLoop.release.set()


def test_lock_is_single_and_stale_locks_are_recycled(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    now = time.time()
    assert _acquire_lock(store, now=now)
    assert not _acquire_lock(store, now=now + 1)      # fresh holder
    stale = (now - 60, now - 60)
    os.utime(store / "worker.lock", stale)
    assert _acquire_lock(store, now=now)               # stale -> recycled


def test_daemon_start_marks_orphans_abandoned(tmp_path):
    _, manager = _make(tmp_path)
    ws = tmp_path / "ws_a"
    stale = manager.enqueue_job("run", _config(ws))
    manager.store.save_job_state(stale, {
        **(manager.store.get_job_state(stale) or {}), "state": "RUNNING",
    })
    flagged = _mark_orphans(manager)
    assert flagged == [stale]
    assert manager.store.get_job_state(stale)["state"] == "ABANDONED"


def test_ui_process_factory_hands_out_store_client(monkeypatch, tmp_path):
    import services.webui.jobs as webui_jobs

    monkeypatch.setattr(webui_jobs, "_manager", None)
    monkeypatch.delenv("SOP_JOB_WORKER", raising=False)
    assert isinstance(webui_jobs.get_job_manager(), StoreJobClient)
    monkeypatch.setenv("SOP_JOB_WORKER", "1")
    monkeypatch.setattr(webui_jobs, "_manager", None)
    assert isinstance(webui_jobs.get_job_manager(), JobManager)
    monkeypatch.setattr(webui_jobs, "_manager", None)
