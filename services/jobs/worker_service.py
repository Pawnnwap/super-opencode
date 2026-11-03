"""Worker daemon: executes queued jobs outside the UI serving process.

The UI enqueues by writing PENDING records; this daemon claims and runs them
in its own process (``SOP_JOB_WORKER=1`` makes ``get_job_manager`` hand out
the real :class:`JobManager`). Everything job-side - codex subprocess chains,
scanner storms, sidecar writes - happens here, so nothing the job machinery
does to its process can ever reach the UI's event loop again.

Single instance via ``.job_store/worker.lock``: the holder touches it every
poll; a lock older than ``_STALE_SECONDS`` belongs to a dead predecessor and
is taken over. At startup, PENDING/RUNNING orphans (the predecessor's jobs)
are marked ABANDONED, exactly like the old in-UI behaviour.

Honours cross-process cancellation: the UI writes CANCELLED, the poll loop
sees it and cancels the live in-process job (which stops the loop and kills
the codex child).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

from services.jobs.job_manager import JobManager
from services.runtime import death_capture

logger = logging.getLogger(__name__)

_SETTINGS_FILE = "worker_settings.json"
_LOCK_FILE = "worker.lock"
_POLL_SECONDS = 1.0
_STALE_SECONDS = 15.0


def _read_settings(store_dir: Path) -> dict:
    try:
        data = json.loads((store_dir / _SETTINGS_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _acquire_lock(store_dir: Path, now: float | None = None) -> bool:
    """Claim the single-instance lock; refuse only a FRESH live holder.

    Liveness is judged by the lock file's mtime (the holder touches it every
    poll), so a crashed daemon's lock is recycled without PID checks.
    """
    lock_path = store_dir / _LOCK_FILE
    now = time.time() if now is None else now
    if lock_path.exists():
        age = now - lock_path.stat().st_mtime
        if age < _STALE_SECONDS:
            return False
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "ts": now}), encoding="utf-8",
    )
    return True


def _touch_lock(store_dir: Path) -> None:
    lock_path = store_dir / _LOCK_FILE
    try:
        os.utime(lock_path)
    except OSError:
        lock_path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")


def _mark_orphans(manager: JobManager) -> list[str]:
    """Flag PENDING/RUNNING records as abandoned at daemon start."""
    flagged: list[str] = []
    for job_id in manager.store.list_jobs():
        state = manager.store.get_job_state(job_id) or {}
        if state.get("state") in ("PENDING", "RUNNING"):
            state["state"] = "ABANDONED"
            manager.store.save_job_state(job_id, state)
            manager.store.append_log(job_id, {
                "level": "warn",
                "msg": "Worker daemon restarted while this job was active - "
                       "marked ABANDONED (re-enqueue via the board or "
                       "SUPERVISOR_REPLAY_JOB=<job_id>).",
            })
            flagged.append(job_id)
    return flagged


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("JOB_WORKER_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # Silent-death forensics (2026-11-03: the daemon vanished with its three
    # RUNNING jobs mid-stream and left no trace) - same capture as app.py.
    death_capture.install("job_worker.fatal.log")
    store_dir = Path(".job_store")
    store_dir.mkdir(parents=True, exist_ok=True)

    settings = _read_settings(store_dir)
    manager = JobManager(
        store_dir=str(store_dir),
        max_parallel_jobs=int(settings.get("max_parallel_jobs") or 4),
        max_stored_jobs=int(settings.get("max_stored_jobs") or 50),
    )
    if not _acquire_lock(store_dir):
        logger.info("another worker daemon holds the lock - exiting")
        return 0

    orphaned = _mark_orphans(manager)
    logger.info(
        "worker daemon started (pid %d, %d orphaned job(s) marked ABANDONED)",
        os.getpid(), len(orphaned),
    )

    claimed: set[str] = set()
    applied_settings: dict = dict(settings)
    while True:
        time.sleep(_POLL_SECONDS)
        try:
            _touch_lock(store_dir)

            # Settings handoff: the UI writes worker_settings.json on change.
            settings = _read_settings(store_dir)
            if settings != applied_settings:
                if "max_parallel_jobs" in settings:
                    manager.set_max_parallel_jobs(
                        int(settings.get("max_parallel_jobs") or 4),
                    )
                if "max_stored_jobs" in settings:
                    manager.set_max_stored_jobs(
                        int(settings.get("max_stored_jobs") or 50),
                    )
                applied_settings = dict(settings)
                logger.info("applied settings: %s", applied_settings)

            for job_id in manager.store.list_jobs():
                if job_id in claimed:
                    # Claimed jobs are never re-claimed, but a cross-process
                    # cancel can arrive at any moment: the sentinel file (not
                    # the state field - the worker heartbeat rewrites it) is
                    # the durable request, honoured here AND by the worker
                    # itself, so even a worker parked in a silent stream get
                    # gets its codex tree killed within one poll cycle.
                    if manager.cancel_sentinel_exists(job_id):
                        if manager.cancel_if_live(job_id):
                            logger.info("honoured cancel request for %s", job_id)
                    continue
                state = manager.store.get_job_state(job_id) or {}
                st = state.get("state")
                if st == "PENDING":
                    if manager.run_stored_job(job_id):
                        claimed.add(job_id)
                        logger.info("claimed PENDING job %s", job_id)
                elif st == "CANCELLED":
                    # A UI-side cancel request for a job this daemon runs.
                    if manager.cancel_if_live(job_id):
                        logger.info("honoured CANCELLED request for %s", job_id)
            manager.prune_stored_jobs()
        except Exception:
            logger.exception("worker daemon poll cycle failed")


if __name__ == "__main__":
    raise SystemExit(main())
