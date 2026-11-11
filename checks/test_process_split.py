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
    """Per-test StubLoop subclass with FRESH end-signal events.

    StubLoop's class-level release/stop_flag events leak across tests: a
    worker thread still terminating from test N can miss its end signal
    (the fixture used to clear it) and later call ``_request_loop_stop()``
    - which sets the shared stop_flag - inside test N+1, ending that
    test's job stream instantly. That race wrote state FAILED 14 ms after
    job start and lost-update-clobbered _mark_orphans' ABANDONED write
    (flake, 2026-11-03). Fresh events per test keep zombies talking to
    their own flags, and an end signal is never erased before it is seen.
    """
    loops = type("StubLoop", (StubLoop,), {
        "release": threading.Event(),
        "stop_flag": threading.Event(),
    })
    monkeypatch.setattr(jm, "SupervisorLoop", loops)
    monkeypatch.setattr(jm, "SelfEvolutionLoop", loops)
    return loops


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
    assert state.get("created_at")  # FIFO claim key, shared by both paths
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


def test_daemon_claims_pending_and_runs_it(tmp_path, stub_loops):
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
    stub_loops.release.set()
    deadline = time.time() + 10
    while time.time() < deadline:
        state = manager.store.get_job_state(job_id) or {}
        if state.get("state") == "SUCCESS":
            break
        time.sleep(0.05)
    assert state["state"] == "SUCCESS"
    assert not manager.run_stored_job(job_id)  # terminal: not claimable


def test_daemon_honours_ui_cancel_of_live_job(tmp_path, stub_loops):
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
    assert stub_loops.stop_flag.is_set()
    # Foreign/unknown ids are ignored, not fatal.
    assert not manager.cancel_if_live("run_unknown00")
    stub_loops.release.set()


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
    # Write the stale RUNNING record directly: enqueue_job spawns a live
    # worker thread whose state writes race _mark_orphans' write and can
    # clobber ABANDONED (last-writer-wins lost update - the flake this
    # test kept hitting). The contract under test is _mark_orphans only.
    stale = "run_stale00000001"
    manager.store.save_job_state(stale, {
        "type": "run", "state": "RUNNING", "progress": 0.0,
        "heartbeat_at": time.time(), "config": {},
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


def test_cancel_sentinel_survives_heartbeat_state_rewrite(tmp_path):
    """The 2026-11-02 batch-cancel miss: the UI's CANCELLED state write was
    resurrected to RUNNING by the worker's own heartbeat, and the daemon
    never re-read claimed jobs - three replicas kept burning provider turns.
    The sentinel file is the intent; state writes must not erase it."""
    client, manager = _make(tmp_path)
    job_id = client.enqueue_job("run", _config(tmp_path / "ws_a"))
    client.cancel_job(job_id)
    # Heartbeat-shaped full-state write lands AFTER the cancel...
    state = manager.store.get_job_state(job_id) or {}
    state["state"] = "RUNNING"
    manager.store.save_job_state(job_id, state)
    # ...the intent survives and the daemon-side check still sees it.
    assert manager.cancel_sentinel_exists(job_id)


def test_daemon_honours_cancel_of_claimed_running_job(tmp_path, stub_loops):
    """The poll-loop branch that was skipped for claimed jobs: a sentinel on
    a live, claimed job must reach cancel_if_live and stop the loop."""
    client, manager = _make(tmp_path)
    job_id = client.enqueue_job("run", _config(tmp_path / "ws_a"))
    # Register as a claimed/live job without a streaming worker, so the
    # worker-side sentinel consumer cannot win the race this test targets.
    loop = stub_loops(None)
    stop_event = threading.Event()
    manager._active_jobs[job_id] = {"stop_event": stop_event, "loop": loop}
    (manager.store.store_dir / f"{job_id}.cancel").write_text("x", encoding="utf-8")

    # The exact check the daemon poll runs for every claimed job.
    assert manager.cancel_sentinel_exists(job_id)
    assert manager.cancel_if_live(job_id)
    assert loop.stop_flag.is_set()
    # The worker's own cleanup path removes the consumed sentinel.
    stop_event.set()
    sentinel = manager.store.store_dir / f"{job_id}.cancel"
    sentinel.unlink(missing_ok=True)


def test_worker_exits_cancelled_when_sentinel_drops_mid_stream(tmp_path):
    client, manager = _make(tmp_path)
    job_id = client.enqueue_job("run", _config(tmp_path / "ws_a"))
    manager.run_stored_job(job_id)
    deadline = time.time() + 10
    while time.time() < deadline:
        if (manager.store.get_job_state(job_id) or {}).get("state") == "RUNNING":
            break
        time.sleep(0.05)
    client.cancel_job(job_id)
    deadline = time.time() + 10
    while time.time() < deadline:
        state = manager.store.get_job_state(job_id) or {}
        if state.get("state") == "CANCELLED" and not manager.cancel_sentinel_exists(job_id):
            break
        time.sleep(0.05)
    assert state.get("state") == "CANCELLED"
    assert not manager.cancel_sentinel_exists(job_id)  # consumed, not stale


def test_sentinel_marked_pending_job_is_never_claimed(tmp_path):
    client, manager = _make(tmp_path)
    job_id = client.enqueue_job("run", _config(tmp_path / "ws_a"))
    client.cancel_job(job_id)
    assert not manager.run_stored_job(job_id)
    assert (manager.store.get_job_state(job_id) or {}).get("state") == "CANCELLED"
