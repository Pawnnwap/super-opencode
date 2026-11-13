"""Shared base for every process a run starts.

All the mechanics that made past cleanup dangerous live here exactly once:
the ledger registration, the containment scope, the timeout, the log
redirection, and the termination that goes through
:mod:`supervisor.runtime.process_control`. A concrete language wrapper only
supplies the four seams that actually differ — how to find the executable,
how to build its argv, what environment it needs, and where its logs belong.

This is the "相通之处写基类" half of the design; see
:mod:`supervisor.runtime.processes` for the per-language subclasses
(engine / MATLAB / Python / Rust / service).
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from supervisor.runtime.job_object import JobScope
from supervisor.runtime.process_control import (
    SpawnedProcess,
    close_scope,
    spawn_tracked,
    terminate_tree,
)
from supervisor.runtime.process_registry import ProcessRegistry

logger = logging.getLogger(__name__)


@dataclass
class ProcResult:
    """Outcome of a bounded, non-streaming child run."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    killed: bool = False
    duration_s: float = 0.0
    log_path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def summary(self, limit: int = 400) -> str:
        tail = (self.stdout or self.stderr or "").strip()
        if len(tail) > limit:
            tail = tail[-limit:]
        state = "timeout" if self.timed_out else f"exit {self.returncode}"
        return f"{state}: {tail}" if tail else state


class OwnedProcess(ABC):
    """One child process whose lifetime is provably owned by one run."""

    role: str = "child"

    def __init__(
        self,
        *,
        run_id: str,
        cwd: str | os.PathLike | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        log_dir: Path | None = None,
        tag: str = "",
    ):
        self.run_id = str(run_id or "")
        self.cwd = Path(cwd) if cwd is not None else None
        self._env = dict(env) if env else None
        self.timeout = timeout
        self.log_dir = Path(log_dir) if log_dir is not None else None
        self.tag = tag or self.role
        self._spawned: SpawnedProcess | None = None
        # Stem fixed at construction: default_logs() is called both when
        # opening (for redirection) and when reporting the result, and a
        # timestamp recomputed per call would name two different files.
        self._log_stem = f"{self.tag}_{time.strftime('%Y%m%d_%H%M%S')}"

    # -- subclass seams -------------------------------------------------- #

    @abstractmethod
    def resolve_exe(self) -> str:
        """Absolute path (or PATH-resolvable name) of the executable."""

    @abstractmethod
    def build_argv(self) -> list[str]:
        """Full argv *including* the executable at index 0."""

    def child_env(self, base: dict[str, str]) -> dict[str, str]:
        """Environment additions for the child (base is already a copy)."""
        return base

    def default_logs(self) -> tuple[Path, Path] | None:
        """(stdout, stderr) log files, or None to inherit pipes."""
        return None

    def log_paths(self, prefix: str) -> tuple[Path, Path] | None:
        """``<log_dir>/<prefix>_<stem>-out.log`` and ``...-err.log``."""
        if self.log_dir is None:
            return None
        stem = f"{prefix}_{self._log_stem}"
        return self.log_dir / f"{stem}.log", self.log_dir / f"{stem}.err.log"

    # -- template methods ------------------------------------------------ #

    def base_env(self) -> dict[str, str]:
        """A copy of the current environment plus the run-scoped markers."""
        env = {**os.environ, **(self._env or {})}
        env.setdefault("SOP_RUN_ID", self.run_id)
        env.setdefault("SOP_PROC_LEDGER", str(self._ledger_hint()))
        return self.child_env(env)

    def _ledger_hint(self) -> Path:
        from supervisor.runtime.process_registry import ledger_path

        return ledger_path(self.run_id) if self.run_id else Path("")

    def open(self, *, job: JobScope | None = None, **popen_kwargs) -> SpawnedProcess:
        """Spawn inside the run's scope; the ledger record is written first."""
        argv = self.build_argv()
        logs = self.default_logs()
        handles: list = []
        if logs is not None:
            out_path, err_path = logs
            for path in (out_path, err_path):
                path.parent.mkdir(parents=True, exist_ok=True)
            out = out_path.open("ab")
            err = err_path.open("ab")
            handles.extend([out, err])
            popen_kwargs.setdefault("stdout", out)
            popen_kwargs.setdefault("stderr", err)
        popen_kwargs.setdefault("stdin", subprocess.DEVNULL)
        try:
            spawned = spawn_tracked(
                run_id=self.run_id,
                role=self.role,
                argv=argv,
                cwd=self.cwd,
                env=self.base_env() if self._env is not None or self.run_id else None,
                job=job,
                **popen_kwargs,
            )
        except Exception:
            for handle in handles:
                handle.close()
            raise
        # The child owns the handles now; closing ours is what lets the file
        # be rotated after the process exits.
        for handle in handles:
            handle.close()
        self._spawned = spawned
        logger.info(
            "spawned %s pid=%s run=%s: %s",
            self.role,
            spawned.pid,
            self.run_id,
            " ".join(argv)[:300],
        )
        return spawned

    def run(self, *, timeout: float | None = None) -> ProcResult:
        """Run to completion under the ledger's ownership; kills on timeout."""
        spawned = self.open()
        limit = timeout if timeout is not None else self.timeout
        start = time.monotonic()
        timed_out = False
        try:
            stdout, stderr = spawned.popen.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate(reason=f"timeout {limit}s")
            try:
                stdout, stderr = spawned.popen.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
        duration = time.monotonic() - start
        if spawned.record.run_id:
            # The child is reaped; close its ledger record so a later reboot
            # cannot mistake it for a live leftover.
            ProcessRegistry(spawned.record.run_id).mark_exited(spawned.record.pid)
        logs = self.default_logs()
        text_out = _as_text(stdout)
        if not text_out and logs is not None:
            text_out = _tail(logs[0])
        return ProcResult(
            returncode=int(spawned.popen.returncode if spawned.popen.returncode is not None else -1),
            stdout=text_out,
            stderr=_as_text(stderr) or (_tail(logs[1]) if logs is not None else ""),
            timed_out=timed_out,
            killed=timed_out,
            duration_s=duration,
            log_path=logs[0] if logs else None,
        )

    def terminate(self, reason: str = "") -> bool:
        """Terminate this child's tree through the ownership gate."""
        return self._terminate(reason=reason)

    def _terminate(self, *, reason: str) -> bool:
        if self._spawned is None:
            return False
        return terminate_tree(
            self.run_id,
            int(self._spawned.record.pid),
            reason=f"{self.role}: {reason}" if reason else self.role,
            expected_create_time=self._spawned.record.create_time,
        )

    def release(self) -> None:
        """Drop the scope after a normal exit (members keep their own life)."""
        if self._spawned is not None:
            self._spawned = None
        close_scope(self.run_id)


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _tail(path: Path, limit: int = 4000) -> str:
    """Last chunk of a redirected log (stdout is a file, not a pipe)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


__all__ = ["OwnedProcess", "ProcResult"]
