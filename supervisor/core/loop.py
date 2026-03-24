"""
supervisor/loop.py — main orchestration loop.

With the CLI-based runner, each turn is:
  1. opencode -p "<prompt>" runs and exits  (done inside runner.start / runner.send)
  2. runner.read_output() returns the captured text immediately
  3. supervisor judges, produces feedback
  4. feedback becomes the next prompt → goto 1

No idle detection, no drain threads, no pipe hacks.
"""

from __future__ import annotations

import logging
import sys
from enum import Enum, auto
from typing import Generator

from supervisor.utils.config import SupervisorConfig
from supervisor.monitoring.context_monitor import ContextMonitor
from supervisor.core.llm_supervisor import LLMSupervisor, StepContext
from supervisor.core.loop_base import BaseLoop, Event, _ev
from supervisor.runners.opencode_runner import OpencodeRunner
from supervisor.analyzers.opencode_step_detector import OpencodeStepDetector, Step, StepProgress
from supervisor.protocols.protocol import load_protocol
from supervisor.workspace.workspace_guard import WorkspaceGuard
from supervisor.workspace.workspace_archiver import WorkspaceArchiver

logger = logging.getLogger(__name__)

class LoopState(Enum):
    RUNNING = auto()
    ENDED_SUCCESS = auto()
    ENDED_FAILURE = auto()


