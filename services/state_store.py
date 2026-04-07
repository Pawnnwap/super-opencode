import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JobStateStore:
    """Handles persistence of job states and logs to disk."""

    def __init__(self, store_dir: str = ".job_store"):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def _get_job_path(self, job_id: str) -> Path:
        return self.store_dir / f"{job_id}.json"

    def _get_logs_path(self, job_id: str) -> Path:
        return self.store_dir / f"{job_id}.logs"

    def save_job_state(self, job_id: str, state: dict[str, Any]):
        """Save job metadata and current state."""
        state["job_id"] = job_id
        state["updated_at"] = time.time()
        path = self._get_job_path(job_id)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2)
                return
            except OSError as exc:
                if attempt == 2:
                    logger.error("Failed to save job state for %s after 3 attempts: %s", job_id, exc)
                    raise
                logger.warning("Transient error saving job state for %s (attempt %d): %s", job_id, attempt + 1, exc)
                time.sleep(0.1 * (attempt + 1))

    def get_job_state(self, job_id: str) -> dict[str, Any] | None:
        """Load job metadata and state."""
        path = self._get_job_path(job_id)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as exc:
            logger.warning("Corrupt job state file for %s: %s", job_id, exc)
            return None
        except OSError as exc:
            logger.warning("Cannot read job state for %s: %s", job_id, exc)
            return None

    def append_log(self, job_id: str, log_event: dict[str, Any]):
        """Append a log event to the job's log file."""
        path = self._get_logs_path(job_id)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(log_event) + "\n")
                return
            except OSError as exc:
                if attempt == 2:
                    logger.error("Failed to append log for %s after 3 attempts: %s", job_id, exc)
                    raise
                logger.warning("Transient error appending log for %s (attempt %d): %s", job_id, attempt + 1, exc)
                time.sleep(0.05 * (attempt + 1))

    def get_logs(self, job_id: str) -> list[dict[str, Any]]:
        """Retrieve all log events for a job."""
        path = self._get_logs_path(job_id)
        if not path.exists():
            return []
        logs = []
        try:
            with open(path, encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        parsed = json.loads(line)
                        if isinstance(parsed, dict):
                            logs.append(parsed)
                    except json.JSONDecodeError as exc:
                        logger.warning("Skipping corrupt log line %d for job %s: %s", lineno, job_id, exc)
        except OSError as exc:
            logger.warning("Cannot read logs for job %s: %s", job_id, exc)
        return logs

    def delete_job(self, job_id: str):
        """Clean up job files."""
        for path in [self._get_job_path(job_id), self._get_logs_path(job_id)]:
            if path.exists():
                path.unlink()

    def list_jobs(self) -> list[str]:
        """List all tracked job IDs."""
        return [f.stem for f in self.store_dir.glob("*.json")]
