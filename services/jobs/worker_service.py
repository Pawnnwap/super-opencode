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

from services.config.settings import sanitize_proxy_env
from services.jobs.code_freshness import code_fingerprint, freshness_action
from services.jobs.job_manager import JobManager
from services.jobs.job_states import JOB_STORE_DIR, WORKER_SETTINGS_FILE
from services.runtime import death_capture

logger = logging.getLogger(__name__)

# Captured at import time, not in main(): the daemon executes the modules
# loaded here, so a file edited between import and main() must read as a
# change to reload, not be baked in as "loaded".
_LOADED_CODE_FINGERPRINT = code_fingerprint()

_LOCK_FILE = "worker.lock"
_POLL_SECONDS = 1.0
_STALE_SECONDS = 15.0
# How often the daemon compares its imported code with the tree on disk.
# Content hashing ~1.3 MB is milliseconds; a deployment must not wait for a
# natural worker restart (one stayed stale for 20 hours).
_CODE_CHECK_SECONDS = 10.0


def _read_settings(store_dir: Path) -> dict:
    try:
        data = json.loads((store_dir / WORKER_SETTINGS_FILE).read_text(encoding="utf-8"))
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


def _reexec_worker() -> None:
    """Replace this process image so a fresh interpreter imports the new code.

    Only called when no job is live. The PID's lock is retained (the lock is
    an mtime-touched file, not an OS handle) and the next boot skips the
    acquire step via SOP_JOB_WORKER_REEXEC.
    """
    os.environ["SOP_JOB_WORKER_REEXEC"] = "1"
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(
        sys.executable,
        [sys.executable, "-u", "-m", "services.jobs.worker_service"],
    )


def _mark_orphans(manager: JobManager) -> list[str]:
    """Flag RUNNING records as abandoned at daemon start.

    PENDING records are queued work, not orphans: a job enqueued while the
    daemon was down (or held during an in-place re-exec) must be claimed by
    the new daemon, not thrown away.
    """
    flagged: list[str] = []
    for job_id in manager.store.list_jobs():
        state = manager.store.get_job_state(job_id) or {}
        if state.get("state") == "RUNNING":
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
    # A bracketed IPv6 NO_PROXY entry crashes the openai/httpx2 client at
    # construction; the worker builds one per job, so normalize before anything.
    sanitize_proxy_env()
    logging.basicConfig(
        level=os.environ.get("JOB_WORKER_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # Silent-death forensics (2026-11-03: the daemon vanished with its three
    # RUNNING jobs mid-stream and left no trace) - same capture as app.py.
    death_capture.install("job_worker.fatal.log")
    store_dir = Path(JOB_STORE_DIR)
    store_dir.mkdir(parents=True, exist_ok=True)

    settings = _read_settings(store_dir)
    manager = JobManager(
        store_dir=str(store_dir),
        max_parallel_jobs=int(settings.get("max_parallel_jobs") or 4),
        max_stored_jobs=int(settings.get("max_stored_jobs") or 50),
    )
    reexec = os.environ.pop("SOP_JOB_WORKER_REEXEC", "") == "1"
    if reexec:
        # This PID already held the lock before os.execv; re-acquiring would
        # see its own fresh lock and refuse to start.
        _touch_lock(store_dir)
    elif not _acquire_lock(store_dir):
        logger.info("another worker daemon holds the lock - exiting")
        return 0

    # Only RUNNING records are orphans; PENDING jobs are queued work and are
    # claimed by this daemon, including after an in-place re-exec.
    orphaned = _mark_orphans(manager)
    startup_fingerprint = _LOADED_CODE_FINGERPRINT
    logger.info(
        "worker daemon started (pid %d, code %s, %d orphaned job(s) marked ABANDONED)",
        os.getpid(), startup_fingerprint[:12], len(orphaned),
    )

    claimed: set[str] = set()
    applied_settings: dict = dict(settings)
    allow_claims = True
    stale_logged = False
    last_code_check = time.time()

    def check_freshness() -> str:
        """Compare loaded code with disk; re-exec when idle if it changed."""
        nonlocal last_code_check, allow_claims, stale_logged
        last_code_check = time.time()
        current = code_fingerprint()
        action = freshness_action(
            startup_fingerprint, current, manager.active_job_count(),
        )
        allow_claims = action == "ok"
        if action == "ok":
            stale_logged = False
        elif not stale_logged:
            stale_logged = True
            logger.warning(
                "supervisor code changed on disk (loaded %s, disk %s); %s",
                startup_fingerprint[:12],
                current[:12],
                "restarting the worker now (idle)"
                if action == "restart"
                else "holding new jobs until active jobs finish",
            )
        if action == "restart":
            logger.info(
                "restarting the worker to load the new code (pid %d)",
                os.getpid(),
            )
            _reexec_worker()
        return action

    while True:
        time.sleep(_POLL_SECONDS)
        try:
            _touch_lock(store_dir)

            # Freshness gate: this process runs the modules it imported at
            # boot. If the tree changed underneath it, never start another
            # job on that stale code; wait for live jobs, then re-exec.
            if time.time() - last_code_check >= _CODE_CHECK_SECONDS:
                check_freshness()

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

            checked_this_poll = False
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
                    # One claim-time freshness check per poll: a deployment
                    # that landed after the periodic check must not slip a job
                    # through, while per-job hashing would rescan the tree for
                    # every queued job on every second.
                    if not checked_this_poll:
                        check_freshness()
                        checked_this_poll = True
                    if allow_claims and manager.run_stored_job(job_id):
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
