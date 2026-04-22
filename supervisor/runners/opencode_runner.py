"""supervisor/opencode_runner.py

Drives opencode via its non-interactive CLI:

    opencode run "<prompt>" [--model <model>]

- stdin=DEVNULL guarantees no TTY, no interactive prompts
- All permissions auto-approved in `run` mode
"""

from __future__ import annotations

import logging
import subprocess
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
from supervisor.runners.opencode_support.command_builder import (
    build_cmd as _build_cmd_impl,
    fresh_session_prompt as _fresh_session_prompt_impl,
    validate_message as _validate_message_impl,
)
from supervisor.runners.opencode_support.inspection import extract_file_refs
from supervisor.runners.opencode_support.locator import find_opencode as _find_opencode
from supervisor.runners.opencode_support.process import run_prompt as _run_prompt_impl
from supervisor.runners.opencode_support.result import RunResult
from supervisor.runners.opencode_support.session import (
    SESSION_CAPTURE_LOCK as _SESSION_CAPTURE_LOCK,
    capture_new_session_id as _capture_new_session_id_impl,
    list_all_session_ids as _list_all_session_ids_impl,
)
from supervisor.utils.text_utils import coerce_str, strip_thinking_blocks
from supervisor.workspace.cleanup_candidates import (
    build_cleanup_inquiry,
    identify_cleanup_candidates as discover_cleanup_candidates,
)
from supervisor.workspace.workspace_archiver import ArchiveResult, WorkspaceArchiver

logger = logging.getLogger(__name__)


def find_opencode(explicit: str = "") -> str:
    return _find_opencode(explicit)


def _validate_message(message: str, context: str = "message") -> str | None:
    return _validate_message_impl(message, context)


