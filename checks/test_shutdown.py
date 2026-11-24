"""Normal shutdown drains active work and flushes UI state."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_worker_shutdown_cancels_active_jobs_and_sets_deadline():
    from services.jobs.worker_service import _begin_shutdown

    cancelled = []
    manager = SimpleNamespace(
        _active_jobs={"run_a": {}, "run_b": {}},
        cancel_job=lambda job_id: cancelled.append(job_id),
    )
    pulse = SimpleNamespace(shutdown_requested=threading.Event(), exit_requested=False)
    pulse.shutdown_requested.set()

    deadline = _begin_shutdown(manager, pulse, now=100.0)

    assert cancelled == ["run_a", "run_b"]
    assert deadline == pytest.approx(130.0)


def test_app_shutdown_flushes_pending_settings():
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text(
        encoding="utf-8"
    )
    assert "@app.on_shutdown" in source
    assert "app_state.flush()" in source


def test_worker_loop_does_not_claim_after_shutdown_starts():
    source = (
        Path(__file__).resolve().parents[1] / "services" / "jobs" / "worker_service.py"
    ).read_text(
        encoding="utf-8",
    )
    assert "_begin_shutdown(manager, pulse" in source
    assert "if shutdown_deadline is not None:" in source
    assert "continue" in source
