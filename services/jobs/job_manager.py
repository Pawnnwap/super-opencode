import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from services.jobs.event_recorder import EventRecorder
from services.jobs.state_store import JobStateStore
from supervisor.core.loop import SupervisorLoop
from supervisor.core.self_evolution_loop import SelfEvolutionLoop
from supervisor.utils.config import SupervisorConfig
from supervisor.utils.text_utils import sanitize_event_message

logger = logging.getLogger(__name__)


class JobManager:
    """Manages long-running supervisor and self-evolution jobs."""

    TERMINAL_STATES = {"SUCCESS", "FAILED", "CANCELLED"}

    def __init__(
        self,
        store_dir: str = ".job_store",
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

        The codex command builder reads SOP_PARALLEL_RUNS to disable its
        `resume --last` fallback: with several runs sharing one CODEX_HOME,
        a run without a captured session id must not adopt the globally
        latest rollout — it could be another replica's.
        """
        if self._running_count > 1:
            os.environ["SOP_PARALLEL_RUNS"] = "1"
        else:
            os.environ.pop("SOP_PARALLEL_RUNS", None)

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
        active_states = {"PENDING", "RUNNING"}
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
                if state and state.get("state") in active_states:
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
        """Retrieve current status and logs for a job.

        ``max_logs`` caps the log payload to the newest N events (see
        ``JobStateStore.tail_logs``); ``None`` keeps the historical
        read-the-whole-file behaviour used by the detail screen.
        """
        state = self.store.get_job_state(job_id)
        if not state:
            return None

        # Add logs to the state payload
        state["logs"] = (
            self.store.get_logs(job_id)
            if max_logs is None
            else self.store.tail_logs(job_id, max_logs)
        )
        return state

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

    def cancel_job(self, job_id: str):
        """Request cancellation of a running job."""
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
        out = []
        for jid in self.store.list_jobs():
            state = self.store.get_job_state(jid) or {}
            if state.get("batch_id") == batch_id:
                out.append(state)
        out.sort(key=lambda s: int(s.get("replica_index") or 0))
        return out

    def cancel_batch(self, batch_id: str) -> list[str]:
        """Cancel every still-active job of a batch. Returns cancelled ids."""
        cancelled: list[str] = []
        for jid in self.store.list_jobs():
            state = self.store.get_job_state(jid) or {}
            if state.get("batch_id") != batch_id:
                continue
            if state.get("state") in {"PENDING", "RUNNING"}:
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
            for event in stream:
                if stop_event.is_set():
                    self._request_loop_stop(loop)
                    stream.close()
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
                    if stop_event.is_set():
                        self._request_loop_stop(loop)
                        stream.close()
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

            if stop_event.is_set():
                self._request_loop_stop(loop)
                return
            # Determine final state
            logs = self.store.get_logs(job_id)
            final_state = "SUCCESS" if any(e.get("level") == "success" for e in logs) else "FAILED"

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
            report_files = ["failure_report.md", "summary.md"]
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
