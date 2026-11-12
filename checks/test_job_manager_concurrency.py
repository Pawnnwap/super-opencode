"""JobManager concurrency cap + study-batch metadata checks.

The cap answers a real failure mode: unbounded one-thread-per-job spawning,
where an N-instance parallel study also meant N simultaneous CLI subprocess
chains. Contract under test:

- at most ``max_parallel_jobs`` jobs are RUNNING at once; extras park in
  PENDING and start as slots free;
- the same-workspace exclusivity guard still rejects a second active job;
- batch metadata (batch_id / replica_index / replica_count) survives the
  worker's state writes;
- ``cancel_batch`` takes down both RUNNING and still-parked PENDING
  replicas, and a cancelled-while-parked job never starts running.
"""

import threading
import time
from pathlib import Path

import pytest

from services.jobs import job_manager as jm
from services.jobs.job_manager import JobManager, claim_order
from services.jobs.store_client import StoreJobClient
from supervisor.utils.config import SupervisorConfig


class StubLoop:
    """Fake loop: yields one event, then pings until released or stopped."""

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


def _config(ws: Path) -> SupervisorConfig:
    return SupervisorConfig(protocol_path=ws / "protocol.md", workspace=ws)


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _states(manager: JobManager) -> dict[str, str]:
    return {
        jid: (manager.store.get_job_state(jid) or {}).get("state", "?")
        for jid in manager.store.list_jobs()
    }


def test_cap_limits_running_jobs(tmp_path):
    manager = JobManager(str(tmp_path / ".job_store"), max_parallel_jobs=2)
    ids = []
    for i in range(4):
        ws = tmp_path / f"ws{i}"
        ws.mkdir()
        ids.append(manager.enqueue_job("run", _config(ws)))

    assert _wait_for(lambda: sum(
        1 for s in _states(manager).values() if s == "RUNNING"
    ) == 2)
    states = _states(manager)
    assert sum(1 for s in states.values() if s == "PENDING") == 2

    # Free one slot; exactly one parked job must move to RUNNING.
    StubLoop.release.set()
    assert _wait_for(lambda: sum(
        1 for s in _states(manager).values() if s in ("SUCCESS", "FAILED")
    ) == 4), _states(manager)
    # All four ran to a terminal state — the counter never leaked.
    final = _states(manager)
    assert set(final.values()) == {"SUCCESS"}


def test_same_workspace_guard_still_rejects(tmp_path):
    manager = JobManager(str(tmp_path / ".job_store"), max_parallel_jobs=4)
    ws = tmp_path / "ws"
    ws.mkdir()
    manager.enqueue_job("run", _config(ws))
    with pytest.raises(ValueError, match="already active in this workspace"):
        manager.enqueue_job("run", _config(ws))


def test_batch_metadata_survives_state_writes(tmp_path):
    manager = JobManager(str(tmp_path / ".job_store"), max_parallel_jobs=1)
    for i in (1, 2):
        ws = tmp_path / f"r{i}"
        ws.mkdir()
        manager.enqueue_job(
            "run", _config(ws),
            meta={"batch_id": "study_t", "replica_index": i, "replica_count": 2},
        )
    StubLoop.release.set()
    assert _wait_for(lambda: len([
        j for j, s in _states(manager).items() if s == "SUCCESS"
    ]) == 2)
    batch = manager.list_batch_jobs("study_t")
    assert [b["replica_index"] for b in batch] == [1, 2]
    assert all(b.get("replica_count") == 2 for b in batch)


def test_cancel_batch_kills_running_and_pending(tmp_path):
    manager = JobManager(str(tmp_path / ".job_store"), max_parallel_jobs=1)
    ids = []
    for i in (1, 2, 3):
        ws = tmp_path / f"r{i}"
        ws.mkdir()
        ids.append(manager.enqueue_job(
            "run", _config(ws),
            meta={"batch_id": "study_c", "replica_index": i, "replica_count": 3},
        ))
    # r1 RUNNING, r2/r3 parked under the cap=1.
    assert _wait_for(lambda: _states(manager).get(ids[0]) == "RUNNING")
    assert _states(manager)[ids[1]] == "PENDING"

    cancelled = manager.cancel_batch("study_c")
    assert set(cancelled) == set(ids)
    assert _wait_for(lambda: all(
        (manager.store.get_job_state(j) or {}).get("state") == "CANCELLED"
        for j in ids
    )), _states(manager)
    # The parked replicas never started: no stub start event was logged.
    for j in ids[1:]:
        levels = [e.get("level") for e in manager.store.get_logs(j)]
        assert "info" not in levels


