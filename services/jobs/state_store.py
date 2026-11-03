import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Full-body sidecar rotation bound (single .full.1 backup kept).
_MAX_FULL_BYTES = 16 * 1024 * 1024


class JobStateStore:
    """Handles persistence of job states and logs to disk."""

    def __init__(self, store_dir: str = ".job_store"):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        # Per-job next sidecar ref, seeded lazily from existing files.
        self._full_next_ref: dict[str, int] = {}

    def _get_job_path(self, job_id: str) -> Path:
        return self.store_dir / f"{job_id}.json"

    def _get_logs_path(self, job_id: str) -> Path:
        return self.store_dir / f"{job_id}.logs"

    def _get_full_path(self, job_id: str) -> Path:
        return self.store_dir / f"{job_id}.full"

    def _seed_full_ref(self, job_id: str) -> int:
        """Recover the highest ref ever written so refs stay unique."""
        highest = 0
        for suffix in ("", ".1"):
            path = self._get_full_path(job_id).with_name(
                self._get_full_path(job_id).name + suffix
            )
            if not path.exists():
                continue
            try:
                with open(path, encoding="utf-8-sig") as f:
                    for line in f:
                        try:
                            ref = json.loads(line).get("ref")
                        except json.JSONDecodeError:
                            continue
                        if isinstance(ref, int) and ref > highest:
                            highest = ref
            except OSError as exc:
                logger.warning("Cannot seed sidecar refs for %s: %s", job_id, exc)
        return highest

    def append_full(self, job_id: str, record: dict[str, Any]) -> int:
        """Append a full event body to the job's sidecar; return its ref.

        One JSON line per body: ``{"ref": N, "ts": ..., "level": ..., "body": ...}``.
        Rotates to ``.full.1`` (single backup) past 16 MB, mirroring the
        supervisor_prompts.log pattern — headline events in ``.logs`` keep
        working, only very old bodies become unretrievable.
        """
        if job_id not in self._full_next_ref:
            self._full_next_ref[job_id] = self._seed_full_ref(job_id)
        self._full_next_ref[job_id] += 1
        ref = self._full_next_ref[job_id]

        record = {**record, "ref": ref, "ts": time.time()}
        path = self._get_full_path(job_id)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            try:
                if (
                    path.exists()
                    and path.stat().st_size > _MAX_FULL_BYTES
                ):
                    backup = path.with_name(path.name + ".1")
                    if backup.exists():
                        backup.unlink()
                    path.rename(backup)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
                return ref
            except OSError as exc:
                if attempt == 2:
                    logger.error("Failed to append sidecar for %s: %s", job_id, exc)
                    raise
                logger.warning(
                    "Transient error appending sidecar for %s (attempt %d): %s",
                    job_id, attempt + 1, exc,
                )
                time.sleep(0.05 * (attempt + 1))
        return ref  # unreachable; for type-checkers

    def read_full(self, job_id: str, ref: int) -> dict[str, Any] | None:
        """Fetch one sidecar record by ref (current file first, then backup)."""
        base = self._get_full_path(job_id)
        for suffix in ("", ".1"):
            path = base.with_name(base.name + suffix)
            if not path.exists():
                continue
            try:
                with open(path, encoding="utf-8-sig") as f:
                    for line in f:
                        try:
                            parsed = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if parsed.get("ref") == ref:
                            return parsed
            except OSError as exc:
                logger.warning("Cannot read sidecar for %s: %s", job_id, exc)
        return None

    def save_job_state(self, job_id: str, state: dict[str, Any]):
        """Save job metadata and current state.

        Writes go to a per-call temp file then ``os.replace``: another
        thread (an enqueue's collision scan, a cancel) may read this file at
        any moment, and a plain open("w") truncates first — a concurrent
        reader saw an empty file and treated the job as unknown.

        The temp name is unique per call: the worker's RUNNING heartbeat and
        the UI process's cancel write race on the same state file, and with
        one fixed ``.tmp`` name a writer truncated the temp file another
        writer was mid-replace on, publishing torn JSON (2026-11-02 UI
        freeze). On Windows ``os.replace`` also fails with EACCES while any
        reader holds the destination open, hence the retries — 5 of them,
        because readers poll every 2 s and hold the file briefly.
        """
        state["job_id"] = job_id
        state["updated_at"] = time.time()
        path = self._get_job_path(job_id)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        payload = json.dumps(state, indent=2)
        try:
            for attempt in range(5):
                try:
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        f.write(payload)
                    os.replace(tmp_path, path)
                    return
                except OSError as exc:
                    if attempt == 4:
                        logger.error("Failed to save job state for %s after 5 attempts: %s", job_id, exc)
                        raise
                    logger.warning("Transient error saving job state for %s (attempt %d): %s", job_id, attempt + 1, exc)
                    time.sleep(0.1 * (attempt + 1))
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def get_job_state(self, job_id: str) -> dict[str, Any] | None:
        """Load job metadata and state.

        ``utf-8-sig`` so a state file carrying a UTF-8 BOM (written by an
        editor or an ad-hoc script) still loads; plain UTF-8 is unaffected.

        Transient OSErrors (Windows EACCES while a concurrent ``os.replace``
        is in flight — the exact failure behind the 2026-11-02 UI freeze)
        are retried before the read is given up on; a single blip must not
        surface to callers as "job unknown".
        """
        path = self._get_job_path(job_id)
        if not path.exists():
            return None
        for attempt in range(3):
            try:
                with open(path, encoding="utf-8-sig") as f:
                    return json.load(f)
            except json.JSONDecodeError as exc:
                logger.warning("Corrupt job state file for %s: %s", job_id, exc)
                return None
            except OSError as exc:
                if attempt == 2:
                    logger.warning("Cannot read job state for %s: %s", job_id, exc)
                    return None
                time.sleep(0.02 * (attempt + 1))
        return None  # unreachable; for type-checkers

    def append_log(self, job_id: str, log_event: dict[str, Any]):
        """Append a log event to the job's log file.

        Stamps a wall-clock ``ts`` (epoch seconds) for the UI log view if the
        event does not already carry one. This is the single chokepoint every
        logged event passes through (loop ``_ev`` events, raw runner-yielded
        dicts, and direct status appends alike), so it is the one place a
        timestamp is needed. The timestamp is purely for logging — the
        supervisor's context is built from ``read_output()`` and never flows
        through here, so it stays timestamp-free.
        """
        path = self._get_logs_path(job_id)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(log_event, dict) and "ts" not in log_event:
            log_event = {**log_event, "ts": time.time()}
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
            with open(path, encoding="utf-8-sig") as f:
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

    def tail_logs(self, job_id: str, max_events: int) -> list[dict[str, Any]]:
        """Return at most the last ``max_events`` log events, reading only the
        tail of the file.

        ``get_logs`` parses the whole file, which is fine for one job's detail
        view but ruinous when a board scans every job on a timer - job logs grow
        to tens of megabytes. Reads backwards in growing byte chunks so a small
        file costs one read and a huge file never loads wholesale.
        """
        path = self._get_logs_path(job_id)
        if max_events <= 0 or not path.exists():
            return []
        chunk = 65536
        raw = b""
        full_read = False
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                span = min(chunk, size)
                while True:
                    # Re-read the whole tail span each round (never prepend:
                    # the wider slice already contains the previous one).
                    f.seek(size - span)
                    raw = f.read(span)
                    if span >= size:
                        full_read = True
                        break
                    if raw.count(b"\n") > max_events:
                        break
                    span = min(size, span * 4)
        except OSError as exc:
            logger.warning("Cannot read logs for job %s: %s", job_id, exc)
            return []
        lines = raw.decode("utf-8-sig", errors="replace").splitlines()
        if not full_read and lines:
            # We started mid-file, so the leading line is a fragment.
            lines = lines[1:]
        logs: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue  # partial leading line / corrupt tail; skip quietly
            if isinstance(parsed, dict):
                logs.append(parsed)
        return logs[-max_events:]

    def delete_job(self, job_id: str):
        """Clean up job files."""
        base = self._get_logs_path(job_id)
        for path in [self._get_job_path(job_id), base] + [
            base.with_name(base.name + suffix) for suffix in (".full", ".full.1")
        ]:
            if path.exists():
                path.unlink()

    def drop_job_logs(self, job_id: str) -> bool:
        """Remove a terminal job's bulky log files, keeping the state json.

        Retention pruning: the state summary stays for the board's history
        list, while the multi-MB .logs/.full payloads go. Returns True when
        anything was removed.
        """
        removed = False
        base = self._get_logs_path(job_id)
        for path in [base] + [
            base.with_name(base.name + suffix) for suffix in (".full", ".full.1")
        ]:
            if path.exists():
                try:
                    path.unlink()
                    removed = True
                except OSError as exc:
                    logger.warning("Could not prune %s: %s", path, exc)
        return removed

    def list_jobs(self) -> list[str]:
        """List all tracked job IDs."""
        return [f.stem for f in self.store_dir.glob("*.json")]
