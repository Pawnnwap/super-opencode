import json
import time
from pathlib import Path
from typing import Any


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
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    def get_job_state(self, job_id: str) -> dict[str, Any] | None:
        """Load job metadata and state."""
        path = self._get_job_path(job_id)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def append_log(self, job_id: str, log_event: dict[str, Any]):
        """Append a log event to the job's log file."""
        path = self._get_logs_path(job_id)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_event) + "\n")

    def get_logs(self, job_id: str) -> list[dict[str, Any]]:
        """Retrieve all log events for a job."""
        path = self._get_logs_path(job_id)
        if not path.exists():
            return []
        logs = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        parsed = json.loads(line)
                        if isinstance(parsed, dict):
                            logs.append(parsed)
        except (json.JSONDecodeError, OSError):
            pass
        return logs

    def delete_job(self, job_id: str):
        """Clean up job files."""
        for path in [self._get_job_path(job_id), self._get_logs_path(job_id)]:
            if path.exists():
                path.unlink()

    def list_jobs(self) -> list[str]:
        """List all tracked job IDs."""
        return [f.stem for f in self.store_dir.glob("*.json")]