def test_manager_headline_mode_applies_to_job_logs(tmp_path):
    manager = JobManager(str(tmp_path / ".job_store"), max_parallel_jobs=2)

    class BigLoop(StubLoop):
        def run_streaming(self):
            yield {"level": "opencode_output", "msg": "line\n" + "x" * 20_000}
            yield {"level": "success", "msg": "done"}

    jm.SupervisorLoop = BigLoop
    try:
        ws = tmp_path / "ws"
        ws.mkdir()
        jid = manager.enqueue_job("run", _config(ws))
        assert _wait_for(lambda: (
            manager.store.get_job_state(jid) or {}
        ).get("state") == "SUCCESS")
        logs = manager.store.get_logs(jid)
        output_events = [e for e in logs if e.get("level") == "opencode_output"]
        assert len(output_events) == 1
        # Headline stays headline-sized even for a pathological single
        # 20k-char line: a few hundred chars, never the body.
        assert len(output_events[0]["msg"]) < 800
        assert output_events[0].get("ref")
        detail = manager.get_event_detail(jid, output_events[0]["ref"])
        assert detail["body"].endswith("x" * 20_000)
    finally:
        jm.SupervisorLoop = StubLoop


def test_cancel_if_live_is_idempotent_while_the_job_drains(tmp_path):
    """The poll must not re-cancel a job that is already stopping.

    Observed on run_152ccb68: "Job cancelled by user." was appended every
    poll for the ~60 s the codex turn took to die, because the worker kept
    calling cancel_if_live on a job whose stop flag was already set.
    """
    manager = JobManager(str(tmp_path / ".job_store"), max_parallel_jobs=1)
    stop = threading.Event()
    manager._active_jobs["job_x"] = {"stop_event": stop, "loop": None}
    assert manager.cancel_if_live("job_x") is True
    assert stop.is_set()
    assert manager.cancel_if_live("job_x") is False
    assert manager.cancel_if_live("job_missing") is False


def test_claim_order_is_fifo_and_batch_ordered():
    # Enqueue time first: an older record wins regardless of its hash id.
    old = {"created_at": 100.0, "batch_id": "b", "replica_index": 2}
    new = {"created_at": 200.0, "batch_id": "b", "replica_index": 1}
    assert claim_order(old, "run_z") < claim_order(new, "run_a")
    # Replicas of one batch share created_at; replica_index breaks the tie.
    r2 = {"created_at": 100.0, "batch_id": "b", "replica_index": 2}
    r1 = {"created_at": 100.0, "batch_id": "b", "replica_index": 1}
    assert claim_order(r1, "run_x") < claim_order(r2, "run_y")
    # Legacy records without created_at fall back to the enqueue heartbeat.
    assert claim_order({"heartbeat_at": 50.0}, "run_a") < claim_order({"heartbeat_at": 60.0}, "run_b")


def test_claim_pending_fills_free_slots_oldest_first(tmp_path, stub_loops, monkeypatch):
    """5 queued, 2 slots: 2 threads (FIFO), the other 3 store-only."""
    monkeypatch.setattr(jm, "codex_engine_ready", lambda explicit="": (True, ""))
    from services.jobs.worker_service import _claim_pending

    store_dir = tmp_path / "store"
    client = StoreJobClient(str(store_dir))
    manager = JobManager(str(store_dir), max_parallel_jobs=2)
    ids = []
    for i in range(5):
        jid = client.enqueue_job("run", _config(tmp_path / f"ws{i}"))
        state = manager.store.get_job_state(jid)
        state["created_at"] = 1000.0 + i  # deterministic enqueue order
        manager.store.save_job_state(jid, state)
        ids.append(jid)

    # Order the records deliberately reversed to prove the sort does the work.
    pending = [
        (claim_order(manager.store.get_job_state(jid), jid), jid)
        for jid in reversed(ids)
    ]
    claimed: set[str] = set()
    assert _claim_pending(manager, pending, claimed) == ids[:2]
    assert claimed == set(ids[:2])
    assert manager.available_slots() == 0
    assert manager.active_job_count() == 2  # no parked threads
    assert not manager.run_stored_job(ids[2])  # full: stays PENDING
    assert _states(manager)[ids[2]] == "PENDING"

    # Finishing jobs frees the slots; the counter never leaks.
    StubLoop.release.set()
    assert _wait_for(lambda: sum(
        1 for s in _states(manager).values() if s == "SUCCESS"
    ) == 2)
    assert _wait_for(lambda: manager.available_slots() == 2)
