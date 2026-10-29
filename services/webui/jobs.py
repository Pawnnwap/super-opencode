"""JobManager singleton for the web UI (replaces st.cache_resource)."""

from __future__ import annotations

from services.jobs.job_manager import JobManager
from services.webui.state import app_state

_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager(
            ".job_store",
            max_parallel_jobs=int(app_state.get("max_parallel_jobs") or 4),
            max_stored_jobs=int(app_state.get("max_stored_jobs") or 50),
        )
    return _manager


def sync_max_parallel_jobs() -> None:
    """Push the persisted settings into the running manager.

    The caps are read once at manager creation; this keeps later UI edits
    effective without rebuilding the singleton (which would orphan the
    in-memory cancel handles of running jobs).
    """
    manager = get_job_manager()
    manager.set_max_parallel_jobs(int(app_state.get("max_parallel_jobs") or 4))
    manager.set_max_stored_jobs(int(app_state.get("max_stored_jobs") or 50))
