"""Append-only ownership ledger for a run's child processes.

A kill must be provable: "this pid belongs to this run" is decided by a
record written at spawn time — ``(pid, create_time)`` — never by a process
name or a command-line keyword. The ledger is fsynced per append because the
one case that matters most (the harness dies right after a spawn) cannot be
reconstructed afterwards: psutil can tell us a pid exists, but not who
started it.

Layout: ``<job store>/<run_id>.procs.jsonl``, one JSON object per line. The
last record for a pid wins (a later ``exited`` marker supersedes the spawn
record), so the file stays append-only and crash-safe with no rewriting.

``reconcile`` deliberately only classifies: a restart must never kill, it may
only report. Termination is the job of
:mod:`supervisor.runtime.process_control`, which revalidates identity first.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# A Windows create_time is a float in seconds; a recycled pid differs by far
# more than this. Shared by every ownership check in the package.
CREATE_TIME_TOLERANCE = 0.01

# The job store is the IPC directory shared with services/jobs; the supervisor
# package must not import services, so the default is derived from the repo
# root and can be overridden for tests and out-of-tree deployments.
_ENV_STORE_DIR = "SOP_JOB_STORE_DIR"
_DEFAULT_SUBDIR = ".job_store"


def ledger_dir() -> Path:
    """Directory holding run job records and their process ledgers."""
    override = (os.environ.get(_ENV_STORE_DIR) or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / _DEFAULT_SUBDIR


def ledger_path(run_id: str) -> Path:
    return ledger_dir() / f"{_safe_run_id(run_id)}.procs.jsonl"


def refusal_log_path(run_id: str) -> Path:
    return ledger_dir() / f"{_safe_run_id(run_id)}.kill_refusals.jsonl"


def _safe_run_id(run_id: str) -> str:
    """A run id names a file: keep it to one plain path segment."""
    text = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(run_id))
    return text or "run"


@dataclass(frozen=True)
class ProcRecord:
    """One spawn (or one exit marker) in a run's ledger."""

    run_id: str
    pid: int
    create_time: float
    role: str = "child"  # engine | matlab | python | rust | service | probe
    exe: str = ""
    cmdline: str = ""
    parent_pid: int | None = None
    job_id: str | None = None
    ts: float = 0.0
    state: str = "live"  # live | exited

    def as_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class ProcessRegistry:
    """Per-run ledger: register at spawn, read for ownership proofs."""

    def __init__(self, run_id: str, directory: Path | None = None):
        self.run_id = str(run_id)
        self.directory = Path(directory) if directory is not None else ledger_dir()

    # -- paths ----------------------------------------------------------- #

    @property
    def path(self) -> Path:
        return self.directory / f"{_safe_run_id(self.run_id)}.procs.jsonl"

    @property
    def refusal_path(self) -> Path:
        return self.directory / f"{_safe_run_id(self.run_id)}.kill_refusals.jsonl"

    # -- writes ---------------------------------------------------------- #

    def register(self, record: ProcRecord) -> None:
        """Append one spawn record and fsync it before returning."""
        self._append(record)

    def mark_exited(self, pid: int, create_time: float | None = None) -> None:
        """Append a terminal marker for *pid* (no-op when already terminal)."""
        current = self.find(pid)
        if current is None or current.state != "live":
            return
        self._append(
            ProcRecord(
                run_id=current.run_id,
                pid=current.pid,
                create_time=(
                    current.create_time if create_time is None else create_time
                ),
                role=current.role,
                exe=current.exe,
                cmdline=current.cmdline,
                parent_pid=current.parent_pid,
                job_id=current.job_id,
                ts=time.time(),
                state="exited",
            ),
        )

    def record_refusal(self, command: str, target: str, reason: str) -> None:
        """Write one refused-kill line (the audit trail of the hard rule)."""
        payload = {
            "ts": time.time(),
            "run_id": self.run_id,
            "pid": os.getpid(),
            "command": command,
            "target": target,
            "reason": reason,
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.refusal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            logger.debug("could not append kill refusal", exc_info=True)

    def _append(self, record: ProcRecord) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(record.as_line() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            logger.warning("could not append process ledger record", exc_info=True)

    # -- reads ----------------------------------------------------------- #

    def records(self) -> list[ProcRecord]:
        """Latest record per pid, in first-seen order."""
        latest: dict[int, ProcRecord] = {}
        for raw in self._read_lines():
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            try:
                pid = int(data.get("pid") or 0)
                create_time = float(data.get("create_time") or 0.0)
            except (TypeError, ValueError):
                continue
            if pid <= 0:
                continue
            latest[pid] = ProcRecord(
                run_id=str(data.get("run_id") or self.run_id),
                pid=pid,
                create_time=create_time,
                role=str(data.get("role") or "child"),
                exe=str(data.get("exe") or ""),
                cmdline=str(data.get("cmdline") or ""),
                parent_pid=_opt_int(data.get("parent_pid")),
                job_id=(str(data["job_id"]) if data.get("job_id") else None),
                ts=float(data.get("ts") or 0.0),
                state=str(data.get("state") or "live"),
            )
        return list(latest.values())

    def live_records(self) -> list[ProcRecord]:
        return [record for record in self.records() if record.state == "live"]

    def find(self, pid: int) -> ProcRecord | None:
        for record in self.records():
            if record.pid == int(pid):
                return record
        return None

    def _read_lines(self) -> list[str]:
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except OSError:
            return []
        return [line for line in text.splitlines() if line.strip()]

    # -- reconciliation -------------------------------------------------- #

    def reconcile(self) -> tuple[list[ProcRecord], list[ProcRecord]]:
        """Classify live records against reality; NEVER kills anything.

        Returns ``(live, stale)``. A record is live only while a process with
        the same pid *and* create time exists; a recycled pid therefore
        classifies as stale instead of being mistaken for the original child.
        """
        live: list[ProcRecord] = []
        stale: list[ProcRecord] = []
        psutil = _psutil()
        for record in self.live_records():
            alive = False
            if psutil is not None:
                try:
                    proc = psutil.Process(record.pid)
                    alive = (
                        abs(proc.create_time() - record.create_time)
                        <= CREATE_TIME_TOLERANCE
                        and proc.is_running()
                    )
                except Exception:  # noqa: BLE001 - gone or denied means stale
                    alive = False
            if alive:
                live.append(record)
            else:
                stale.append(record)
                self.mark_exited(record.pid)
        return live, stale


def _opt_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _psutil():
    try:
        import psutil

        return psutil
    except Exception:  # noqa: BLE001 - optional at call time
        return None


__all__ = [
    "CREATE_TIME_TOLERANCE",
    "ProcRecord",
    "ProcessRegistry",
    "ledger_dir",
    "ledger_path",
    "refusal_log_path",
]
