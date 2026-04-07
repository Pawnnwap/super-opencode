"""supervisor/opencode_runner.py

Drives opencode via its non-interactive CLI:

    opencode run "<prompt>" [--model <model>]

- stdin=DEVNULL  → guarantees no TTY, no interactive prompts
- All permissions auto-approved in `run` mode
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from pathlib import Path

from supervisor.analyzers.opencode_step_detector import (OpencodeStepDetector,
                                                         PhaseTransition, Step,
                                                         StepProgress)
from supervisor.prompts.commands import BREVITY_COMMAND
from supervisor.utils.text_utils import strip_thinking_blocks
from supervisor.workspace.workspace_archiver import (ArchiveResult,
                                                     WorkspaceArchiver)

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

_DOT_MODEL_FILE = Path(__file__).parent.parent / ".opencode_model"


def _coerce_str(value: object, field_name: str) -> str:
    """Coerce *value* to a stripped string, logging a warning when the raw type
    is not already ``str`` so the caller knows where bad data entered the system.

    Returns an empty string for ``None`` and falsy values.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        logger.warning(
            "Type coercion: field '%s' received %r (type=%s) — expected str. "
            "Converting automatically. Check the caller / UI widget that produced this value.",
            field_name,
            value,
            type(value).__name__,
        )
        value = str(value)
    return value.strip()


