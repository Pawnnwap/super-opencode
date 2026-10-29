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
from services.jobs.job_manager import JobManager
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
    monkeypatch.setattr(jm, "SelfEvolutionLoop", StubLoop)


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
