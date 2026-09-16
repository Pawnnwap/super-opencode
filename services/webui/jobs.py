"""JobManager singleton for the web UI (replaces st.cache_resource)."""

from __future__ import annotations

from services.jobs.job_manager import JobManager

_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager(".job_store")
    return _manager
