import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from services.jobs.event_recorder import EventRecorder
from services.jobs.job_states import (
    ACTIVE_JOB_STATES,
    JOB_STORE_DIR,
    TERMINAL_JOB_STATES,
)
from services.jobs.state_store import JobStateStore
from supervisor.core.loop import SupervisorLoop
from supervisor.core.self_evolution_loop import SelfEvolutionLoop
from supervisor.runners.command_common import PARALLEL_RUNS_ENV
from supervisor.utils.config import SupervisorConfig
from supervisor.utils.text_utils import sanitize_event_message

logger = logging.getLogger(__name__)

# Missing-engine preflight. A PENDING record must stay queued when the CLI it
# needs is not installed: on 2026-11-06 an interrupted npm upgrade removed
# codex's bin shims, and the worker claimed seven queued replicas that all
# died in under a second with "codex not found", burning them as FAILED.
_CODEX_PROBE_TTL_SECONDS = 30.0
_codex_probe: dict[str, Any] = {"at": 0.0, "path": "", "error": ""}
_codex_blocked_logged: set[str] = set()


def codex_engine_ready(explicit: str = "") -> tuple[bool, str]:
    """Whether a codex job can start at all (probe cached for the poll loop)."""
    now = time.time()
    if not explicit and now - _codex_probe["at"] < _CODEX_PROBE_TTL_SECONDS:
        return bool(_codex_probe["path"]), str(_codex_probe["error"])
    try:
        from supervisor.runners.codex_support.locator import find_codex

        path, error = find_codex(explicit), ""
    except FileNotFoundError as exc:
        path, error = "", str(exc)
    if not explicit:
        _codex_probe.update(at=now, path=path, error=error)
    return bool(path), error

# Level -> terminal state. The newest one in the log wins: a run that emitted
# success long ago and then aborted must not be recorded SUCCESS off that old
# event (the previous `any(...)` scan did exactly that over 28h job logs).
_TERMINAL_EVENT_STATES = {"success": "SUCCESS", "blocked": "BLOCKED"}


def final_state_from_logs(logs: list[dict[str, Any]]) -> str:
    """Map a job's event log to its terminal state (last terminal event wins)."""
    for index in range(len(logs) - 1, -1, -1):
        state = _TERMINAL_EVENT_STATES.get(str(logs[index].get("level")))
        if not state:
            continue
        # An error newer than the closing marker means the run broke on its way
        # out (cleanup, occam pass, worker) - the marker cannot vouch for it.
        if any(str(e.get("level")) == "error" for e in logs[index + 1:]):
            return "FAILED"
        return state
    return "FAILED"


