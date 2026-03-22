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

from .config import SupervisorConfig
from .context_monitor import ContextMonitor
from .llm_supervisor import LLMSupervisor, StepContext
from .opencode_runner import OpencodeRunner
from .opencode_step_detector import OpencodeStepDetector, Step, StepProgress
from .protocol import load_protocol
from .workspace_guard import WorkspaceGuard
from .workspace_archiver import WorkspaceArchiver

logger = logging.getLogger(__name__)

Event = dict


class LoopState(Enum):
    RUNNING = auto()
    ENDED_SUCCESS = auto()
    ENDED_FAILURE = auto()


class SupervisorLoop:
    def __init__(self, config: SupervisorConfig):
        self.config = config
        _setup_logging(config.log_level)

        self.protocol = load_protocol(config.protocol_path)
        self.supervisor = LLMSupervisor(
            self.protocol, config.workspace, config.supervisor_model
        )
        self.runner = OpencodeRunner(
            config.workspace,
            config.opencode_model,
            config.opencode_executable,
            config.timeout,
        )
        self.ctx_monitor = ContextMonitor(config.context_threshold)
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

    def run_streaming(self) -> Generator[Event, None, None]:
        try:
            yield from self._run()
        except KeyboardInterrupt:
            self.runner.stop()
            yield _ev("warn", "Interrupted by user.")
        except Exception as exc:
            import traceback

            self.runner.stop()
            yield _ev("error", f"Unhandled exception:\n{traceback.format_exc()}")

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
            self.ctx_monitor.update(self.runner.estimated_context_tokens)
            
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

    def _emit_step_events(self, output: str) -> Generator[Event, None, None]:
        for event in self.runner.get_step_events(output):
            lvl = event.get("level", "info")
            if lvl == "step":
                yield _ev(
                    "step",
                    f"{event.get('phase_label', 'Step')} - {event.get('msg', '')[:100]}",
                )
            elif lvl == "phase_transition":
                yield _ev(
                    "phase_transition",
                    f"Phase transition: {event.get('from_phase', '?')} → {event.get('to_phase', '?')}",
                )
            elif lvl == "step_progress":
                progress = self.runner.get_step_progress()
                progress_event = {
                    "current_step": progress.current_step,
                    "total_steps_estimate": progress.total_steps_estimate,
                    "percentage": progress.percentage,
                    "phase": progress.phase.name.lower(),
                    "completed_phases": list(progress.completed_phases),
                }
                yield _ev(
                    "step_progress",
                    f"Step progress: {progress.current_step}/{progress.total_steps_estimate} ({progress.percentage:.0f}%) - {progress.phase.name.lower()}",
                    **progress_event
                )

    def _should_extend_timeout(self, progress) -> bool:
        import time
        if self._timeout_extension_count >= self._max_timeout_extensions:
            return False
        activity_state = self.runner.get_activity_state()
        if activity_state in ("active_progress", "waiting_for_output"):
            time_since_last_step = time.time() - self._last_step_time
            return time_since_last_step < (self.config.timeout * 0.8)
        if progress.current_step > 0 and progress.phase.name != "UNKNOWN":
            time_since_last_step = time.time() - self._last_step_time
            return time_since_last_step < (self.config.timeout * 0.8)
        return False

    def _handle_active_progress_timeout(
        self, progress
    ) -> Generator[Event, None, None]:
        ext_count = self._timeout_extension_count + 1
        activity_state = self.runner.get_activity_state()
        wait_msg = " (may be waiting for output)" if activity_state == "waiting_for_output" else ""
        yield _ev(
            "info",
            f"opencode is actively working (step {progress.current_step}, "
            f"phase: {progress.phase.name.lower()}, state: {activity_state}{wait_msg}). "
            f"Timeout extension {ext_count}/{self._max_timeout_extensions} — continuing...",
        )

    def _emit_heartbeat(self, progress) -> Generator[Event, None, None]:
        heartbeat_data = {
            "current_step": progress.current_step,
            "total_steps_estimate": progress.total_steps_estimate,
            "percentage": progress.percentage,
            "phase": progress.phase.name.lower(),
        }
        yield _ev(
            "heartbeat",
            f"opencode active: step {progress.current_step}/{progress.total_steps_estimate} "
            f"({progress.phase.name.lower()}) — {progress.percentage:.0f}% complete",
            **heartbeat_data
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

    def _do_compaction(self) -> Generator[Event, None, None]:
        yield _ev(
            "warn", f"Context at {self.ctx_monitor.fraction * 100:.0f}% — compacting."
        )
        candidates = self.runner.identify_cleanup_candidates()
        if candidates:
            yield _ev("info", f"Identified {len(candidates)} files for potential cleanup.")
        deletion_permission = self.supervisor.ask_for_deletion_permission(
            candidates, self.config.workspace
        )
        yield _ev("supervisor_response", deletion_permission.raw)
        msg, _ = self.guard.sanitize_message(deletion_permission.feedback)
        yield _ev("opencode_prompt", msg)
        self.runner.send(msg)
        self.ctx_monitor.reset()
        yield _ev("info", "Compaction prompt with deletion permissions sent.")

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
            f"{protected_files_desc}"
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


def _ev(level: str, msg: str, **kwargs) -> Event:
    event = {"level": level, "msg": msg}
    event.update(kwargs)
    return event


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
