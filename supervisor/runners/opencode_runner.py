"""supervisor/opencode_runner.py

Drives opencode via its non-interactive CLI:

    opencode run "<prompt>" [--model <model>]

- stdin=DEVNULL  → guarantees no TTY, no interactive prompts
- All permissions auto-approved in `run` mode

IMPORTANT: All prompts passed to opencode must be wrapped in double quotes.
This is enforced by the `quote_prompt` utility function to ensure proper
shell command execution across all platforms (Windows, Linux, macOS).
Internal double quotes in prompts are escaped by doubling them.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path

from supervisor.analyzers.opencode_step_detector import (
    OpencodeStepDetector,
    PhaseTransition,
    Step,
    StepProgress,
)
from supervisor.prompts.commands import BREVITY_COMMAND
from supervisor.runners.base_runner import BaseRunner
from supervisor.runners.opencode_locator import find_opencode as _find_opencode
from supervisor.utils.text_utils import coerce_str, quote_prompt, strip_thinking_blocks
from supervisor.workspace.cleanup_candidates import (
    build_cleanup_inquiry,
    identify_cleanup_candidates as discover_cleanup_candidates,
)
from supervisor.workspace.workspace_archiver import ArchiveResult, WorkspaceArchiver

logger = logging.getLogger(__name__)

_DOT_MODEL_FILE = Path(__file__).parent.parent / ".opencode_model"

# Process-wide lock serializing the session-creation critical section.
# Opencode's `session list` is not strictly workspace-scoped — concurrent
# start() calls can see each other's just-created sessions and misattribute
# them, causing subsequent `--session <id>` sends to route messages into the
# wrong task's session. Holding this lock across the "snapshot → run BREVITY
# → snapshot → diff" window guarantees the diff contains exactly our session.
_SESSION_CAPTURE_LOCK = threading.Lock()


def find_opencode(explicit: str = "") -> str:
    return _find_opencode(explicit)


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


def _validate_message(message: str, context: str = "message") -> str | None:
    """Validate message is non-empty after coercion. Returns None for empty/whitespace instead of raising."""
    message = coerce_str(message, context)
    if not message:
        logger.warning("Empty message provided to opencode (%s). Returning None to trigger graceful handling.", context)
        return None
    return message


class OpencodeRunner(BaseRunner):
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
        super().__init__(workspace)
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
        raw_model = coerce_str(opencode_model, "opencode_model")
        self.opencode_model: str | None = raw_model or None

        raw_backup = coerce_str(opencode_model_backup, "opencode_model_backup")
        self.opencode_model_backup: str | None = raw_backup or None

        self.opencode_executable = coerce_str(opencode_executable, "opencode_executable")
        self.agent = coerce_str(agent, "agent")

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
        self._process: subprocess.Popen | None = None
        self._archiver = WorkspaceArchiver(workspace)
        self._session_active: bool = False
        self._use_continue: bool = False
        self._session_id: str | None = None

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

    def start(self, initial_prompt: str) -> Generator[dict]:
        validated = _validate_message(initial_prompt, "initial_prompt (start)")
        if validated is None:
            logger.warning("Empty initial prompt in start(). Using fallback to continue session.")
            validated = "Continue based on the current context and proceed with the task."
        self._alive = True
        if not self._session_active:
            logger.info("New session detected. Sending brevity command...")
            yield {"level": "info", "msg": "New session detected. Sending brevity command..."}
            with _SESSION_CAPTURE_LOCK:
                before = self._list_all_session_ids()
                yield from self._run_prompt(BREVITY_COMMAND)
                self._session_id = self._capture_new_session_id(before)
            if self._session_id:
                logger.info("Session ID captured: %s", self._session_id)
                yield {"level": "info", "msg": f"Session ID captured: {self._session_id}"}
            else:
                logger.warning(
                    "Could not isolate a newly-created session; falling back to --continue.",
                )
                yield {
                    "level": "warn",
                    "msg": "Session ID capture failed; using --continue fallback.",
                }
            self._session_active = True
            self.enable_continuation(True)
        yield from self._run_prompt(validated)

    def send(self, message: str) -> Generator[dict]:
        validated = _validate_message(message, "message (send)")
        resolved_message = validated if validated is not None else "Continue based on the current context and proceed."
        max_retries = 5
        base_wait = 30

        for attempt in range(max_retries + 1):
            if self._alive:
                if self._session_active:
                    self.enable_continuation(True)

                logger.info("send() — message length=%d chars", len(resolved_message))
                yield from self._run_prompt(resolved_message)
                return
            if attempt == max_retries:
                state_info = f"alive={self._alive}, session_active={self._session_active}"
                error_msg = (
                    f"OpencodeRunner has been stopped. (Operation: send, State: {state_info}, "
                    f"Retries: {attempt}/{max_retries})"
                )
                raise RuntimeError(error_msg)

            wait_time = base_wait * (2 ** attempt)
            logger.warning(
                "OpencodeRunner is stopped. Retrying send operation (attempt %d/%d) in %ds...",
                attempt + 1, max_retries, wait_time,
            )
            time.sleep(wait_time)

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
        # Intentionally do NOT sweep all opencode processes by name here:
        # in multi-task mode, sibling tasks are running their own opencode
        # subprocesses and a broad kill would cancel them too.

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

    def _run_prompt(self, prompt: str) -> Generator[dict]:
        # Defensive coercion — should already be clean but belt-and-suspenders
        prompt = coerce_str(prompt, "prompt (_run_prompt)")

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

            use_shell = sys.platform == "win32" and exe.lower().endswith(
                (".cmd", ".bat", ".ps1"),
            )

            cmd = self._build_cmd(exe, prompt, model=model_for_cmd, use_shell=use_shell)
            msg = f"Running opencode command: {' '.join(cmd)}"
            logger.info(msg)
            yield {"level": "info", "msg": msg}

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
        self._session_id = None

    def _list_all_session_ids(self) -> set[str]:
        """Enumerate every session ID opencode currently reports.

        Returns an empty set on failure. Used as a snapshot to diff before and
        after BREVITY_COMMAND so we can isolate *our* newly-created session and
        avoid picking up another concurrent task's session ID.
        """
        try:
            exe = find_opencode(self.opencode_executable)
            use_shell = sys.platform == "win32" and exe.lower().endswith(
                (".cmd", ".bat", ".ps1"),
            )
            result = subprocess.run(
                [exe, "session", "list"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self.workspace),
                timeout=10,
                shell=use_shell,
            )
            ids: set[str] = set()
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("ses_"):
                    ids.add(stripped.split()[0])
            return ids
        except Exception as exc:
            logger.warning("Failed to list sessions: %s", exc)
            return set()

    def _capture_new_session_id(self, before: set[str]) -> str | None:
        """Return the single session ID that appeared since the `before` snapshot.

        When exactly one new ID is present, that is unambiguously the session
        this runner just created. When zero or more-than-one new IDs are
        present we refuse to guess and return None; callers then fall back to
        the generic `--continue` flag. The surrounding module-level lock
        normally guarantees exactly one new ID, but we stay defensive in case
        something external (another user, a daemon) creates sessions too.
        """
        after = self._list_all_session_ids()
        new_ids = after - before
        if len(new_ids) == 1:
            session_id = next(iter(new_ids))
            logger.info("Captured session ID: %s", session_id)
            return session_id
        if len(new_ids) > 1:
            logger.warning(
                "Ambiguous session capture: %d new sessions appeared (%s); "
                "refusing to pick one.",
                len(new_ids),
                sorted(new_ids),
            )
        else:
            logger.warning("No new session appeared after BREVITY_COMMAND.")
        return None

    def reset_context_counter(self) -> None:
        self._chars_exchanged = 0

    def _build_cmd(
        self,
        exe: str,
        prompt: str,
        model: str | None = None,
        use_shell: bool = False,
    ) -> list[str]:
        """Build the opencode CLI command list.

        When ``use_shell`` is True (Windows ``.cmd``/``.bat``/``.ps1``), the
        prompt is wrapped with ``quote_prompt`` so cmd.exe receives it as a
        single argument. On POSIX (or whenever the subprocess is launched
        without a shell), the prompt is passed verbatim — argv entries must
        not carry literal surrounding quotes.
        """
        exe = coerce_str(exe, "exe (_build_cmd)")
        prompt = coerce_str(prompt, "prompt (_build_cmd)")
        agent = coerce_str(self.agent, "self.agent (_build_cmd)")

        # Resolve model with explicit coercion at every step
        raw_model_arg = coerce_str(model, "model arg (_build_cmd)")
        raw_self_model = coerce_str(self.opencode_model, "self.opencode_model (_build_cmd)")
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
            if self._session_id:
                cmd += ["--session", self._session_id]
            else:
                cmd.append("--continue")

        if resolved_model:
            cmd += ["--model", resolved_model]

        # End-of-flags separator: everything after `--` is treated as the
        # positional message, even if it starts with `-` / `--`. Without this,
        # prompts beginning with "--" (or even containing a token like "--help"
        # as the first word) get misparsed as unknown flags, and opencode falls
        # back to printing the `run` subcommand help to stderr instead of
        # executing the prompt.
        cmd.append("--")
        cmd.append(quote_prompt(prompt) if use_shell else prompt)

        return cmd

    def process_step_detection(self, output: str) -> Generator[dict]:
        yield from self._step_detector.process_output(output)

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
        inquiry = build_cleanup_inquiry(self.workspace, candidates)
        if not inquiry:
            return

        logger.info(
            "Sending cleanup inquiry to opencode for %d candidates", len(candidates),
        )
        for _ in self.send(inquiry):
            pass

    def identify_cleanup_candidates(self) -> list[str]:
        """Identify files that might be outdated or unused."""
        candidates = discover_cleanup_candidates(self.workspace)
        if candidates:
            self.send_cleanup_inquiry(candidates)
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
