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
import threading
import time
from pathlib import Path

from services.config.settings import sanitize_proxy_env
from services.jobs.code_freshness import code_fingerprint, freshness_action
from services.jobs.job_manager import JobManager, claim_order
from services.jobs.job_states import JOB_STORE_DIR, WORKER_SETTINGS_FILE
from services.runtime import death_capture

logger = logging.getLogger(__name__)

# Captured at import time, not in main(): the daemon executes the modules
# loaded here, so a file edited between import and main() must read as a
# change to reload, not be baked in as "loaded".
_LOADED_CODE_FINGERPRINT = code_fingerprint()

_LOCK_FILE = "worker.lock"
# Guardian repair intent: the guardian does not own this process, so it asks
# instead of killing (see services/runtime/guardian.py).
_STOP_FILE = "worker.stop"
_POLL_SECONDS = 1.0
_STALE_SECONDS = 15.0
# Liveness pulse cadence and the silent-stall detector threshold (see
# _LoopPulse): the watchdog tree-kills the worker once the lock ages past
# its own threshold, so the lock must be touched independently of whatever
# the poll loop is doing.
_PULSE_SECONDS = 1.0
_STALL_DUMP_SECONDS = 15.0
# How often the daemon compares its imported code with the tree on disk.
# Content hashing ~1.3 MB is milliseconds; a deployment must not wait for a
# natural worker restart (one stayed stale for 20 hours).
_CODE_CHECK_SECONDS = 10.0
# Retention pruning walks and parses the whole store; the poll loop already
# does that every second, so pruning runs on its own slower clock.
_PRUNE_SECONDS = 60.0


