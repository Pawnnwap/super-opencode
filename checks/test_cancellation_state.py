"""Cancellation is durable, monotonic, and retains ownership while draining."""

from __future__ import annotations

import os
import threading

import pytest

from services.jobs import job_manager as jm
from services.jobs.file_lock import FileLock
from services.jobs.job_manager import JobManager
from services.jobs.store_client import StoreJobClient
from supervisor.utils.config import SupervisorConfig


class _BlockingLoop:
    release = threading.Event()
    stopped = threading.Event()

    def __init__(self, _config):
        self.runner = None

    def request_stop(self):
        self.stopped.set()

    def run_streaming(self):
        yield {"level": "info", "msg": "started"}
        while not self.release.wait(0.02):
            yield {"level": "info", "msg": "working"}


@pytest.fixture(autouse=True)
def _stub_loop(monkeypatch):
    _BlockingLoop.release.clear()
    _BlockingLoop.stopped.clear()
    monkeypatch.setattr(jm, "SupervisorLoop", _BlockingLoop)
    monkeypatch.setattr(jm, "codex_engine_ready", lambda _explicit="": (True, ""))


def _config(workspace):
    return SupervisorConfig(
        protocol_path=workspace / "protocol.md", workspace=workspace
    )


def test_cancel_sentinel_failure_does_not_report_cancelled(tmp_path, monkeypatch):
    client = StoreJobClient(str(tmp_path / "store"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    job_id = client.enqueue_job("run", _config(workspace))
    original_replace = os.replace

    def _replace(src, dst):
        if str(dst).endswith(".cancel"):
            raise OSError("disk full")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", _replace)

    assert client.cancel_job(job_id) is False
    assert client.store.get_job_state(job_id)["state"] == "PENDING"
    assert not (client.store.store_dir / f"{job_id}.cancel").exists()


def test_stopping_retains_workspace_until_worker_finishes(tmp_path):
    client = StoreJobClient(str(tmp_path / "store"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    job_id = client.enqueue_job("run", _config(workspace))
    state = client.store.get_job_state(job_id)
    state["state"] = "STOPPING"
    client.store.save_job_state(job_id, state)

    with pytest.raises(ValueError, match="already active"):
        client.enqueue_job("run", _config(workspace))

    state["state"] = "CANCELLED"
    client.store.save_job_state(job_id, state)
    client.enqueue_job("run", _config(workspace))


def test_claim_after_stopping_finalizes_cancelled(tmp_path):
    client = StoreJobClient(str(tmp_path / "store"))
    manager = JobManager(str(client.store.store_dir), max_parallel_jobs=1)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    job_id = client.enqueue_job("run", _config(workspace))
    claim = FileLock(client.store.store_dir / f"{job_id}.claim")
    assert claim.acquire()
    try:
        assert client.cancel_job(job_id) is True
        assert client.store.get_job_state(job_id)["state"] == "STOPPING"
    finally:
        claim.release()

    assert manager.run_stored_job(job_id) is False
    assert manager.store.get_job_state(job_id)["state"] == "CANCELLED"


def test_delete_refuses_a_job_that_is_still_stopping(tmp_path):
    client = StoreJobClient(str(tmp_path / "store"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    job_id = client.enqueue_job("run", _config(workspace))
    state = client.store.get_job_state(job_id)
    state["state"] = "STOPPING"
    client.store.save_job_state(job_id, state)

    with pytest.raises(ValueError, match="not terminal"):
        client.store.delete_job(job_id)
    assert client.store.get_job_state(job_id)["state"] == "STOPPING"
