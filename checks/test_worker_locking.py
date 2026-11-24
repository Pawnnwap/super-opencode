"""Worker singleton and per-job claims must be exclusive across processes."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from services.jobs.worker_service import (
    _acquire_lock,
    _release_worker_lock,
)


@pytest.fixture(autouse=True)
def _isolated_worker_lock():
    _release_worker_lock()
    yield
    _release_worker_lock()


def test_stale_mtime_cannot_steal_a_live_worker_lock(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    assert _acquire_lock(store)
    try:
        stale = time.time() - 120
        os.utime(store / "worker.lock", (stale, stale))
        assert _acquire_lock(store) is False
    finally:
        _release_worker_lock()

    assert _acquire_lock(store) is True
    _release_worker_lock()


def test_another_process_cannot_acquire_the_worker_lock(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    assert _acquire_lock(store)
    try:
        code = (
            "from pathlib import Path; "
            "from services.jobs.worker_service import _acquire_lock; "
            f"raise SystemExit(0 if _acquire_lock(Path({str(store)!r})) else 7)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 7, result.stderr
    finally:
        _release_worker_lock()