class JobManager:
    """Manages long-running supervisor and self-evolution jobs."""

    TERMINAL_STATES = TERMINAL_JOB_STATES

    def __init__(
        self,
        store_dir: str = JOB_STORE_DIR,
        max_parallel_jobs: int = 4,
        max_stored_jobs: int = 50,
    ):
        self.store = JobStateStore(store_dir)
        self._active_jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        # Concurrency cap: at most ``max_parallel_jobs`` workers hold a slot
        # (RUNNING); extra workers park in PENDING until one frees. A
        # condition + counter instead of a Semaphore so the cap is adjustable
        # while jobs are queued, and so waiting workers can notice
        # cancellation instead of blocking on acquire().
        self._cond = threading.Condition(self._lock)
        self._max_parallel = max(1, int(max_parallel_jobs))
        self._running_count = 0
        # Retention cap for TERMINAL jobs: beyond this many, the oldest
        # jobs' log payloads (.logs/.full) are pruned on enqueue; their
        # state json summaries stay for the board history.
        self._max_stored = max(0, int(max_stored_jobs))

    def set_max_parallel_jobs(self, value: int) -> None:
        with self._cond:
            self._max_parallel = max(1, int(value))
            self._cond.notify_all()

    def set_max_stored_jobs(self, value: int) -> None:
        self._max_stored = max(0, int(value))

    def prune_stored_jobs(self) -> list[str]:
        """Drop log payloads of the oldest terminal jobs past the cap.

        The store grew past 100 MB / ~100 jobs with no cap and nothing
        called delete_job; summaries (json) are kept so history cards still
        render. Active jobs are never touched.
        """
        if self._max_stored <= 0:
            return []
        terminal: list[tuple[float, str]] = []
        for jid in self.store.list_jobs():
            state = self.store.get_job_state(jid) or {}
            if state.get("state") in self.TERMINAL_STATES:
                terminal.append((float(state.get("updated_at") or 0), jid))
        terminal.sort()
        pruned: list[str] = []
        for _ts, jid in terminal[: max(0, len(terminal) - self._max_stored)]:
            if self.store.drop_job_logs(jid):
                pruned.append(jid)
        return pruned

    def _wait_for_slot(self, stop_event: threading.Event) -> bool:
        """Park until a run slot frees. Returns False if cancelled while parked."""
        with self._cond:
            while self._running_count >= self._max_parallel:
                if stop_event.is_set():
                    return False
                self._cond.wait(timeout=0.5)
            self._running_count += 1
            self._sync_parallel_flag()
            return True

    def _release_slot(self) -> None:
        with self._cond:
            self._running_count = max(0, self._running_count - 1)
            self._sync_parallel_flag()
            self._cond.notify()

    def _sync_parallel_flag(self) -> None:
        """Announce multi-run concurrency to child-facing code.

        The command builders read :data:`PARALLEL_RUNS_ENV` to disable their
        resume fallback: with several runs sharing one session store, a run
        without a captured session id must not adopt the globally latest
        rollout — it could be another replica's.
        """
        if self._running_count > 1:
            os.environ[PARALLEL_RUNS_ENV] = "1"
        else:
            os.environ.pop(PARALLEL_RUNS_ENV, None)

    def enqueue_job(
        self,
        job_type: str,
        config: SupervisorConfig,
        meta: dict[str, Any] | None = None,
    ) -> str:
        """Submit a job to be executed in the background.

        ``meta`` carries optional grouping fields (``batch_id``,
        ``replica_index``, ``replica_count``) persisted into the job state so
        parallel-study batches can be rendered and cancelled as one unit.
        """
        new_workspace = Path(config.workspace).resolve()
        # Block on any non-terminal state, not just RUNNING. A PENDING peer
        # would otherwise be allowed to share the workspace and race us for
        # `.opencode/sessions`, defeating per-task session isolation.
        job_id = f"{job_type}_{uuid.uuid4().hex[:8]}"
        stop_event = threading.Event()
        meta = dict(meta or {})

        # Hold the lock across the collision check AND the PENDING write so
        # two concurrent enqueues for the same workspace cannot both pass the
        # guard. Without this, each caller could read the store before either
        # one has written its PENDING record.
        with self._lock:
            for jid in self.store.list_jobs():
                state = self.store.get_job_state(jid)
                if state and state.get("state") in ACTIVE_JOB_STATES:
                    existing_workspace = Path(state.get("config", {}).get("workspace", "")).resolve()
                    if existing_workspace == new_workspace:
                        raise ValueError(f"Another task is already active in this workspace: {new_workspace}")

            self._active_jobs[job_id] = {"stop_event": stop_event, "loop": None}

            # Initial state — written while still holding the lock so the
            # next enqueue's collision scan is guaranteed to observe it.
            self.store.save_job_state(job_id, {
                "type": job_type,
                "state": "PENDING",
                "progress": 0.0,
                "heartbeat_at": time.time(),
                "config": self._serialize_config(config),
                "report": "",
                **meta,
            })

        # Start worker thread. The name shows up in log records (via the
        # threadName formatter token) so concurrent jobs can be told apart
        # on shared stderr.
        thread = threading.Thread(
            target=self._worker,
            args=(job_id, job_type, config, stop_event, meta),
            daemon=True,
            name=f"job-{job_id}",
        )
        thread.start()

        # Opportunistic retention: prune old terminal payloads off the
        # enqueue path (cheap scan, no locks held).
        pruned = self.prune_stored_jobs()
        if pruned:
            logger.info("Pruned log payloads of %d old job(s)", len(pruned))

        return job_id

    def get_job_status(self, job_id: str, max_logs: int | None = None) -> dict[str, Any] | None:
        """Retrieve current status and logs for a job (see ``read_job_status``)."""
        return self.store.read_job_status(job_id, max_logs)

    def get_job_summary(self, job_id: str) -> dict[str, Any] | None:
        """State WITHOUT logs - for board/switcher scans.

        Those widgets call this for every job on a 1.5 s timer, and attaching
        logs there meant parsing every job's whole log file on every refresh
        (tens of MB with a few hundred historical jobs), which is what made
        ``/run`` unrenderable. Nothing in the list widgets reads logs.
        """
        return self.store.get_job_state(job_id)

    def get_event_detail(self, job_id: str, ref: int) -> dict[str, Any] | None:
        """Fetch one full event body from the job's sidecar by ref."""
        return self.store.read_full(job_id, ref)

    def config_from_state(self, job_id: str) -> SupervisorConfig | None:
        """Rebuild a SupervisorConfig from the job's stored record."""
        from dataclasses import fields as dataclass_fields

        state = self.store.get_job_state(job_id)
        if not state:
            return None
        raw = dict(state.get("config") or {})
        names = {f.name for f in dataclass_fields(SupervisorConfig)}
        kwargs = {k: v for k, v in raw.items() if k in names}
        kwargs["protocol_path"] = Path(kwargs["protocol_path"])
        kwargs["workspace"] = Path(kwargs["workspace"])
        kwargs["protected_files"] = tuple(kwargs.get("protected_files") or ())
        return SupervisorConfig(**kwargs)

    def run_stored_job(self, job_id: str) -> bool:
        """Claim a PENDING store record and execute it in this process.

        The worker daemon's claim path: the UI enqueues by writing a PENDING
        record, and this turns the record into a live worker thread here.
        Callers must keep their own claimed-id set - a parked job stays
        PENDING until a run slot frees.
        """
        state = self.store.get_job_state(job_id)
        if not state or state.get("state") != "PENDING":
            return False
        if self.cancel_sentinel_exists(job_id):
            # Cancelled between enqueue and claim: never start it.
            state["state"] = "CANCELLED"
            self.store.save_job_state(job_id, state)
            return False
        config = self.config_from_state(job_id)
        if config is None:
            return False
        if str(getattr(config, "engine", "") or "").lower() == "codex":
            ready, why = codex_engine_ready(
                str(getattr(config, "opencode_executable", "") or ""),
            )
            if not ready:
                if job_id not in _codex_blocked_logged:
                    _codex_blocked_logged.add(job_id)
                    logger.error(
                        "leaving PENDING job %s queued: %s",
                        job_id,
                        (why or "codex CLI not found").splitlines()[0],
                    )
                return False
            _codex_blocked_logged.discard(job_id)
        job_type = str(state.get("type") or "run")
        meta = {
            k: state[k]
            for k in ("batch_id", "replica_index", "replica_count")
            if state.get(k) is not None
        }
        stop_event = threading.Event()
        with self._lock:
            self._active_jobs[job_id] = {"stop_event": stop_event, "loop": None}
        thread = threading.Thread(
            target=self._worker,
            args=(job_id, job_type, config, stop_event, meta),
            daemon=True,
            name=f"job-{job_id}",
        )
        thread.start()
        return True

    def active_job_count(self) -> int:
        """Live entries owned by this process (parked PENDING included)."""
        with self._lock:
            return len(self._active_jobs)

    def cancel_if_live(self, job_id: str) -> bool:
        """Cancel only when this process owns a live job not already stopping.

        The worker's poll visits a claimed job every second until its thread
        exits. Re-cancelling an already-signalled job spammed "Job cancelled
        by user." every poll for the whole drain (observed for ~60 s on a
        stuck codex turn), so the stop flag is the idempotence marker.
        """
        with self._lock:
            entry = self._active_jobs.get(job_id)
            if entry is None:
                return False
            if entry["stop_event"].is_set():
                return False
        self.cancel_job(job_id)
        return True

    def _cancel_sentinel_path(self, job_id: str) -> Path:
        return self.store.store_dir / f"{job_id}.cancel"

    def cancel_sentinel_exists(self, job_id: str) -> bool:
        return self._cancel_sentinel_path(job_id).exists()

    def cancel_job(self, job_id: str):
        """Request cancellation of a running job."""
        # Sentinel FIRST, before any state write: the worker heartbeat
        # rewrites the state file (RUNNING) every few seconds, so a cancel
        # that lives only in the state field can be erased by the very
        # process it targets. The file is the durable intent; the state
        # write is just immediate board feedback.
        try:
            self._cancel_sentinel_path(job_id).write_text(
                str(time.time()), encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Could not write cancel sentinel for %s: %s", job_id, exc)
        with self._lock:
            if job_id in self._active_jobs:
                entry = self._active_jobs[job_id]
                # Signal the worker to stop FIRST, before any state writes
                entry["stop_event"].set()
                loop = entry.get("loop")
                if loop is not None:
                    self._request_loop_stop(loop)

        # Write cancellation state AFTER signalling, outside the lock,
        # so the worker's stop_event check wins the race reliably.
        self.store.append_log(job_id, {"level": "warn", "msg": "Job cancelled by user."})
        current = self.store.get_job_state(job_id) or {}
        current["state"] = "CANCELLED"
        current["heartbeat_at"] = time.time()
        self.store.save_job_state(job_id, current)

    def _request_loop_stop(self, loop: Any) -> None:
        """Ask a loop to stop without assuming its concrete implementation."""
        request_stop = getattr(loop, "request_stop", None)
        if callable(request_stop):
            request_stop()
            return

        runner = getattr(loop, "runner", None)
        if runner is not None:
            runner.stop()

    def list_batch_jobs(self, batch_id: str) -> list[dict[str, Any]]:
        """Summaries (no logs) of every job belonging to a study batch."""
        return self.store.list_batch_jobs(batch_id)

    def cancel_batch(self, batch_id: str) -> list[str]:
        """Cancel every still-active job of a batch. Returns cancelled ids."""
        cancelled: list[str] = []
        for jid in self.store.list_jobs():
            state = self.store.get_job_state(jid) or {}
            if state.get("batch_id") != batch_id:
                continue
            if state.get("state") in ACTIVE_JOB_STATES:
                self.cancel_job(jid)
                cancelled.append(jid)
        return cancelled

    def _worker(
        self,
        job_id: str,
        job_type: str,
        config: SupervisorConfig,
        stop_event: threading.Event,
        meta: dict[str, Any] | None = None,
    ):
        """Worker thread that executes the job loop."""
        meta = dict(meta or {})
        # Only release the run slot in `finally` if one was actually taken —
        # the cancelled-while-parked path never increments the counter.
        slot_held = False
        sentinel = self._cancel_sentinel_path(job_id)
        try:
            # Park in PENDING while all run slots are taken. A cancellation
            # while parked has already written CANCELLED — just leave.
            if not self._wait_for_slot(stop_event):
                with self._lock:
                    self._active_jobs.pop(job_id, None)
                return
            slot_held = True

            # cancel_batch may have written CANCELLED while we were parked.
            stored = self.store.get_job_state(job_id) or {}
            if stored.get("state") in self.TERMINAL_STATES:
                return

            # Update state to RUNNING
            self.store.save_job_state(job_id, {
                "type": job_type,
                "state": "RUNNING",
                "progress": 0.0,
                "heartbeat_at": time.time(),
                "config": self._serialize_config(config),
                "report": "",
                **meta,
            })

            # Select the appropriate loop
            if job_type == "run":
                loop = SupervisorLoop(config)
            elif job_type == "evolve":
                loop = SelfEvolutionLoop(config)
            else:
                raise ValueError(f"Unknown job type: {job_type}")

            with self._lock:
                if job_id in self._active_jobs:
                    self._active_jobs[job_id]["loop"] = loop

            heartbeat_interval = 5.0
            last_heartbeat = time.time()
            report_content = ""
            # Headline mode condenses large blocks to one key line each (full
            # bodies go to the .full sidecar); "full" restores verbatim logs.
            recorder = EventRecorder(
                self.store, job_id,
                mode=str(getattr(config, "log_detail", "headline")),
            )

            # Run the loop and stream events
            stream = loop.run_streaming()
            # Cross-process cancel: the UI (or another process) may drop a
            # sentinel file at any moment - the state field alone is racy
            # against this worker's own RUNNING heartbeats.
            stop_requested = (
                lambda: stop_event.is_set() or sentinel.exists()
            )
            for event in stream:
                if stop_requested():
                    self._request_loop_stop(loop)
                    stream.close()
                    if sentinel.exists():
                        self._update_state(job_id, "CANCELLED", report=report_content)
                    return

                # Handle non-dict events (e.g., strings) by converting to event dict
                if not isinstance(event, dict):
                    if isinstance(event, str):
                        event = {"level": "info", "msg": event}
                    else:
                        event = {"level": "info", "msg": sanitize_event_message(event)}

                # Rewrite to headline form BEFORE sanitizing: the recorder
                # builds the final msg (digest/headline), so sanitizing the
                # raw body first would be wasted work on soon-dropped text.
                recorded = recorder.record(event)
                if recorded is None:
                    continue
                event = recorded
                # Sanitize msg field before logging
                if "msg" in event:
                    event["msg"] = sanitize_event_message(event["msg"])
                self.store.append_log(job_id, event)

                # Capture report if it's emitted as an event
                if event.get("level") == "report":
                    report_content = event.get("msg", "")
                # Periodic heartbeat. The heartbeat lands in the state file
                # only — a heartbeat *event* per 5 s was 3k+ noise lines per
                # run and nothing in the UI consumed them.
                now = time.time()
                if now - last_heartbeat >= heartbeat_interval:
                    if stop_requested():
                        self._request_loop_stop(loop)
                        stream.close()
                        if sentinel.exists():
                            self._update_state(job_id, "CANCELLED", report=report_content)
                        return

                    self.store.save_job_state(job_id, {
                        "type": job_type,
                        "state": "RUNNING",
                        "progress": 0.0,
                        "heartbeat_at": now,
                        "config": self._serialize_config(config),
                        "report": report_content,
                        **meta,
                    })
                    last_heartbeat = now

            if stop_requested():
                self._request_loop_stop(loop)
                if sentinel.exists():
                    self._update_state(job_id, "CANCELLED", report=report_content)
                return
            # Determine final state
            logs = self.store.get_logs(job_id)
            final_state = final_state_from_logs(logs)

            # Final report check if not captured during streaming
            if not report_content:
                report_content = self._fetch_report(config.workspace, job_type)

            self._update_state(job_id, final_state, report=report_content)

        except Exception as e:
            import traceback
            error_msg = f"Worker error: {e!s}\n{traceback.format_exc()}"
            logger.error("Job %s failed: %s", job_id, e, exc_info=True)
            self.store.append_log(job_id, {"level": "error", "msg": error_msg})
            self._update_state(job_id, "FAILED", report=report_content if "report_content" in locals() else "")
        finally:
            if slot_held:
                self._release_slot()
            with self._lock:
                if job_id in self._active_jobs:
                    del self._active_jobs[job_id]
            # The request has been fully consumed (worker exited); leave no
            # stale sentinel behind to cancel a future re-enqueue.
            try:
                sentinel.unlink(missing_ok=True)
            except OSError:
                pass

    def _update_state(self, job_id: str, state_name: str, report: str = ""):
        """Helper to update the stored job state."""
        current = self.store.get_job_state(job_id) or {}
        current.update({
            "state": state_name,
            "heartbeat_at": time.time(),
            "report": report,
        })
        self.store.save_job_state(job_id, current)

    def _fetch_report(self, workspace: Path, job_type: str) -> str:
        """Try to read final report files from workspace."""
        report_files = []
        if job_type == "run":
            # A BLOCKED run writes blocked_report.md, not failure_report.md -
            # without it the job's stored report (and its preview) is empty.
            report_files = ["failure_report.md", "blocked_report.md", "summary.md"]
        elif job_type == "evolve":
            report_files = ["evolution_report.md"]

        for filename in report_files:
            p = workspace / filename
            if p.exists():
                try:
                    return p.read_text(encoding="utf-8")
                except OSError as exc:
                    logger.warning("Could not read report file %s: %s", p, exc)
        return ""

    def _serialize_config(self, config: SupervisorConfig) -> dict[str, Any]:
        """Convert SupervisorConfig to a serializable dict."""
        return config.to_state_dict()