def _read_settings(store_dir: Path) -> dict:
    try:
        data = json.loads((store_dir / WORKER_SETTINGS_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _running_job_count(store_dir: Path) -> int:
    """RUNNING records in the store (the pulse thread's stop guard)."""
    count = 0
    try:
        for path in store_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and str(data.get("state", "")).upper() == "RUNNING":
                count += 1
    except OSError:
        return 0
    return count


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


class _LoopPulse(threading.Thread):
    """Touches ``worker.lock`` independent of the poll loop; forensics on stall.

    The lock is the guardian's only liveness signal (services/runtime/
    guardian.py treats 30 s of lock age as stale and the successor marks
    every RUNNING job ABANDONED), but it used to be touched only at the top
    of the poll loop - so ONE blocked poll step looked exactly like a dead
    daemon.
    On 2026-11-11 that killed four healthy runs four times (16:55, 17:51,
    20:15, 22:23) while their job threads streamed events fine to the last
    second: the runs were "abandoned silently at some stage".

    This thread touches the lock every second no matter where the loop is.
    A blocked loop is a degraded worker (no claims/cancels), never a dead
    one: live jobs keep their processes and the queue probe still recovers a
    claim-stalled idle worker. When the loop falls silent for
    ``_STALL_DUMP_SECONDS`` every thread's stack is dumped into the fatal
    log, naming the step the loop is stuck in.
    """

    def __init__(self, store_dir: Path, active_jobs=None):
        super().__init__(name="lock-pulse", daemon=True)
        self._store_dir = store_dir
        # In-process live-job count. The store only knows RUNNING records, and
        # a job thread that was just claimed has not written RUNNING yet, so
        # the disk view alone can call a busy worker idle.
        self._active_jobs = active_jobs
        self._last_progress = time.time()
        self._last_step = "startup"
        self._stall_dumped = False
        # Set by the pulse, consumed by the poll loop: the process exits from
        # its own main thread so job threads keep their finally cleanup.
        self.exit_requested = False

    def note(self, step: str) -> None:
        """Record poll-loop progress; cheap attribute writes only."""
        self._last_step = step
        self._last_progress = time.time()

    def run(self) -> None:
        while True:
            try:
                time.sleep(_PULSE_SECONDS)
                _touch_lock(self._store_dir)
                if self._stop_requested():
                    # The guardian cannot kill a Task-Scheduler-owned process;
                    # it writes this intent file instead. Honouring it here
                    # keeps the decision with the process that owns its own
                    # lifetime, and only when no job is in flight.
                    logger.info("worker stop requested by the guardian - exiting")
                    self.exit_requested = True
                    return
                gap = time.time() - self._last_progress
                if gap >= _STALL_DUMP_SECONDS:
                    if not self._stall_dumped:
                        self._stall_dumped = True
                        death_capture.dump_stacks(
                            f"poll loop stalled {gap:.0f}s at step '{self._last_step}'"
                        )
                else:
                    self._stall_dumped = False
            except Exception:
                logger.exception("lock pulse iteration failed")

    def _stop_requested(self) -> bool:
        """A guardian stop request, honoured only while no job is running."""
        path = self._store_dir / _STOP_FILE
        try:
            if not path.exists():
                return False
            if _running_job_count(self._store_dir) > 0:
                return False
            if self._active_jobs is not None and self._active_jobs() > 0:
                return False
            path.unlink(missing_ok=True)
            return True
        except OSError:
            return False


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


def _last_activity(store, job_id: str) -> str:
    """One-line digest of the job's newest log event: where the run stopped."""
    for event in reversed(store.tail_logs(job_id, 3)):
        msg = " ".join(str(event.get("msg") or "").split())
        if msg:
            return msg[:160]
    return ""


def _mark_orphans(manager: JobManager) -> list[str]:
    """Flag RUNNING records as abandoned at daemon start.

    PENDING records are queued work, not orphans: a job enqueued while the
    daemon was down (or held during an in-place re-exec) must be claimed by
    the new daemon, not thrown away.

    The record is not a silent tombstone: it carries ``abandon_reason``,
    ``abandoned_at`` and a ``last_activity`` digest of the newest log event,
    and the warn line shows the stage the run stopped at.
    """
    flagged: list[str] = []
    for job_id in manager.store.list_jobs():
        state = manager.store.get_job_state(job_id) or {}
        if state.get("state") == "RUNNING":
            last = _last_activity(manager.store, job_id)
            state["state"] = "ABANDONED"
            state["abandon_reason"] = (
                "worker daemon restarted while this job was active"
            )
            state["abandoned_at"] = time.time()
            if last:
                state["last_activity"] = last
            manager.store.save_job_state(job_id, state)
            manager.store.append_log(job_id, {
                "level": "warn",
                "msg": "Worker daemon restarted while this job was active - "
                       "marked ABANDONED at stage: "
                       + (last or "(no recent activity)")
                       + " (re-enqueue via the board or "
                         "SUPERVISOR_REPLAY_JOB=<job_id>).",
            })
            flagged.append(job_id)
    return flagged


def _claim_pending(
    manager: JobManager,
    pending: list[tuple[tuple, str]],
    claimed: set[str],
) -> list[str]:
    """Claim sorted PENDING records into free slots; returns the new claims.

    Materializing every PENDING record would park a thread per queued job
    behind the concurrency cap - and active_job_count() counts parked jobs,
    so a code redeploy would wait for the entire queue to drain. Claim only
    what a slot can hold, oldest first.
    """
    newly: list[str] = []
    pending.sort()
    for _order, job_id in pending:
        if manager.available_slots() <= 0:
            break
        if manager.run_stored_job(job_id):
            claimed.add(job_id)
            newly.append(job_id)
            logger.info("claimed PENDING job %s", job_id)
    return newly


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

    # Liveness is no longer the poll loop's job: one blocked loop step must
    # not read as a dead daemon to the watchdog (see _LoopPulse).
    pulse = _LoopPulse(store_dir, active_jobs=manager.active_job_count)
    pulse.start()

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
    stale_logged = False
    last_code_check = time.time()
    last_prune = time.time()
    # Per-file (mtime_ns, size) parse cache for the poll scan. Summaries are
    # never deleted (retention prunes payloads only), so re-parsing the whole
    # store every second was pure disk work that grew with history.
    state_cache: dict[str, tuple] = {}

    def check_freshness() -> str:
        """Compare loaded code with disk; re-exec when idle if it changed."""
        nonlocal stale_logged
        current = code_fingerprint()
        action = freshness_action(
            startup_fingerprint, current, manager.active_job_count(),
        )
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
                else "live jobs keep their code, the queue keeps filling, "
                     "reloading at the next idle poll",
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
        if pulse.exit_requested:
            # The pulse saw a guardian stop request and no live job. Exit from
            # the main thread so every job thread's finally cleanup (process
            # termination, slot release, log flush) has already run.
            logger.info("worker exiting on the guardian's stop request")
            return 0
        try:
            pulse.note("poll")
            now = time.time()

            # Freshness gate: this process runs the modules it imported at
            # boot. If the tree changed underneath it, never start another
            # job on that stale code; wait for live jobs, then re-exec.
            # The throttle lives HERE, not inside check_freshness: resetting
            # the clock in the callee let the claim path below re-hash the
            # whole tree every second whenever a PENDING job existed.
            if now - last_code_check >= _CODE_CHECK_SECONDS:
                pulse.note("freshness")
                last_code_check = now
                check_freshness()

            # Settings handoff: the UI writes worker_settings.json on change.
            pulse.note("settings-scan")
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

            pending: list[tuple[tuple, str]] = []
            job_ids = manager.store.list_jobs()
            for job_id in job_ids:
                if job_id in claimed:
                    # A cross-process cancel can arrive at any moment; the
                    # sentinel file (not the state field, which this worker's
                    # heartbeat rewrites) is the durable request.
                    if manager.cancel_sentinel_exists(job_id) and manager.cancel_if_live(job_id):
                        logger.info("honoured cancel request for %s", job_id)
                    continue
                state = manager.store.get_job_state_cached(job_id, state_cache) or {}
                st = state.get("state")
                if st == "PENDING":
                    pending.append((claim_order(state, job_id), job_id))
                elif st == "CANCELLED" and manager.cancel_if_live(job_id):
                    logger.info("honoured CANCELLED request for %s", job_id)
            # Deleted jobs must not keep a parse-cache entry alive forever.
            for dead in set(state_cache) - set(job_ids):
                state_cache.pop(dead, None)

            # Claim only into a FREE slot, oldest first (see _claim_pending).
            # A stale code fingerprint does not gate this: the cap is the only
            # limit on total parallelism, and the worker reloads at the next
            # idle poll (see check_freshness).
            if pending:
                pulse.note("claim")
                if now - last_code_check >= _CODE_CHECK_SECONDS:
                    last_code_check = now
                    check_freshness()
                _claim_pending(manager, pending, claimed)
            # Retention pruning walks the store again; it does not need the
            # poll cadence, and running it every second doubled the scan.
            if now - last_prune >= _PRUNE_SECONDS:
                pulse.note("prune")
                last_prune = now
                manager.prune_stored_jobs()
        except Exception:
            logger.exception("worker daemon poll cycle failed")


if __name__ == "__main__":
    raise SystemExit(main())
