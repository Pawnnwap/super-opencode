"""supervisor/core/loop.py — main orchestration loop.

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
from collections.abc import Generator

from supervisor.analyzers.codebase_analyzer import snapshot_codebase
from supervisor.analyzers.opencode_step_detector import StepProgress
from supervisor.core.llm_supervisor import LLMSupervisor, StepContext
from supervisor.core.loop_base import BaseLoop, Event, LoopState, _ev
from supervisor.protocols.protocol import load_protocol
from supervisor.utils.config import SupervisorConfig
from supervisor.utils.gitignore_utils import update_gitignore_files
from supervisor.workspace.workspace_archiver import WorkspaceArchiver

logger = logging.getLogger(__name__)


class SupervisorLoop(BaseLoop):
    def __init__(self, config: SupervisorConfig):
        super().__init__(config)
        _setup_logging(config.log_level)

        # Update .gitignore files before any other operations
        modified_gitignores = update_gitignore_files(config.workspace)
        if modified_gitignores:
            logger.info(
                f"Modified {len(modified_gitignores)} .gitignore file(s): {[str(p) for p in modified_gitignores]}",
            )

        self.protocol = load_protocol(config.protocol_path)
        self._cached_snapshot = snapshot_codebase(config.workspace)
        self.supervisor = LLMSupervisor(
            self.protocol,
            config.workspace,
            config.supervisor_model,
            extra_system=self._codebase_preamble(),
            read_external_feedback=config.read_external_feedback,
            max_tokens=config.max_tokens,
            max_protected_files_for_suggestions=config.max_protected_files_for_suggestions,
            truncation_enabled=config.truncation_enabled,
            max_history_turns=config.max_history_turns,
            compact_intermediate_steps=config.compact_intermediate_steps,
        )
        self._init_components(agent="build")
        self.archiver = WorkspaceArchiver(config.workspace)
        self._step_detector_initialized = False
        self._plan_context: str = (
            ""  # populated by _run_plan_mode, carried into _init_prompt
        )
        self._last_plan: str = ""
        self._last_supervisor_feedback: str = ""

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
            yield _ev(
                "info",
                f"Archived {len(archive_result.archived_files)} files to {archive_result.archive_path}",
            )
        else:
            yield _ev("warn", f"Archive warning: {archive_result.message}")

        if self.config.plan_mode_rounds > 0:
            yield from self._run_plan_mode()
            if self._state != LoopState.RUNNING:
                return

        yield from self._apply_protection()

        try:
            yield _ev("info", "Running initial prompt with opencode…")

            init_prompt = self._init_prompt()
            yield _ev("opencode_prompt", init_prompt)  # ← full prompt visible
            self.runner.reset_step_detector()
            self.runner.start(init_prompt)
            self._last_step_time = time.time()
            output, timed_out = self.runner.read_output()

            yield from self._run_loop(output, timed_out)
        finally:
            yield from self._remove_protection()
            self.runner.stop()

        if self._state == LoopState.ENDED_SUCCESS:
            yield _ev("success", "All targets met — run finished successfully.")
        else:
            yield _ev(
                "error", "Run ended with failures. See failure_report.md in workspace.",
            )

    def _run_plan_mode(self) -> Generator[Event, None, None]:
        """Run the plan phase for the configured number of rounds.

        A dedicated ``OpencodeRunner`` with ``agent="plan"`` handles all plan
        invocations so opencode stays in read-only mode.  After all rounds the
        final supervisor feedback is stored in ``self._plan_context`` and
        prepended to the build-mode initial prompt by ``_init_prompt()``, giving
        opencode full context of the agreed plan when it starts writing code.

        All supervisor↔opencode exchanges are emitted as ``log-plan_phase``
        events so they appear as a distinct section in the UI event stream.
        """
        from supervisor.runners.opencode_runner import OpencodeRunner

        total = self.config.plan_mode_rounds
        yield _ev(
            "info",
            f"[plan mode] Starting plan phase ({total} round{'s' if total != 1 else ''})…",
        )

        # Dedicated runner locked to the plan agent — the main self.runner
        # stays untouched and will be used for build mode.
        plan_runner = OpencodeRunner(
            self.config.workspace,
            self.config.opencode_model,
            self.config.opencode_executable,
            self.config.timeout,
            agent="plan",
        )

        protocol_text = self.config.protocol_path.read_text(encoding="utf-8")
        ws = self.config.workspace.resolve()
        protected_files_desc = self.guard.get_all_protected_files_description()
        plan_prompt = (
            "@explore You are in PLAN MODE. Do NOT create, modify, or delete any files yet.\n\n"
            "Read the protocol below carefully and produce a detailed implementation plan:\n"
            "  1. Break the work into concrete, ordered steps.\n"
            "  2. Identify dependencies between steps.\n"
            "  3. Flag any ambiguities or risks in the requirements.\n"
            "  4. Do NOT write or edit any source files during this phase.\n\n"
            f"PROTOCOL:\n{protocol_text}\n\n"
            f"Your project root (cwd) is: {ws}\n"
            f"{protected_files_desc}\n"
            "Output your plan now."
        )

        last_feedback: str = ""
        last_plan_output: str = ""

        for round_num in range(1, total + 1):
            yield _ev(
                "log-plan_phase",
                f"[plan mode] Round {round_num}/{total} — sending prompt to opencode…",
            )

            # Round 1: full plan prompt.  Subsequent rounds: supervisor feedback only.
            if last_feedback:
                prompt = (
                    f"[plan mode — round {round_num}/{total}]\n\n"
                    "Supervisor feedback on your previous plan:\n"
                    f"{last_feedback}\n\n"
                    "Revise your plan accordingly. Remember: do NOT modify any files."
                )
            else:
                prompt = plan_prompt

            yield _ev("opencode_prompt", prompt)
            plan_runner.reset_step_detector()
            if round_num > 1:
                plan_runner.enable_continuation(True)
            plan_runner.start(prompt)
            output, timed_out = plan_runner.read_output()

            if timed_out or not output.strip():
                yield _ev(
                    "warn",
                    f"[plan mode] Round {round_num}/{total} produced no output — skipping.",
                )
                continue

            last_plan_output = output

            yield _ev("opencode_output", output)
            yield _ev(
                "log-plan_phase",
                f"[plan mode] Round {round_num}/{total} — supervisor evaluating plan…",
            )

            progress = plan_runner.get_step_progress()
            step_context = StepContext(
                current_step=progress.current_step,
                total_steps_estimate=progress.total_steps_estimate,
                phase="plan",
                completed_phases=list(progress.completed_phases),
            )
            verdict = self.supervisor.judge_plan(
                opencode_output=output,
                plan_round=round_num,
                total_plan_rounds=total,
                step_context=step_context,
            )

            yield _ev("supervisor_response", verdict.raw)
            yield _ev(
                "log-plan_phase",
                f"[plan mode] Round {round_num}/{total} complete — "
                f"supervisor feedback ({len(verdict.feedback)} chars) recorded.",
            )
            yield from self._emit_token_warnings()

            # Early termination: if the supervisor says all targets are met,
            # exit plan mode immediately regardless of remaining rounds.
            if verdict.all_targets_met:
                yield _ev(
                    "info",
                    f"[plan mode] Supervisor signaled all targets met after round {round_num}/{total} — ending plan phase early.",
                )
                last_feedback = verdict.feedback
                break

            last_feedback = verdict.feedback

        plan_runner.stop()

        # Persist the final plan + supervisor feedback so _init_prompt() can
        # inject it into the first build-mode prompt.  This is the only mechanism
        # that carries plan context across the subprocess boundary.
        if last_feedback:
            self._plan_context = (
                "## Agreed plan from plan phase\n\n"
                f"{last_feedback}\n\n"
                "Implement the above plan now. You may create and modify files freely."
            )
            self._last_plan = last_plan_output
            self._last_supervisor_feedback = last_feedback

        yield _ev(
            "info",
            f"[plan mode] Plan phase complete after {total} round{'s' if total != 1 else ''}. "
            "Transitioning to build mode…",
        )

    def get_step_progress(self) -> StepProgress:
        return self.runner.get_step_progress()

    def get_step_summary(self) -> dict:
        return self.runner.get_step_summary()

    def _on_successful_output(self, output: str) -> Generator[Event, None, None]:
        yield from self._check_and_update_snapshot()
        yield from []

    def _get_verdict(self, output: str, progress) -> SupervisorVerdict:
        step_context = self._get_step_context(progress)
        return self.supervisor.judge_with_step_context(output, step_context)

    def _post_judge_feedback(
        self, safe_msg: str, output: str,
    ) -> Generator[Event, None, str]:
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
        return safe_msg

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
                f"Run terminated after {self._failures} failures.\n\n{report}",
            )
            self._state = LoopState.ENDED_FAILURE
            return

        yield _ev(
            "info",
            f"Retrying with restart prompt… (attempt {self._failures}/{self.config.max_retries})",
        )
        self.runner.start(self._restart_prompt())

    # Override run_streaming to update state when stopping the runner
    def run_streaming(self) -> Generator[Event, None, None]:
        try:
            yield from super().run_streaming()
        except KeyboardInterrupt:
            self.runner.stop()
            self._state = LoopState.ENDED_FAILURE
            yield _ev("warn", "Interrupted by user.")
        except Exception:
            import traceback

            self.runner.stop()
            self._state = LoopState.ENDED_FAILURE
            yield _ev("error", f"Unhandled exception:\n{traceback.format_exc()}")

    def _init_prompt(self) -> str:
        from supervisor.prompts import (HASHLINE_SYSTEM_INSTRUCTIONS,
                                        INIT_PROMPT_TEMPLATE)

        text = self.config.protocol_path.read_text(encoding="utf-8")
        ws = self.config.workspace.resolve()
        protected_files_desc = self.guard.get_all_protected_files_description()
        plan_section = f"{self._plan_context}\n\n" if self._plan_context else ""
        plan_output_section = ""
        if self._last_plan and self._plan_context:
            plan_output_section = (
                "## Last Plan Output from Plan Mode\n\n"
                f"{self._last_plan}\n\n"
                "## Last Supervisor Feedback on Plan\n\n"
                f"{self._last_supervisor_feedback}\n\n"
            )
        plan_section = self._strip_done_phrases(plan_section)
        plan_output_section = self._strip_done_phrases(plan_output_section)
        return INIT_PROMPT_TEMPLATE.format(
            hashline_instructions=HASHLINE_SYSTEM_INSTRUCTIONS,
            protocol_text=text,
            plan_section=plan_section,
            plan_output_section=plan_output_section,
            workspace=ws,
            protected_files_desc=protected_files_desc,
        )

    def _restart_prompt(self) -> str:
        summary, text = self._get_restart_context()
        return (
            "Resuming previous session. Context was cleared.\n\n"
            f"PROTOCOL:\n{text}\n\n"
            f"LAST SUMMARY:\n{summary}\n\n"
            f"Working directory: {self.config.workspace.resolve()}\n"
            "Continue from where the summary left off."
        )


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
