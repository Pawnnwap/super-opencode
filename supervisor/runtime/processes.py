"""Per-language process wrappers: the concrete half of the ownership design.

Every class here inherits the ledger + containment + timeout + termination
mechanics from :class:`~supervisor.runtime.process_base.OwnedProcess` and
supplies only what differs for its language:

==============  ==========================================================
EngineProcess    the agent CLI (opencode / codex / pi); opened, not run —
                 the streaming consumer owns the pipes.
MatlabProcess    ``matlab -batch <script>``. ``bin\\matlab.exe`` is a
                 launcher: the compute runs in a spawned ``MATLAB.exe``, so
                 the pair can only be cleaned up as a group.
PythonProcess    ``python -u <script>`` / ``python -m <module>``: scripts,
                 pytest, py_compile, conda-env interpreters.
RustProcess      ``cargo build|test|run|check``; ``rustc`` and the test
                 binary it spawns are descendants of the job root.
ServiceProcess   the long-lived stack pieces (app / proxy / worker).
==============  ==========================================================

The containment is structural: whatever these roots spawn — a conda env's
``python.exe``, ``rustc``, a test executable, ``MATLAB.exe``, a shell the
agent starts — is created inside the run's job object and therefore in scope.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import sys
from pathlib import Path

from supervisor.runtime.process_base import OwnedProcess

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Engine (opencode / codex / pi)
# --------------------------------------------------------------------------- #


class EngineProcess(OwnedProcess):
    """The agent CLI turn: argv and env come from the engine's own builder."""

    role = "engine"

    def __init__(
        self,
        *,
        run_id: str,
        argv: list[str],
        cwd: str | os.PathLike | None = None,
        env: dict[str, str] | None = None,
        shell: bool = False,
    ):
        super().__init__(run_id=run_id, cwd=cwd, env=env)
        self._argv = list(argv)
        self.shell = bool(shell)

    def resolve_exe(self) -> str:
        return self._argv[0] if self._argv else ""

    def build_argv(self) -> list[str]:
        return list(self._argv)

    def open(self, *, job=None, **popen_kwargs):  # type: ignore[override]
        """Streaming engines need pipes and no log redirection."""
        popen_kwargs.setdefault("stdin", None)
        popen_kwargs.setdefault("stdout", None)
        popen_kwargs.setdefault("stderr", None)
        # npm `.cmd`/`.bat` shims only start through cmd.exe on Windows.
        popen_kwargs.setdefault("shell", self.shell)
        return super().open(job=job, **popen_kwargs)

    def child_env(self, base: dict[str, str]) -> dict[str, str]:
        base.setdefault("NO_COLOR", "1")
        base.setdefault("TERM", "dumb")
        return base


# --------------------------------------------------------------------------- #
# MATLAB
# --------------------------------------------------------------------------- #


class MatlabProcess(OwnedProcess):
    """``matlab -batch <script>`` — the only sanctioned MATLAB entry point.

    ``-batch`` makes MATLAB run the statement, print to stdout, and exit with
    a status, so no GUI/desktop session is involved. The launcher process
    (``bin\\matlab.exe``) forks the real engine (``MATLAB.exe``); both are job
    members, so ``terminate`` takes the pair and nothing else.
    """

    role = "matlab"

    def __init__(
        self,
        *,
        run_id: str,
        script: str,
        cwd: str | os.PathLike,
        matlab_exe: str | None = None,
        timeout: float | None = 3600,
        log_dir: Path | None = None,
    ):
        super().__init__(
            run_id=run_id,
            cwd=cwd,
            timeout=timeout,
            log_dir=log_dir or (Path(cwd) / "logs"),
            tag=_safe_tag(script),
        )
        self.script = script
        self._exe = (matlab_exe or "").strip()

    def resolve_exe(self) -> str:
        if self._exe:
            return self._exe
        self._exe = find_matlab()
        return self._exe

    def build_argv(self) -> list[str]:
        return [self.resolve_exe(), "-batch", self.script]

    def default_logs(self) -> tuple[Path, Path] | None:
        return self.log_paths("matlab")


def find_matlab() -> str:
    """Locate ``matlab.exe``: env override, PATH, then installed releases."""
    override = (os.environ.get("SOP_MATLAB_EXE") or "").strip()
    if override and Path(override).is_file():
        return override
    found = shutil.which("matlab")
    if found:
        return found
    patterns = (
        r"C:\Program Files\MATLAB\R20*\bin\matlab.exe",
        r"C:\Program Files\Polyspace\R20*\bin\matlab.exe",
        r"C:\Program Files (x86)\MATLAB\R20*\bin\matlab.exe",
    )
    candidates: list[str] = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))
    if candidates:
        # Newest release first: the version string sorts lexicographically.
        return max(candidates)
    raise FileNotFoundError(
        "matlab.exe not found: install MATLAB or set SOP_MATLAB_EXE",
    )


# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #


class PythonProcess(OwnedProcess):
    """A Python child: a script, a module (pytest), or an inline statement."""

    role = "python"

    def __init__(
        self,
        *,
        run_id: str,
        script: str = "",
        module: str = "",
        args: list[str] | None = None,
        code: str = "",
        cwd: str | os.PathLike | None = None,
        python_exe: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        log_dir: Path | None = None,
        tag: str = "",
    ):
        super().__init__(
            run_id=run_id,
            cwd=cwd,
            env=env,
            timeout=timeout,
            log_dir=log_dir,
            tag=tag or _safe_tag(script or module or "inline"),
        )
        if not (script or module or code):
            raise ValueError("PythonProcess needs a script, a module, or code")
        self.script = script
        self.module = module
        self.code = code
        self.args = list(args or [])
        self._exe = (python_exe or "").strip()

    def resolve_exe(self) -> str:
        if self._exe:
            return self._exe
        override = (os.environ.get("SOP_PYTHON_EXE") or "").strip()
        if override and Path(override).is_file():
            self._exe = override
        else:
            self._exe = sys.executable
        return self._exe

    def build_argv(self) -> list[str]:
        argv = [self.resolve_exe(), "-u"]
        if self.code:
            argv.extend(["-c", self.code])
        elif self.module:
            argv.extend(["-m", self.module])
        else:
            argv.append(self.script)
        argv.extend(self.args)
        return argv

    def child_env(self, base: dict[str, str]) -> dict[str, str]:
        base.setdefault("PYTHONIOENCODING", "utf-8")
        base.setdefault("PYTHONUNBUFFERED", "1")
        return base

    def default_logs(self) -> tuple[Path, Path] | None:
        return self.log_paths("python")


# --------------------------------------------------------------------------- #
# Rust
# --------------------------------------------------------------------------- #


class RustProcess(OwnedProcess):
    """``cargo <subcommand>``; rustc and test binaries inherit the job."""

    role = "rust"

    def __init__(
        self,
        *,
        run_id: str,
        subcommand: str = "build",
        args: list[str] | None = None,
        cwd: str | os.PathLike | None = None,
        cargo_exe: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        log_dir: Path | None = None,
    ):
        super().__init__(
            run_id=run_id,
            cwd=cwd,
            env=env,
            timeout=timeout,
            log_dir=log_dir,
            tag=_safe_tag(subcommand or "build"),
        )
        self.subcommand = subcommand
        self.args = list(args or [])
        self._exe = (cargo_exe or "").strip()

    def resolve_exe(self) -> str:
        if self._exe:
            return self._exe
        override = (os.environ.get("SOP_CARGO_EXE") or "").strip()
        if override and Path(override).is_file():
            self._exe = override
            return self._exe
        found = shutil.which("cargo")
        if found:
            self._exe = found
            return found
        home = Path.home() / ".cargo" / "bin"
        for name in ("cargo.exe", "cargo"):
            candidate = home / name
            if candidate.is_file():
                self._exe = str(candidate)
                return self._exe
        raise FileNotFoundError("cargo not found: install Rust or set SOP_CARGO_EXE")

    def build_argv(self) -> list[str]:
        argv = [self.resolve_exe(), self.subcommand, *self.args]
        return argv

    def default_logs(self) -> tuple[Path, Path] | None:
        return self.log_paths("cargo")


# --------------------------------------------------------------------------- #
# Service stack
# --------------------------------------------------------------------------- #


class ServiceProcess(OwnedProcess):
    """One long-lived stack piece (app / proxy / worker), python-run."""

    role = "service"

    def __init__(
        self,
        *,
        run_id: str = "service",
        argv: list[str],
        cwd: str | os.PathLike | None = None,
        env: dict[str, str] | None = None,
        log_dir: Path | None = None,
        tag: str = "service",
    ):
        super().__init__(run_id=run_id, cwd=cwd, env=env, log_dir=log_dir, tag=tag)
        self._argv = list(argv)

    def resolve_exe(self) -> str:
        return self._argv[0] if self._argv else ""

    def build_argv(self) -> list[str]:
        return list(self._argv)

    def default_logs(self) -> tuple[Path, Path] | None:
        if self.log_dir is None:
            return None
        return self.log_dir / f"{self.tag}.out.log", self.log_dir / f"{self.tag}.err.log"


def _safe_tag(text: str) -> str:
    name = Path(str(text).split()[0] if str(text).strip() else "proc").name
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)
    return cleaned[:60] or "proc"


__all__ = [
    "EngineProcess",
    "MatlabProcess",
    "PythonProcess",
    "RustProcess",
    "ServiceProcess",
    "find_matlab",
]
