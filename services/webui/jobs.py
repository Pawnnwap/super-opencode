"""JobManager singleton for the web UI (replaces st.cache_resource).

Process split: the UI process gets a store-only :class:`StoreJobClient`
(enqueue/cancel/reads - no threads, no subprocesses), while the worker
daemon (``SOP_JOB_WORKER=1``, :mod:`services.jobs.worker_service`) gets the
real :class:`JobManager` that executes jobs. The job store is the IPC.
"""

from __future__ import annotations

import os

from services.webui.state import app_state

_manager = None


def get_job_manager():
    global _manager
    if _manager is None:
        store_dir = ".job_store"
        max_parallel = int(app_state.get("max_parallel_jobs") or 4)
        max_stored = int(app_state.get("max_stored_jobs") or 50)
        if os.environ.get("SOP_JOB_WORKER") == "1":
            from services.jobs.job_manager import JobManager

            _manager = JobManager(
                store_dir,
                max_parallel_jobs=max_parallel,
                max_stored_jobs=max_stored,
            )
        else:
            from services.jobs.store_client import StoreJobClient

            _manager = StoreJobClient(
                store_dir,
                max_parallel_jobs=max_parallel,
                max_stored_jobs=max_stored,
            )
    return _manager


def sync_max_parallel_jobs() -> None:
    """Push the persisted settings into the job client/daemon.

    The caps are read once at client creation; this keeps later UI edits
    effective: in the worker process it resizes the live manager, in the UI
    it lands in worker_settings.json for the daemon to apply.
    """
    manager = get_job_manager()
    manager.set_max_parallel_jobs(int(app_state.get("max_parallel_jobs") or 4))
    manager.set_max_stored_jobs(int(app_state.get("max_stored_jobs") or 50))
