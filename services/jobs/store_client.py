"""Store-backed job client for the UI process.

The UI never executes jobs: enqueueing writes a PENDING record that the
worker daemon (:mod:`services.jobs.worker_service`, ``SOP_JOB_WORKER=1``)
claims and runs in its own process. Codex turns, scanner subprocess storms
and every other job-side side effect stay out of the serving process - two
serving deaths (2026-11-01 under IOCP, 2026-11-02 under the selector loop)
happened in the old shared process while the worker threads themselves kept
running. The job store is the IPC: state files already carry config, logs,
heartbeat and report.

This class mirrors the JobManager surface the UI touches (enqueue, cancel,
reads, settings handoff); anything that would spawn threads belongs to the
daemon's JobManager, not here.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from services.jobs.job_manager import (
    JobManager,
    assert_workspace_free,
    pending_record,
)
from services.jobs.job_states import (
    ACTIVE_JOB_STATES,
    JOB_STORE_DIR,
    WORKER_SETTINGS_FILE,
)
from services.jobs.state_store import JobStateStore


class StoreJobClient:
    """UI-side JobManager surface, backed purely by the job store."""

    TERMINAL_STATES = JobManager.TERMINAL_STATES

    def __init__(
        self,
        store_dir: str = JOB_STORE_DIR,
        max_parallel_jobs: int = 4,
        max_stored_jobs: int = 50,
    ):
        self.store = JobStateStore(store_dir)
        self._lock = threading.Lock()
        self._max_parallel = max(1, int(max_parallel_jobs))
        self._max_stored = max(0, int(max_stored_jobs))
        self._write_settings()

    # ------------------------------------------------- settings handoff -- #

    def _write_settings(self) -> None:
        payload = {
            "max_parallel_jobs": self._max_parallel,
            "max_stored_jobs": self._max_stored,
        }
        self.store.store_dir.mkdir(parents=True, exist_ok=True)
        (self.store.store_dir / WORKER_SETTINGS_FILE).write_text(
            json.dumps(payload), encoding="utf-8",
        )

    def set_max_parallel_jobs(self, value: int) -> None:
        with self._lock:
            self._max_parallel = max(1, int(value))
            self._write_settings()

    def set_max_stored_jobs(self, value: int) -> None:
        with self._lock:
            self._max_stored = max(0, int(value))
            self._write_settings()

    # ---------------------------------------------------- enqueue/cancel -- #

    def enqueue_job(
        self,
        job_type: str,
        config,
        meta: dict[str, Any] | None = None,
    ) -> str:
        """Write a PENDING record; the worker daemon claims and runs it."""
        job_id = f"{job_type}_{uuid.uuid4().hex[:8]}"
        record, _ = pending_record(job_type, config, meta, time.time())

        with self._lock:
            assert_workspace_free(self.store, Path(config.workspace).resolve())
            self.store.save_job_state(job_id, record)
        return job_id

    def cancel_job(self, job_id: str) -> None:
        """Request cancellation by writing state; the daemon honours it.

        The sentinel file carries the durable intent: the daemon worker
        rewrites the state field (RUNNING heartbeat) every few seconds, so
        the request must not live only there. Terminal jobs are left
        untouched - rewriting a finished job's state would corrupt history.
        """
        state = self.store.get_job_state(job_id) or {}
        if state.get("state") not in ACTIVE_JOB_STATES:
            return
        sentinel = self.store.store_dir / f"{job_id}.cancel"
        try:
            sentinel.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass
        self.store.append_log(job_id, {"level": "warn", "msg": "Job cancelled by user."})
        state["state"] = "CANCELLED"
        state["heartbeat_at"] = time.time()
        self.store.save_job_state(job_id, state)

    def cancel_batch(self, batch_id: str) -> list[str]:
        """Cancel every still-active job of a batch. Returns cancelled ids."""
        cancelled: list[str] = []
        for jid in self.store.list_jobs():
            state = self.store.get_job_state(jid) or {}
            if state.get("batch_id") == batch_id and state.get("state") in ACTIVE_JOB_STATES:
                self.cancel_job(jid)
                cancelled.append(jid)
        return cancelled

    # ----------------------------------------------------------- reads -- #

    def get_job_status(
        self, job_id: str, max_logs: int | None = None,
    ) -> dict[str, Any] | None:
        return self.store.read_job_status(job_id, max_logs)

    def get_job_summary(self, job_id: str) -> dict[str, Any] | None:
        return self.store.get_job_state(job_id)

    def get_event_detail(self, job_id: str, ref: int) -> dict[str, Any] | None:
        return self.store.read_full(job_id, ref)

    def list_batch_jobs(self, batch_id: str) -> list[dict[str, Any]]:
        return self.store.list_batch_jobs(batch_id)
