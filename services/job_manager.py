import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from services.state_store import JobStateStore
from supervisor.core.loop import SupervisorLoop
from supervisor.core.self_evolution_loop import SelfEvolutionLoop
from supervisor.utils.config import SupervisorConfig


class JobManager:
    """Manages long-running supervisor and self-evolution jobs."""

    def __init__(self, store_dir: str = ".job_store"):
        self.store = JobStateStore(store_dir)
        self._active_jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def enqueue_job(self, job_type: str, config: SupervisorConfig) -> str:
        """Submit a job to be executed in the background."""
        job_id = f"{job_type}_{uuid.uuid4().hex[:8]}"
        stop_event = threading.Event()

        with self._lock:
            self._active_jobs[job_id] = {"stop_event": stop_event, "loop": None}

        # Initial state
        self.store.save_job_state(job_id, {
            "type": job_type,
            "state": "PENDING",
            "progress": 0.0,
            "heartbeat_at": time.time(),
            "config": self._serialize_config(config),
            "report": ""
        })

        # Start worker thread
        thread = threading.Thread(
            target=self._worker,
            args=(job_id, job_type, config, stop_event),
            daemon=True
        )
        thread.start()

        return job_id

    def get_job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve current status and logs for a job."""
        state = self.store.get_job_state(job_id)
        if not state:
            return None

        # Add logs to the state payload
        state["logs"] = self.store.get_logs(job_id)
        return state

    def cancel_job(self, job_id: str):
        """Request cancellation of a running job."""
        with self._lock:
            if job_id in self._active_jobs:
                entry = self._active_jobs[job_id]
                # Signal the worker to stop FIRST, before any state writes
                entry["stop_event"].set()
                loop = entry.get("loop")
                if loop is not None:
                    runner = getattr(loop, "runner", None)
                    if runner is not None:
                        runner.stop()

        # Write cancellation state AFTER signalling, outside the lock,
        # so the worker's stop_event check wins the race reliably.
        self.store.append_log(job_id, {"level": "warn", "msg": "Job cancelled by user."})
        current = self.store.get_job_state(job_id) or {}
        current["state"] = "CANCELLED"
        current["heartbeat_at"] = time.time()
        self.store.save_job_state(job_id, current)

    def _worker(self, job_id: str, job_type: str, config: SupervisorConfig, stop_event: threading.Event):
        """Worker thread that executes the job loop."""
        try:
            # Update state to RUNNING
            self.store.save_job_state(job_id, {
                "type": job_type,
                "state": "RUNNING",
                "progress": 0.0,
                "heartbeat_at": time.time(),
                "config": self._serialize_config(config),
                "report": ""
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

            # Run the loop and stream events
            for event in loop.run_streaming():
                if stop_event.is_set():
                    return

                # Record the event in the log store
                self.store.append_log(job_id, event)

                # Capture report if it's emitted as an event
                if event.get("level") == "report":
                    report_content = event.get("msg", "")

                # Periodic heartbeat
                now = time.time()
                if now - last_heartbeat >= heartbeat_interval:
                    if stop_event.is_set():
                        return

                    # Also append a heartbeat event so the UI can count them
                    self.store.append_log(job_id, {
                        "level": "heartbeat",
                        "msg": "Heartbeat — supervisor still active"
                    })

                    self.store.save_job_state(job_id, {
                        "type": job_type,
                        "state": "RUNNING",
                        "progress": self._estimate_progress(event),
                        "heartbeat_at": now,
                        "config": self._serialize_config(config),
                        "report": report_content
                    })
                    last_heartbeat = now

            if stop_event.is_set():
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
            error_msg = f"Worker error: {str(e)}\n{traceback.format_exc()}"
            self.store.append_log(job_id, {"level": "error", "msg": error_msg})
            self._update_state(job_id, "FAILED", report=report_content if 'report_content' in locals() else "")
        finally:
            with self._lock:
                if job_id in self._active_jobs:
                    del self._active_jobs[job_id]

    def _update_state(self, job_id: str, state_name: str, report: str = ""):
        """Helper to update the stored job state."""
        current = self.store.get_job_state(job_id) or {}
        current.update({
            "state": state_name,
            "heartbeat_at": time.time(),
            "report": report
        })
        self.store.save_job_state(job_id, current)

    def _estimate_progress(self, event: Dict[str, Any]) -> float:
        """Crude progress estimation based on event messages."""
        # This could be improved if loops emitted explicit progress percentages
        return 0.0  # Default to 0 if not easily inferrable

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
                except Exception:
                    pass
        return ""

    def _serialize_config(self, config: SupervisorConfig) -> Dict[str, Any]:
        """Convert SupervisorConfig to a serializable dict."""
        return {
            "protocol_path": str(config.protocol_path),
            "workspace": str(config.workspace),
            "max_retries": config.max_retries,
            "context_threshold": config.context_threshold,
            "opencode_model": config.opencode_model,
            "opencode_executable": config.opencode_executable,
            "supervisor_model": config.supervisor_model,
            "timeout": config.timeout,
            "protected_files": list(config.protected_files),
            "max_tokens": config.max_tokens,
            "plan_mode_rounds": config.plan_mode_rounds
        }