def find_opencode(explicit: str = "") -> str:
    """Locate the opencode executable by running 'where opencode' and picking
    the result that contains 'chocolatey\\bin'.

    Raises FileNotFoundError with actionable instructions if nothing is found.
    """
    explicit = _coerce_str(explicit, "opencode_executable (find_opencode arg)")

    if explicit:
        p = Path(explicit)
        if p.is_file():
            logger.debug("Using explicit opencode path: %s", explicit)
            return explicit
        logger.warning(
            "Explicit opencode path '%s' does not exist — falling back to auto-detection.",
            explicit,
        )

    try:
        result = subprocess.run(
            ["where", "opencode"], capture_output=True, text=True, check=True,
        )
        for line in result.stdout.splitlines():
            if "chocolatey\\bin" in line.lower():
                path = line.strip()
                if Path(path).is_file():
                    logger.info("Found opencode.exe at %s", path)
                    return path
    except subprocess.CalledProcessError:
        pass

    raise FileNotFoundError(
        "opencode.exe not found in Chocolatey.\n\n"
        "To fix:\n"
        "  • Run (as Administrator):  choco install opencode\n"
        "  • Then restart the Streamlit app so it picks up the updated PATH.",
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
    """One send()/start() call = one  opencode run "<prompt>"  subprocess.
    stdin is always DEVNULL so opencode never tries to open a TUI or wait for input.
    Supports --continue flag for session continuity when context allows.
    """

    def __init__(
        self,
        workspace: Path,
        timeout: int,
        opencode_model: str | None = None,
        opencode_executable: str = "",
        agent: str = "",
        opencode_model_backup: str | None = None,
        step_detector: OpencodeStepDetector | None = None,
        on_step: Callable[[Step], None] | None = None,
        on_transition: Callable[[PhaseTransition], None] | None = None,
        on_progress: Callable[[StepProgress], None] | None = None,
    ):
        # ── Coerce and validate all user-supplied string inputs up front ── #
        # Log raw types so we immediately know if a UI widget passed the wrong type
        logger.debug(
            "__init__ raw inputs — "
            "opencode_model=%r (type=%s)  "
            "opencode_executable=%r (type=%s)  "
            "agent=%r (type=%s)  "
            "opencode_model_backup=%r (type=%s)  "
            "timeout=%r (type=%s)",
            opencode_model, type(opencode_model).__name__,
            opencode_executable, type(opencode_executable).__name__,
            agent, type(agent).__name__,
            opencode_model_backup, type(opencode_model_backup).__name__,
            timeout, type(timeout).__name__,
        )

        self.workspace = workspace

        # Coerce model strings — a float like 3.5 from a number widget is the
        # most common source of the "unsupported operand type(s) for +: float and str" error.
        raw_model = _coerce_str(opencode_model, "opencode_model")
        self.opencode_model: str | None = raw_model or None

        raw_backup = _coerce_str(opencode_model_backup, "opencode_model_backup")
        self.opencode_model_backup: str | None = raw_backup or None

        self.opencode_executable = _coerce_str(opencode_executable, "opencode_executable")
        self.agent = _coerce_str(agent, "agent")

        # timeout must be an int; guard against float from UI sliders
        if not isinstance(timeout, int):
            logger.warning(
                "Type coercion: 'timeout' received %r (type=%s) — casting to int.",
                timeout, type(timeout).__name__,
            )
        self.timeout = int(timeout)

        logger.info(
            "__init__ coerced values — "
            "opencode_model=%r  opencode_model_backup=%r  "
            "agent=%r  timeout=%d  workspace=%s",
            self.opencode_model,
            self.opencode_model_backup,
            self.agent,
            self.timeout,
            self.workspace,
        )

        self._last_result: RunResult | None = None
        self._chars_exchanged: int = 0
        self._alive: bool = False
        self._process: subprocess.Popen | None = None
        self._archiver = WorkspaceArchiver(workspace)
        self._session_active: bool = False
        self._use_continue: bool = False

        if step_detector is not None:
            self._step_detector = step_detector
        else:
            self._step_detector = OpencodeStepDetector(
                step_callback=on_step,
                transition_callback=on_transition,
                progress_callback=on_progress,
            )

    @classmethod
    def from_config(cls, config, agent: str = "") -> OpencodeRunner:
        """Factory method to create a runner from a SupervisorConfig object."""
        return cls(
            workspace=config.workspace,
            timeout=config.timeout,
            opencode_model=config.opencode_model,
            opencode_executable=config.opencode_executable,
            agent=agent,
            opencode_model_backup=config.opencode_model_backup,
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
        initial_prompt = _coerce_str(initial_prompt, "initial_prompt (start)")
        if not initial_prompt:
            logger.warning("Empty prompt provided to opencode. Skipping run.")
            self._last_result = RunResult(
                exception="Empty prompt provided. Skipping run.",
            )
            return

        if not self._session_active:
            logger.info("New session detected. Sending brevity command...")
            self._run_prompt(BREVITY_COMMAND)
            self._session_active = True
            self.enable_continuation(True)

        logger.info("start() — prompt length=%d chars", len(initial_prompt))
        self._alive = True
        self._prepare_workspace()
        self._run_prompt(initial_prompt)

    def send(self, message: str) -> None:
        message = _coerce_str(message, "message (send)")
        if not self._alive:
            raise RuntimeError("OpencodeRunner has been stopped.")

        if self._session_active:
            self.enable_continuation(True)

        logger.info("send() — message length=%d chars", len(message))
        self._run_prompt(message)

    def read_output(self, timeout: int | None = None) -> tuple[str, bool]:
        if self._last_result is None:
            return "", False
        return strip_thinking_blocks(self._last_result.output), self._last_result.timed_out

    def last_diagnostic(self) -> str:
        return self._last_result.diagnostic() if self._last_result else "(no run yet)"

    def stop(self) -> None:
        self._alive = False
        self._session_active = False
        if self._process is not None:
            try:
                self._process.kill()
            except Exception as exc:
                logger.warning("Error killing process: %s", exc)

        self._kill_chocolatey_processes()

    def _kill_chocolatey_processes(self) -> None:
        """Kill processes that have 'chocolatey', 'choco', or 'opencode' in their names."""
        try:
            system = platform.system()
            if system == "Windows":
                # Primary method: Use tasklist /fo csv
                try:
                    result = subprocess.run(
                        ["tasklist", "/fo", "csv"],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    found_processes = False
                    for line in result.stdout.splitlines()[1:]:  # Skip header
                        if line.strip():
                            parts = line.split('","')
                            if len(parts) >= 1:
                                process_name = parts[0].strip('"').lower()
                                if any(
                                    keyword in process_name
                                    for keyword in ["chocolatey", "choco", "opencode"]
                                ):
                                    if len(parts) >= 2:
                                        pid = parts[1].strip('"')
                                        try:
                                            subprocess.run(
                                                ["taskkill", "/PID", pid, "/F"],
                                                capture_output=True,
                                                text=True,
                                                timeout=2,
                                            )
                                            found_processes = True
                                        except Exception as e:
                                            logger.warning(
                                                "Error killing process %s: %s", pid, e,
                                            )
                    if not found_processes:
                        logger.debug("No chocolatey/opencode processes found to kill")
                    return
                except subprocess.TimeoutExpired:
                    logger.debug("Primary process scan timed out, using fallback method")
                except Exception as e:
                    logger.warning("Primary process scan failed: %s", e)

                # Fallback: plain tasklist
                try:
                    result = subprocess.run(
                        ["tasklist"], capture_output=True, text=True, timeout=2,
                    )
                    found_processes = False
                    for line in result.stdout.splitlines()[3:]:
                        parts = line.split()
                        if len(parts) >= 2:
                            process_name = parts[0].lower()
                            if any(
                                keyword in process_name
                                for keyword in ["chocolatey", "choco", "opencode"]
                            ):
                                pid = parts[1]
                                try:
                                    subprocess.run(
                                        ["taskkill", "/PID", pid, "/F"],
                                        capture_output=True,
                                        text=True,
                                        timeout=2,
                                    )
                                    found_processes = True
                                except Exception as e:
                                    logger.warning(
                                        "Error killing process %s: %s", pid, e,
                                    )
                    if not found_processes:
                        logger.debug("No processes found with fallback method")
                except Exception as e:
                    logger.warning("Fallback process scan failed: %s", e)
            else:
                # Unix-like systems
                try:
                    subprocess.run(
                        ["pkill", "-f", "-i", "chocolatey"],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                except subprocess.TimeoutExpired:
                    logger.debug("Unix pkill command timed out")
                except Exception as e:
                    logger.warning("Unix pkill failed: %s", e)
        except Exception as e:
            logger.warning("Error in chocolatey process killing: %s", e)

    @property
    def is_alive(self) -> bool:
        return self._alive

    @property
    def estimated_context_tokens(self) -> int:
        return self._chars_exchanged // 4

    # ------------------------------------------------------------------ #

    def _prepare_workspace(self) -> None:
        """Ensure the workspace exists and contains an opencode project marker."""
        self.workspace.mkdir(parents=True, exist_ok=True)

        oc_dir = self.workspace / ".opencode"
        oc_dir.mkdir(exist_ok=True)

        config_path = oc_dir / "config.json"
        if not config_path.exists():
            import json
            config_path.write_text(
                json.dumps({"autoapprove": True}, indent=2),
                encoding="utf-8",
            )
            logger.info("Created .opencode/config.json in workspace")

    def _run_prompt(self, prompt: str) -> None:
        # Defensive coercion — should already be clean but belt-and-suspenders
        prompt = _coerce_str(prompt, "prompt (_run_prompt)")

        exe = find_opencode(self.opencode_executable)
        using_backup = False

        while True:
            model_for_cmd = self.opencode_model_backup if using_backup else self.opencode_model

            # ── Log exactly what we are about to pass to _build_cmd ── #
            logger.debug(
                "_run_prompt pre-build — "
                "using_backup=%s  model_for_cmd=%r (type=%s)  "
                "prompt_len=%d  agent=%r",
                using_backup,
                model_for_cmd,
                type(model_for_cmd).__name__,
                len(prompt),
                self.agent,
            )

            cmd = self._build_cmd(exe, prompt, model=model_for_cmd)
            logger.info("CMD: %s", " ".join(cmd))

            use_shell = sys.platform == "win32" and exe.lower().endswith(
                (".cmd", ".bat", ".ps1"),
            )

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(self.workspace),
                    env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
                    shell=use_shell,
                )

                try:
                    stdout, stderr = self._process.communicate(timeout=self.timeout)
                    returncode = self._process.returncode
                except subprocess.TimeoutExpired:
                    stdout_val = self._process.stdout.read() if self._process.stdout else ""
                    stderr_val = self._process.stderr.read() if self._process.stderr else ""

                    stdout_val = (
                        stdout_val.decode("utf-8", errors="replace")
                        if isinstance(stdout_val, bytes)
                        else (stdout_val or "")
                    )
                    stderr_val = (
                        stderr_val.decode("utf-8", errors="replace")
                        if isinstance(stderr_val, bytes)
                        else (stderr_val or "")
                    )

                    self._last_result = RunResult(
                        stdout=stdout_val,
                        stderr=stderr_val,
                        returncode=-1,
                        timed_out=True,
                    )
                    logger.warning("opencode timed out after %ds", self.timeout)

                    if not using_backup and self.opencode_model_backup:
                        logger.warning(
                            "Primary model %r timed out, falling back to backup %r",
                            self.opencode_model,
                            self.opencode_model_backup,
                        )
                        using_backup = True
                        continue

                    self._chars_exchanged += len(prompt) + len(self._last_result.output)
                    return

                stdout = stdout or ""
                stderr = stderr or ""

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
                    returncode=returncode,
                )
                logger.info(
                    "opencode exit=%d  stdout=%d chars  stderr=%d chars",
                    returncode,
                    len(stdout),
                    len(stderr),
                )
                if stderr.strip():
                    logger.info("stderr snippet: %s", stderr[:400])

                if not self._last_result.ok and not using_backup and self.opencode_model_backup:
                    logger.warning(
                        "Primary model %r failed (exit=%d), falling back to backup %r",
                        self.opencode_model,
                        returncode,
                        self.opencode_model_backup,
                    )
                    using_backup = True
                    continue

            except Exception as exc:
                time.sleep(3)
                logger.error(
                    "opencode launch error — exc=%s  using_backup=%s  "
                    "model_for_cmd=%r (type=%s)  prompt_snippet=%r  agent=%r",
                    exc,
                    using_backup,
                    model_for_cmd,
                    type(model_for_cmd).__name__,
                    prompt[:120],
                    self.agent,
                )
                if not using_backup and self.opencode_model_backup:
                    logger.warning(
                        "Falling back to backup model %r after launch error on primary %r",
                        self.opencode_model_backup,
                        self.opencode_model,
                    )
                    using_backup = True
                    continue
                self._last_result = RunResult(exception=str(exc), returncode=-1)

            self._chars_exchanged += len(prompt) + len(self._last_result.output)
            return

    def enable_continuation(self, enabled: bool = True) -> None:
        """Enable or disable --continue flag for the next run."""
        self._use_continue = enabled

    def is_continuation_enabled(self) -> bool:
        return self._use_continue

    def mark_session_active(self) -> None:
        self._session_active = True

    def reset_session(self) -> None:
        self._session_active = False
        self._use_continue = False

    def reset_context_counter(self) -> None:
        self._chars_exchanged = 0

    def _build_cmd(self, exe: str, prompt: str, model: str | None = None) -> list[str]:
        """Build the opencode CLI command list.

        Every value is coerced to ``str`` here as a final safety net, and the
        resolved values are logged at DEBUG level so any future type surprises
        are immediately visible in the log.
        """
        exe = _coerce_str(exe, "exe (_build_cmd)")
        prompt = _coerce_str(prompt, "prompt (_build_cmd)")
        agent = _coerce_str(self.agent, "self.agent (_build_cmd)")

        # Resolve model with explicit coercion at every step
        raw_model_arg = _coerce_str(model, "model arg (_build_cmd)")
        raw_self_model = _coerce_str(self.opencode_model, "self.opencode_model (_build_cmd)")
        resolved_model = raw_model_arg or raw_self_model

        if not resolved_model and _DOT_MODEL_FILE.exists():
            resolved_model = _DOT_MODEL_FILE.read_text(encoding="utf-8").strip()
            logger.debug("Model resolved from .opencode_model file: %r", resolved_model)

        logger.debug(
            "_build_cmd — exe=%r  agent=%r  use_continue=%s  "
            "model_arg=%r  self.opencode_model=%r  resolved_model=%r  "
            "prompt_len=%d",
            exe,
            agent,
            self._use_continue,
            raw_model_arg,
            raw_self_model,
            resolved_model,
            len(prompt),
        )

        cmd: list[str] = [exe, "run"]

        if agent:
            cmd += ["--agent", agent]

        if self._use_continue:
            cmd.append("--continue")

        cmd.append(prompt)

        if resolved_model:
            cmd += ["--model", resolved_model]

        return cmd

    def process_step_detection(self, output: str) -> Generator[dict, None, None]:
        for event in self._step_detector.process_output(output):
            yield event

    def get_step_events(self, output: str) -> list[dict]:
        return list(self._step_detector.process_output(output))

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

    def send_cleanup_inquiry(self, candidates: list[str]) -> None:
        """Send an inquiry to opencode about identified cleanup candidates."""
        if not candidates:
            return

        workspace_rel = (
            self.workspace.relative_to(self.workspace)
            if self.workspace.is_absolute()
            else self.workspace
        )
        inquiry = (
            f"You are working in workspace: {workspace_rel}\n\n"
            f"I have identified the following files that may be outdated or unused:\n"
        )
        for i, candidate in enumerate(candidates, 1):
            inquiry += f"  {i}. {candidate}\n"

        inquiry += (
            "\nPlease analyze these files and respond with a JSON list of file paths "
            "that should be archived. These files will be moved to .archive/ "
            "instead of being deleted, preserving historical versions.\n"
            "Consider:\n"
            "- Files that are clearly temporary, backup, or cache files\n"
            "- Files that are not referenced by other code\n"
            "- Files that appear to be duplicate or superseded versions\n"
            "- Any __pycache__ directories\n\n"
            "IMPORTANT: Never select protected paths (.opencode/, .checkpoints/, .archive/) "
            "for archiving.\n\n"
            "Respond ONLY with a JSON array of file paths to archive, nothing else. "
            'Example: ["file1.bak", "file2.tmp"]'
        )

        logger.info(
            "Sending cleanup inquiry to opencode for %d candidates", len(candidates),
        )
        self.send(inquiry)

    def identify_cleanup_candidates(self) -> list[str]:
        """Identify files that might be outdated or unused."""
        import re

        candidates: list[str] = []
        workspace = self.workspace

        _VERSION_PATTERNS = [
            re.compile(r"\.bak$"),
            re.compile(r"\.backup$"),
            re.compile(r"\.old$"),
            re.compile(r"\.orig$"),
            re.compile(r"\.tmp$"),
            re.compile(r"~\d+$"),
            re.compile(r"\.v\d+$"),
            re.compile(r"_backup_\d+$"),
            re.compile(r"_old_\d+$"),
            re.compile(r"\.\d+$"),
        ]

        _SOURCE_EXTS = {
            ".py", ".pyc", ".pyo", ".pyd",
            ".md", ".txt", ".rst",
            ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
            ".js", ".ts", ".jsx", ".tsx", ".css", ".scss",
            ".html", ".xml", ".sh", ".bat", ".ps1",
        }

        def should_ignore(path: Path) -> bool:
            if not path.is_file():
                if not (path.is_dir() and path.name == "__pycache__"):
                    return True
            rel = path.relative_to(workspace)
            if ".checkpoints" in rel.parts:
                return True
            if path == workspace / ".checkpoints":
                return True
            ignore_dirs = {".git", ".venv", "venv", "node_modules", ".mypy_cache", ".opencode"}
            if any(part in ignore_dirs for part in rel.parts):
                return True
            return False

        def is_versioned_backup(name: str) -> bool:
            return any(p.search(name) for p in _VERSION_PATTERNS)

        def get_base_name(path: Path) -> str:
            base = path.name
            changed = True
            while changed:
                changed = False
                for pattern in _VERSION_PATTERNS:
                    new_base = pattern.sub("", base)
                    if new_base != base:
                        base = new_base
                        changed = True
                        break
            return base

        candidates.extend(
            self._identify_versioned_backups(
                workspace, should_ignore, is_versioned_backup, get_base_name,
            ),
        )
        candidates.extend(
            self._identify_orphaned_files(workspace, should_ignore, _SOURCE_EXTS),
        )

        if candidates:
            self.send_cleanup_inquiry(candidates)

        return candidates

    def _identify_versioned_backups(
        self,
        workspace: Path,
        should_ignore,
        is_versioned_backup,
        get_base_name,
    ) -> list[str]:
        candidates: list[str] = []
        backup_groups: dict[str, list[Path]] = {}
        all_files: dict[str, Path] = {}

        for path in workspace.rglob("*"):
            if should_ignore(path):
                continue
            all_files[path.name] = path
            if is_versioned_backup(path.name):
                base = get_base_name(path)
                backup_groups.setdefault(base, []).append(path)

        for base_name, backups in backup_groups.items():
            if base_name in all_files:
                backups.append(all_files[base_name])
            backups_sorted = sorted(backups, key=lambda p: len(p.name))
            for backup in backups_sorted[1:]:
                candidates.append(str(backup.relative_to(workspace)))

        return candidates

    def _identify_orphaned_files(
        self,
        workspace: Path,
        should_ignore,
        source_exts: set,
    ) -> list[str]:
        import re

        candidates: list[str] = []
        import_patterns = [
            (re.compile(r"^(?:from|import)\s+([\w.]+)", re.MULTILINE), "py"),
            (re.compile(r'require\s*\(\s*["\']([^"\']+)["\']\s*\)', re.MULTILINE), "js"),
            (re.compile(r'import\s+.*?from\s+["\']([^"\']+)["\']', re.MULTILINE), "js"),
            (re.compile(r'#include\s*["<]([^">]+)[">]', re.MULTILINE), "c"),
        ]

        referenced_paths: set[str] = set()
        for path in workspace.rglob("*"):
            if should_ignore(path):
                continue
            if path.suffix not in source_exts and not path.name.endswith(".h"):
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
                for pattern, ptype in import_patterns:
                    for match in pattern.finditer(content):
                        ref = match.group(1)
                        if ptype == "py":
                            ref = ref.replace(".", "/")
                            if not ref.endswith(".py"):
                                ref += ".py"
                        referenced_paths.add(ref)
            except Exception:
                pass

        for path in workspace.rglob("*"):
            if should_ignore(path):
                continue
            rel_str = str(path.relative_to(workspace))

            if path.is_dir() and path.name == "__pycache__":
                candidates.append(rel_str)
                continue

            if path.suffix in {".pyc", ".pyo", ".pyc.tmp"} or path.name.endswith(".pyc"):
                candidates.append(rel_str)
                continue

        return candidates

    def archive_files(self, files: list[str]) -> ArchiveResult:
        return self._archiver.archive_workspace(label="cleanup", files_to_archive=files)

    def archive_before_new_run(self) -> ArchiveResult:
        return self._archiver.archive_before_new_run()

    def get_archiver(self) -> WorkspaceArchiver:
        return self._archiver

    def list_archives(self) -> list[dict]:
        return self._archiver.list_archives()

    def get_archive_stats(self) -> dict:
        return self._archiver.get_archive_stats()

    def get_files_read(self) -> list[str]:
        """Extract file references from opencode output."""
        import re

        if not self._last_result or not self._last_result.output:
            return []

        output = self._last_result.output
        files: set[str] = set()

        file_patterns = [
            re.compile(
                r"(?:^|\s)([a-zA-Z_][\w./\\-]*\.(?:py|js|ts|jsx|tsx|json|yaml|yml|toml|md|txt|rst|cfg|ini|sh|bat|ps1|html|css|xml))(?:\s|$)",
                re.MULTILINE,
            ),
            re.compile(r"(?:file|path):\s*([a-zA-Z_][\w./\\-]+)", re.IGNORECASE),
            re.compile(
                r"(?:reading|creating|writing|modifying|editing|updating|opening)\s+(?:file\s+)?([a-zA-Z_][\w./\\-]+)",
                re.IGNORECASE,
            ),
            re.compile(
                r"```[\w]*\s*\n\s*(?:#\s*)?([a-zA-Z_][\w./\\-]+\.(?:py|js|ts|json|yaml|yml|toml|md|txt))",
                re.MULTILINE,
            ),
        ]

        for pattern in file_patterns:
            for match in pattern.finditer(output):
                file_path = match.group(1)
                if file_path and len(file_path) > 2:
                    files.add(file_path)

        return sorted(files)
