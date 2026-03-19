"""
supervisor/opencode_runner.py

Drives opencode via its non-interactive CLI:

    opencode run "<prompt>" [--model <model>]

- stdin=DEVNULL  → guarantees no TTY, no interactive prompts
- All permissions auto-approved in `run` mode
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Generator, Optional

from .opencode_step_detector import (
    OpencodeStepDetector,
    Step,
    PhaseTransition,
    StepProgress,
)

logger = logging.getLogger(__name__)

# ── Executable resolution ─────────────────────────────────────────────────── #

_NAMES = ["opencode", "opencode.exe", "opencode.cmd", "opencode.bat"]

_WINDOWS_EXTRA_DIRS = [
    Path.home() / "AppData" / "Local" / "opencode",
    Path.home() / "AppData" / "Local" / "Programs" / "opencode",
    Path.home() / "AppData" / "Roaming" / "npm",
    Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / ".bin",
    Path.home() / ".local" / "bin",
    Path.home() / "bin",
    Path("C:/Program Files/opencode"),
    Path("C:/Program Files (x86)/opencode"),
    Path("C:/tools/opencode"),
]

_DOT_PATH_FILE = Path(__file__).parent.parent / ".opencode_path"
_DOT_MODEL_FILE = Path(__file__).parent.parent / ".opencode_model"


def find_opencode(explicit: str = "") -> str:
    explicit = str(explicit) if explicit is not None else ""
    if explicit.strip():
        return explicit.strip()

    if _DOT_PATH_FILE.exists():
        val = _DOT_PATH_FILE.read_text(encoding="utf-8").strip()
        if val:
            return val

    for name in _NAMES:
        found = shutil.which(name)
        if found:
            return found

    if sys.platform == "win32":
        for d in _WINDOWS_EXTRA_DIRS:
            if not d.exists():
                continue
            for name in _NAMES:
                c = d / name
                if c.exists():
                    return str(c)

    raise FileNotFoundError(
        "Cannot find the opencode executable.\n"
        "Run  python diagnose_opencode.py  to auto-detect it,\n"
        "or paste the full path into the 'opencode executable' field in the UI.\n"
        r"Common Windows location: C:\Users\<you>\AppData\Local\opencode\opencode.exe"
    )


# ── Result container ─────────────────────────────────────────────────────── #


class RunResult:
    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        timed_out: bool = False,
        exception: str = "",
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timed_out = timed_out
        self.exception = exception

    @property
    def output(self) -> str:
        """Combined output surfaced to the supervisor loop."""
        parts = []
        if self.exception:
            parts.append(f"[EXCEPTION] {self.exception}")
        if self.timed_out:
            parts.append("[TIMED OUT]")
        # opencode run prints work output to stdout; progress/errors to stderr
        if self.stdout.strip():
            parts.append(self.stdout.strip())
        if self.stderr.strip():
            parts.append(f"[stderr]\n{self.stderr.strip()}")
        if self.returncode not in (0, None):
            parts.append(f"[exit {self.returncode}]")
        return "\n".join(parts)

    @property
    def ok(self) -> bool:
        return not self.timed_out and not self.exception and self.returncode == 0

    def diagnostic(self) -> str:
        lines = [
            f"exit_code : {self.returncode}",
            f"timed_out : {self.timed_out}",
            f"exception : {self.exception or '(none)'}",
            f"stdout    : {len(self.stdout)} chars",
            f"stderr    : {len(self.stderr)} chars",
        ]
        if self.stdout.strip():
            lines.append(f"--- stdout ---\n{self.stdout[:1200]}")
        if self.stderr.strip():
            lines.append(f"--- stderr ---\n{self.stderr[:1200]}")
        return "\n".join(lines)


# ── Runner ────────────────────────────────────────────────────────────────── #


class OpencodeRunner:
    """
    One send()/start() call = one  opencode run "<prompt>"  subprocess.
    stdin is always DEVNULL so opencode never tries to open a TUI or wait for input.
    """

    def __init__(
        self,
        workspace: Path,
        opencode_model: Optional[str] = None,
        opencode_executable: str = "",
        timeout: int = 300,
        step_detector: Optional[OpencodeStepDetector] = None,
        on_step: Optional[Callable[[Step], None]] = None,
        on_transition: Optional[Callable[[PhaseTransition], None]] = None,
        on_progress: Optional[Callable[[StepProgress], None]] = None,
    ):
        self.workspace = workspace
        self.opencode_model = opencode_model
        self.opencode_executable = opencode_executable
        self.timeout = timeout

        self._last_result: Optional[RunResult] = None
        self._chars_exchanged: int = 0
        self._alive: bool = False

        if step_detector is not None:
            self._step_detector = step_detector
        else:
            self._step_detector = OpencodeStepDetector(
                step_callback=on_step,
                transition_callback=on_transition,
                progress_callback=on_progress,
            )

    @property
    def step_detector(self) -> OpencodeStepDetector:
        return self._step_detector

    def get_step_progress(self) -> StepProgress:
        return self._step_detector.progress

    def reset_step_detector(self) -> None:
        self._step_detector.reset()

    # ------------------------------------------------------------------ #

    def start(self, initial_prompt: str) -> None:
        self._alive = True
        self._prepare_workspace()
        self._run_prompt(initial_prompt)

    def send(self, message: str) -> None:
        if not self._alive:
            raise RuntimeError("OpencodeRunner has been stopped.")
        self._run_prompt(message)

    def read_output(self, timeout: Optional[int] = None) -> tuple[str, bool]:
        if self._last_result is None:
            return "", False
        return self._last_result.output, self._last_result.timed_out

    def last_diagnostic(self) -> str:
        return self._last_result.diagnostic() if self._last_result else "(no run yet)"

    def stop(self) -> None:
        self._alive = False

    @property
    def is_alive(self) -> bool:
        return self._alive

    @property
    def estimated_context_tokens(self) -> int:
        return self._chars_exchanged // 4

    # ------------------------------------------------------------------ #

    def _prepare_workspace(self) -> None:
        """
        Ensure the workspace exists and contains an opencode project marker
        so opencode anchors its project root here instead of walking up the
        directory tree to a parent folder.
        """
        self.workspace.mkdir(parents=True, exist_ok=True)

        # opencode looks for .opencode/ as its project root marker.
        # Create it if missing so opencode doesn't escape the workspace.
        oc_dir = self.workspace / ".opencode"
        oc_dir.mkdir(exist_ok=True)

        # Minimal config.json that tells opencode this is the project root
        # and disables permission prompts that would block non-interactive use.
        config_path = oc_dir / "config.json"
        if not config_path.exists():
            import json

            config_path.write_text(
                json.dumps({"autoapprove": True}, indent=2),
                encoding="utf-8",
            )
            logger.info("Created .opencode/config.json in workspace")

    def _run_prompt(self, prompt: str) -> None:
        exe = find_opencode(self.opencode_executable)
        cmd = self._build_cmd(exe, prompt)
        logger.info("CMD: %s", " ".join(cmd))

        # .cmd/.bat on Windows need shell=True
        use_shell = sys.platform == "win32" and exe.lower().endswith(
            (".cmd", ".bat", ".ps1")
        )

        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,  # ← kills TUI / interactive prompts
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self.workspace),
                timeout=self.timeout,
                env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
                shell=use_shell,
            )

            stdout = result.stdout or ""
            stderr = result.stderr or ""

            # Detect known fatal errors
            combined_lower = (stdout + stderr).lower()
            if (
                "unable to connect" in combined_lower
                or "is the computer able to access" in combined_lower
            ):
                stderr = (
                    "[OPENCODE CONFIG ERROR] opencode cannot reach the AI provider.\n"
                    "Fix: run 'opencode' interactively → configure a working provider,\n"
                    "or set the model in the UI 'opencode model' field.\n\n"
                    "Raw error:\n" + (stdout + stderr).strip()
                )
                stdout = ""

            self._last_result = RunResult(
                stdout=stdout,
                stderr=stderr,
                returncode=result.returncode,
            )
            logger.info(
                "opencode exit=%d  stdout=%d  stderr=%d",
                result.returncode,
                len(stdout),
                len(stderr),
            )
            if stderr.strip():
                logger.info("stderr: %s", stderr[:400])

        except subprocess.TimeoutExpired as exc:
            stdout_val = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout.decode("utf-8", errors="replace") if exc.stdout else "")
            stderr_val = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr.decode("utf-8", errors="replace") if exc.stderr else "")
            self._last_result = RunResult(
                stdout=stdout_val,
                stderr=stderr_val,
                returncode=-1,
                timed_out=True,
            )
            logger.warning("opencode timed out after %ds", self.timeout)

        except Exception as exc:
            self._last_result = RunResult(exception=str(exc), returncode=-1)
            logger.error("opencode launch error: %s", exc)

        self._chars_exchanged += len(prompt) + len(self._last_result.output)

    def _build_cmd(self, exe: str, prompt: str) -> list[str]:
        # opencode run "<prompt>" [--model <model>]
        cmd = [exe, "run", prompt]

        # Resolve model: explicit UI field > .opencode_model file
        model = str(self.opencode_model or "").strip()
        if not model and _DOT_MODEL_FILE.exists():
            model = _DOT_MODEL_FILE.read_text(encoding="utf-8").strip()
        if model:
            cmd += ["--model", model]

        return cmd

    def process_step_detection(self, output: str) -> Generator[dict, None, None]:
        for event in self._step_detector.process_output(output):
            yield event

    def get_step_events(self, output: str) -> list[dict]:
        events = []
        for event in self._step_detector.process_output(output):
            events.append(event)
        return events

    def get_current_phase(self) -> str:
        return self._step_detector.progress.phase.name.lower()

    def is_active(self) -> bool:
        return self._step_detector.is_progressing()

    def is_progressing(self) -> bool:
        return self._step_detector.is_progressing()

    def is_waiting_for_output(self) -> bool:
        return self._step_detector.is_waiting_for_output()

    def get_activity_state(self) -> str:
        return self._step_detector.get_activity_state()

    def get_step_summary(self) -> dict:
        progress = self._step_detector.progress
        return {
            "current_step": progress.current_step,
            "total_steps": progress.total_steps_estimate,
            "percentage": progress.percentage,
            "phase": progress.phase.name.lower(),
            "completed_phases": list(progress.completed_phases),
            "step_count": len(progress.steps),
        }