class SupervisorLoop(BaseLoop):
    def __init__(self, config: SupervisorConfig):
        super().__init__()
        self.config = config
        _setup_logging(config.log_level)

        self.protocol = load_protocol(config.protocol_path)
        self.supervisor = LLMSupervisor(
            self.protocol, config.workspace, config.supervisor_model,
            read_external_feedback=config.read_external_feedback,
            max_tokens=config.max_tokens,
            max_protected_files_for_suggestions=config.max_protected_files_for_suggestions,
            truncation_enabled=config.truncation_enabled,
            max_history_turns=config.max_history_turns,
            compact_intermediate_steps=config.compact_intermediate_steps,
        )
        self.runner = OpencodeRunner(
            config.workspace,
            config.opencode_model,
            config.opencode_executable,
            config.timeout,
        )
        self.ctx_monitor = ContextMonitor(config.context_threshold, config.max_tokens, config.truncation_enabled)
        self.guard = WorkspaceGuard(config.workspace, config.protected_files)
        self.archiver = WorkspaceArchiver(config.workspace)
        self._step_detector = OpencodeStepDetector()
        self._step_detector_initialized = False

        self._failures = 0
        self._state = LoopState.RUNNING
        self._last_step_time: float = 0.0
        self._active_progress_steps: int = 0
        self._timeout_extension_count: int = 0
        self._max_timeout_extensions: int = 3

    # ------------------------------------------------------------------ #

    def run(self) -> int:
        for ev in self.run_streaming():
            lvl = ev["level"]
            msg = ev["msg"]
            (
                logger.error
                if lvl == "error"
                else logger.warning
                if lvl == "warn"
                else logger.info
            )(msg)
        return 0 if self._state == LoopState.ENDED_SUCCESS else 1

    # ------------------------------------------------------------------ #

    def _run(self) -> Generator[Event, None, None]:
        import time

        yield _ev("info", "Archiving previous workspace state...")
        archive_result = self.archiver.archive_before_new_run()
        if archive_result.success:
            yield _ev("info", f"Archived {len(archive_result.archived_files)} files to {archive_result.archive_path}")
        else:
            yield _ev("warn", f"Archive warning: {archive_result.message}")

        yield _ev("info", "Running initial prompt with opencode…")

        init_prompt = self._init_prompt()
        yield _ev("opencode_prompt", init_prompt)  # ← full prompt visible
        self.runner.reset_step_detector()
        self.runner.start(init_prompt)
        self._last_step_time = time.time()
        output, timed_out = self.runner.read_output()

        while self._state == LoopState.RUNNING:
            current_progress = self.runner.get_step_progress()
            
            if timed_out or not output.strip():
                if self._should_extend_timeout(current_progress):
                    yield from self._handle_active_progress_timeout(current_progress)
                    self._timeout_extension_count += 1
                    output, timed_out = self.runner.read_output()
                    continue
                
                diag = self.runner.last_diagnostic()
                yield _ev("warn", f"opencode returned no output. Diagnostic:\n{diag}")
                yield from self._handle_failure(output)
                if self._state != LoopState.RUNNING:
                    break
                output, timed_out = self.runner.read_output()
                continue

            self._failures = 0
            files_read = self.runner.get_files_read()
            self.ctx_monitor.update(self.runner.estimated_context_tokens, files_read=files_read)
            
            # Emit context warning if approaching limit
            if self.ctx_monitor.approaching_limit:
                advice = self.ctx_monitor.get_reduction_advice()
                file_info = f"Files loaded: {', '.join(files_read)}" if files_read else "Files loaded: none"
                yield _ev(
                    "warn",
                    f"⚠️ Context usage high: {advice['current_tokens']}/{advice['max_tokens']} tokens "
                    f"({self.ctx_monitor.fraction*100:.0f}%). {advice['recommendation']}.\n\n{file_info}"
                )

            previous_step = self._active_progress_steps
            yield from self._emit_step_events(output)
            yield _ev("opencode_output", output)  # ← full output visible
            
            current_progress = self.runner.get_step_progress()
            if current_progress.current_step > previous_step:
                self._last_step_time = time.time()
                self._active_progress_steps = current_progress.current_step
                self._timeout_extension_count = 0
                yield from self._emit_heartbeat(current_progress)

            if self.ctx_monitor.should_compact:
                self.supervisor.compact_history()
                yield from self._do_compaction()
                output, timed_out = self.runner.read_output()
                continue

            yield from self._do_judgement(output)
            if self._state != LoopState.RUNNING:
                break

            output, timed_out = self.runner.read_output()

        self.runner.stop()
        if self._state == LoopState.ENDED_SUCCESS:
            yield _ev("success", "All targets met — run finished successfully.")
        else:
            yield _ev(
                "error", "Run ended with failures. See failure_report.md in workspace."
            )

    def get_step_progress(self) -> StepProgress:
        return self.runner.get_step_progress()

    def get_step_summary(self) -> dict:
        return self.runner.get_step_summary()

    def _do_judgement(self, output: str) -> Generator[Event, None, None]:
        yield _ev("info", "Supervisor judging…")
        
        progress = self.runner.get_step_progress()
        step_context = StepContext(
            current_step=progress.current_step,
            total_steps_estimate=progress.total_steps_estimate,
            phase=progress.phase.name.lower(),
            completed_phases=list(progress.completed_phases),
        )
        verdict = self.supervisor.judge_with_step_context(output, step_context)
        yield _ev("supervisor_response", verdict.raw)  # ← full supervisor reply

        # Emit token warnings from supervisor if any
        warnings = self.supervisor.get_token_warnings()
        if warnings:
            files_read = self.runner.get_files_read()
            file_info = f"Files loaded: {', '.join(files_read)}" if files_read else "Files loaded: none"
            for warning in warnings:
                yield _ev("warn", f"⚠️ Token warning: {warning}\n\n{file_info}")
            self.supervisor.clear_token_warnings()

        if verdict.all_targets_met:
            self._state = LoopState.ENDED_SUCCESS
            return

        safe_msg, violations = self.guard.sanitize_message(verdict.feedback)
        if violations:
            yield _ev("warn", f"Blocked out-of-workspace paths: {violations}")

        alignment = self.supervisor.verify_protocol_alignment(output, self.protocol)
        if not alignment.aligned:
            logger.warning(
                "Protocol violations detected: %s",
                [v.description for v in alignment.violations],
            )
            yield _ev(
                "warn",
                f"Protocol alignment issues found: {len(alignment.violations)} violation(s)",
            )
            safe_msg = alignment.reinforcement_message + safe_msg

        yield _ev("opencode_prompt", safe_msg)  # ← full feedback sent to opencode
        self.runner.send(safe_msg)

        suggestions = self.supervisor.generate_suggestions(
            opencode_output=output,
            step_context=step_context,
        )
        if suggestions and "no suggestions" not in suggestions.lower():
            yield _ev("supervisor_suggestions", suggestions)

    def _handle_failure(self, last_output: str) -> Generator[Event, None, None]:
        self._failures += 1
        retries_remaining = max(0, self.config.max_retries - self._failures)
        
        yield _ev(
            "warn",
            f"opencode returned empty/timeout (failure {self._failures}/{self.config.max_retries}, "
            f"{retries_remaining} {'retry' if retries_remaining == 1 else 'retries'} remaining).",
        )
        yield from self._forced_summary(last_output)

        if self._failures >= self.config.max_retries:
            report = self.supervisor.report_final_status(
                reason=f"opencode failed {self._failures} consecutive times",
                opencode_output=last_output,
                workspace=self.config.workspace,
            )
            self._write(report, "failure_report.md")
            yield _ev(
                "error",
                f"All {self.config.max_retries} {'retry' if self.config.max_retries == 1 else 'retries'} exhausted. "
                f"Run terminated after {self._failures} failures.\n\n{report}"
            )
            self._state = LoopState.ENDED_FAILURE
            return

        yield _ev(
            "info",
            f"Retrying with restart prompt… (attempt {self._failures}/{self.config.max_retries})"
        )
        self.runner.start(self._restart_prompt())

    def _forced_summary(self, last_output: str) -> Generator[Event, None, None]:
        yield _ev("info", "Writing summary.md…")
        report = self.supervisor.report_final_status(
            reason="forced summarization",
            opencode_output=last_output,
            workspace=self.config.workspace,
        )
        self._write(report, "summary.md")
        yield _ev("info", "summary.md written.")

    # ------------------------------------------------------------------ #

    def _init_prompt(self) -> str:
        text = self.config.protocol_path.read_text(encoding="utf-8")
        ws = self.config.workspace.resolve()
        protected_files_desc = self.guard.get_all_protected_files_description()
        return (
            f"Here is your protocol:\n\n{text}\n\n"
            f"Your project root (cwd) is: {ws}\n"
            "All files you create or modify MUST be inside this directory.\n"
            "Use relative paths from this directory for all file operations.\n"
            "A .opencode/ folder has been created there to mark this as your project root.\n"
            "IMPORTANT: Never touch .checkpoints/ — that is reserved for the supervisor.\n"
            "The .archive/ directory preserves historical versions — do not modify it.\n"
            f"{protected_files_desc}\n"
            "Begin."
        )

    def _restart_prompt(self) -> str:
        summary_path = self.config.workspace / "summary.md"
        summary = (
            summary_path.read_text(encoding="utf-8")
            if summary_path.exists()
            else "(none)"
        )
        text = self.config.protocol_path.read_text(encoding="utf-8")
        return (
            "Resuming previous session. Context was cleared.\n\n"
            f"PROTOCOL:\n{text}\n\n"
            f"LAST SUMMARY:\n{summary}\n\n"
            f"Working directory: {self.config.workspace.resolve()}\n"
            "Continue from where the summary left off."
        )

    def _write(self, text: str, filename: str) -> None:
        (self.config.workspace / filename).write_text(text, encoding="utf-8")

def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