class OpencodeRunner(BaseRunner):
    """One send()/start() call = one `opencode run "<prompt>"` subprocess."""

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
        logger.debug(
            "__init__ raw inputs - opencode_model=%r (type=%s) opencode_executable=%r (type=%s) "
            "agent=%r (type=%s) opencode_model_backup=%r (type=%s) timeout=%r (type=%s)",
            opencode_model,
            type(opencode_model).__name__,
            opencode_executable,
            type(opencode_executable).__name__,
            agent,
            type(agent).__name__,
            opencode_model_backup,
            type(opencode_model_backup).__name__,
            timeout,
            type(timeout).__name__,
        )

        self.workspace = workspace
        self.opencode_model = coerce_str(opencode_model, "opencode_model") or None
        self.opencode_model_backup = (
            coerce_str(opencode_model_backup, "opencode_model_backup") or None
        )
        self.opencode_executable = coerce_str(
            opencode_executable,
            "opencode_executable",
        )
        self.agent = coerce_str(agent, "agent")
        if not isinstance(timeout, int):
            logger.warning(
                "Type coercion: 'timeout' received %r (type=%s) - casting to int.",
                timeout,
                type(timeout).__name__,
            )
        self.timeout = int(timeout)

        logger.info(
            "__init__ coerced values - opencode_model=%r opencode_model_backup=%r "
            "agent=%r timeout=%d workspace=%s",
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
        self._session_active = False
        self._use_continue = False
        self._session_id: str | None = None

        self._step_detector = step_detector or OpencodeStepDetector(
            step_callback=on_step,
            transition_callback=on_transition,
            progress_callback=on_progress,
        )

    @classmethod
    def from_config(cls, config, agent: str = "") -> OpencodeRunner:
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

    @property
    def estimated_context_tokens(self) -> int:
        return self._chars_exchanged // 4

    def get_step_progress(self) -> StepProgress:
        return self._step_detector.progress

    def reset_step_detector(self) -> None:
        self._step_detector.reset()

    def start(self, initial_prompt: str) -> Generator[dict]:
        validated = _validate_message(initial_prompt, "initial_prompt (start)")
        if validated is None:
            logger.warning(
                "Empty initial prompt in start(). Using fallback to continue session.",
            )
            validated = "Continue based on the current context and proceed with the task."

        self._alive = True
        if self._session_active:
            yield from self._run_prompt(validated)
            return

        logger.info("New session detected. Sending brevity command...")
        yield {"level": "info", "msg": "New session detected. Sending brevity command..."}
        with _SESSION_CAPTURE_LOCK:
            before = self._list_all_session_ids()
            yield from self._run_prompt(BREVITY_COMMAND)
            self._session_id = self._capture_new_session_id(before)

        if self._session_id:
            logger.info("Session ID captured: %s", self._session_id)
            yield {"level": "info", "msg": f"Session ID captured: {self._session_id}"}
            self._session_active = True
            self.enable_continuation(True)
            yield from self._run_prompt(validated)
            return

        logger.warning(
            "Could not isolate a newly-created session after BREVITY_COMMAND. "
            "Running initial prompt in a brand-new session instead of risking old --continue attachment.",
        )
        yield {
            "level": "warn",
            "msg": "Session ID capture failed; forcing a fresh prompt session.",
        }
        self._session_id = None
        self.enable_continuation(False)
        yield from self._run_prompt(self._fresh_session_prompt(validated))
        self._session_active = True
        self.enable_continuation(True)

    def send(self, message: str) -> Generator[dict]:
        validated = _validate_message(message, "message (send)")
        resolved_message = (
            validated
            if validated is not None
            else "Continue based on the current context and proceed."
        )
        max_retries = 5
        base_wait = 30

        for attempt in range(max_retries + 1):
            if self._alive:
                if self._session_active:
                    self.enable_continuation(True)
                logger.info("send() - message length=%d chars", len(resolved_message))
                yield from self._run_prompt(resolved_message)
                return

            if attempt == max_retries:
                state_info = (
                    f"alive={self._alive}, session_active={self._session_active}"
                )
                raise RuntimeError(
                    "OpencodeRunner has been stopped. "
                    f"(Operation: send, State: {state_info}, Retries: {attempt}/{max_retries})",
                )

            wait_time = base_wait * (2 ** attempt)
            logger.warning(
                "OpencodeRunner is stopped. Retrying send operation (attempt %d/%d) in %ds...",
                attempt + 1,
                max_retries,
                wait_time,
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

    def _prepare_workspace(self) -> None:
        """Ensure workspace exists and contains opencode project marker."""
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
        yield from _run_prompt_impl(self, prompt, find_opencode_fn=find_opencode)

    def enable_continuation(self, enabled: bool = True) -> None:
        self._use_continue = enabled

    def is_continuation_enabled(self) -> bool:
        return self._use_continue

    def mark_session_active(self) -> None:
        self._session_active = True

    def reset_session(self) -> None:
        self._session_active = False
        self._use_continue = False
        self._session_id = None

    def _fresh_session_prompt(self, prompt: str) -> str:
        return _fresh_session_prompt_impl(prompt)

    def _list_all_session_ids(self) -> set[str]:
        return _list_all_session_ids_impl(
            workspace=self.workspace,
            opencode_executable=self.opencode_executable,
            find_opencode_fn=find_opencode,
        )

    def _capture_new_session_id(
        self,
        before: set[str],
        attempts: int = 4,
        delay_seconds: float = 0.25,
    ) -> str | None:
        return _capture_new_session_id_impl(
            before,
            workspace=self.workspace,
            opencode_executable=self.opencode_executable,
            find_opencode_fn=find_opencode,
            attempts=attempts,
            delay_seconds=delay_seconds,
        )

    def reset_context_counter(self) -> None:
        self._chars_exchanged = 0

    def _build_cmd(
        self,
        exe: str,
        prompt: str,
        model: str | None = None,
        use_shell: bool = False,
    ) -> list[str]:
        return _build_cmd_impl(
            exe=exe,
            prompt=prompt,
            agent=self.agent,
            opencode_model=self.opencode_model,
            use_continue=self._use_continue,
            session_id=self._session_id,
            model=model,
            use_shell=use_shell,
        )

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
        inquiry = build_cleanup_inquiry(self.workspace, candidates)
        if not inquiry:
            return
        logger.info(
            "Sending cleanup inquiry to opencode for %d candidates",
            len(candidates),
        )
        for _ in self.send(inquiry):
            pass

    def identify_cleanup_candidates(self) -> list[str]:
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
        if not self._last_result or not self._last_result.output:
            return []
        return extract_file_refs(self._last_result.output)


__all__ = ["OpencodeRunner", "RunResult", "find_opencode"]
