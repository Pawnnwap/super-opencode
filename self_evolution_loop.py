"""
supervisor/self_evolution_loop.py

Self-evolution loop using the CLI-based opencode runner.
Each turn: opencode -p "<prompt>" runs, exits, output is captured.
Tests run after each turn; regressions trigger rollback.
"""

from __future__ import annotations

import logging
import time
from enum import Enum, auto
from typing import Generator

from .codebase_analyzer import snapshot_codebase, CodebaseSnapshot
from .checkpoint import CheckpointManager, Checkpoint
from .config import SupervisorConfig
from .context_monitor import ContextMonitor
from .llm_supervisor import LLMSupervisor, StepContext
from .opencode_runner import OpencodeRunner
from .opencode_step_detector import OpencodeStepDetector
from .protocol import load_protocol
from .test_runner import RunTestResult, OcTestRunner
from .workspace_guard import WorkspaceGuard

logger = logging.getLogger(__name__)

Event = dict


class EvoState(Enum):
    RUNNING = auto()
    ENDED_SUCCESS = auto()
    ENDED_FAILURE = auto()


class SelfEvolutionLoop:
    def __init__(self, config: SupervisorConfig):
        self.config = config
        self.protocol = load_protocol(config.protocol_path)
        self.supervisor = LLMSupervisor(
            self.protocol,
            config.workspace,
            config.supervisor_model,
            extra_system=self._codebase_preamble(),
        )
        self.runner = OpencodeRunner(
            config.workspace,
            config.opencode_model,
            config.opencode_executable,
            config.timeout,
        )
        self.ctx_monitor = ContextMonitor(config.context_threshold)
        self.guard = WorkspaceGuard(config.workspace)
        self.checkpoints = CheckpointManager(config.workspace)
        self.test_runner = OcTestRunner(config.workspace)
        self._step_detector = OpencodeStepDetector()

        self._failures = 0
        self._state = EvoState.RUNNING
        self._baseline: RunTestResult | None = None
        self._last_result: RunTestResult | None = None
        self._best_cp: Checkpoint | None = None
        self._iteration = 0
        self._pre_snapshot: CodebaseSnapshot | None = None
        self._step_history: list[dict] = []
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

    # ------------------------------------------------------------------ #

    def _run(self) -> Generator[Event, None, None]:
        yield _ev("info", "📸  Snapshotting codebase…")
        self._pre_snapshot = snapshot_codebase(self.config.workspace)

        yield _ev("info", "🧪  Running test baseline…")
        self._baseline = self.test_runner.run()
        yield _ev("info", f"Baseline: {self._baseline.summary()}")
        if not self._baseline.ok:
            yield _ev(
                "warn",
                f"Baseline has failures — evolution will try to fix them.\n{self._baseline.output[:600]}",
            )

        yield _ev("info", "💾  Saving pre-evolution checkpoint…")
        pre_cp = self.checkpoints.save("pre-evolution baseline")
        self._best_cp = pre_cp
        yield _ev("info", f"Checkpoint saved: {pre_cp}")

        yield _ev("info", "🚀  Starting opencode for self-evolution…")
        init_prompt = self._init_prompt()
        yield _ev("opencode_prompt", init_prompt)
        self.runner.start(init_prompt)
        self._last_step_time = time.time()
        output, timed_out = self.runner.read_output()

        while self._state == EvoState.RUNNING:
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
                if self._state != EvoState.RUNNING:
                    break
                output, timed_out = self.runner.read_output()
                continue

            self._failures = 0
            self._iteration += 1
            self.ctx_monitor.update(self.runner.estimated_context_tokens)
            yield _ev(
                "info",
                f"[iter {self._iteration}] opencode output ({len(output)} chars)",
            )

            previous_step = self._active_progress_steps
            yield from self._emit_step_events(output)
            yield _ev("opencode_output", output)
            
            current_progress = self.runner.get_step_progress()
            if current_progress.current_step > previous_step:
                self._last_step_time = time.time()
                self._active_progress_steps = current_progress.current_step
                self._timeout_extension_count = 0

            if self.ctx_monitor.should_compact:
                yield from self._do_compaction()
                output, timed_out = self.runner.read_output()
                continue

            yield from self._do_judgement(output)
            if self._state != EvoState.RUNNING:
                break
            output, timed_out = self.runner.read_output()

        self.runner.stop()
        yield from self._evolution_summary()

    def _emit_step_events(self, output: str) -> Generator[Event, None, None]:
        for event in self.runner.get_step_events(output):
            lvl = event.get("level", "info")
            if lvl == "step":
                step_info = (
                    f"{event.get('phase_label', 'Step')} - {event.get('msg', '')[:100]}"
                )
                yield _ev("step", step_info)
                self._step_history.append(event)
            elif lvl == "phase_transition":
                trans_info = f"Phase transition: {event.get('from_phase', '?')} → {event.get('to_phase', '?')}"
                yield _ev("phase_transition", trans_info)
                self._step_history.append(event)
            elif lvl == "step_progress":
                progress = self.runner.get_step_progress()
                yield _ev(
                    "step_progress",
                    f"Step progress: {progress.current_step}/{progress.total_steps_estimate} ({progress.percentage:.0f}%) - {progress.phase.name.lower()}",
                )

    def _should_extend_timeout(self, progress) -> bool:
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
        yield _ev(
            "heartbeat",
            f"opencode active: step {progress.current_step}/{progress.total_steps_estimate} "
            f"({progress.phase.name.lower()}) — {progress.percentage:.0f}% complete",
        )

    # ------------------------------------------------------------------ #

    def _do_judgement(self, output: str) -> Generator[Event, None, None]:
        progress = self.runner.get_step_progress()
        yield _ev("info", f"🧪  Running tests (step {progress.current_step})…")
        result = self.test_runner.run()
        self._last_result = result
        yield _ev("info", f"Tests: {result.summary()}")

        if self._baseline and result.is_regression_vs(self._baseline):
            yield _ev("warn", "⚠️  Regression — rolling back.")
            yield from self._rollback()
            msg = (
                f"Your changes introduced a regression.\n"
                f"Delta: {result.delta(self._baseline)}\n"
                f"Step progress: {progress.current_step}/{progress.total_steps_estimate}\n"
                f"Output:\n{result.output[-800:]}\n\n"
                "Files rolled back. Fix the regression before continuing."
            )
            safe_rollback, _ = self.guard.sanitize_message(msg)
            yield _ev("opencode_prompt", safe_rollback)
            self.runner.send(safe_rollback)
            return

        cp = self.checkpoints.save(
            f"iter-{self._iteration}-step-{progress.current_step}"
        )
        self._best_cp = cp
        yield _ev("success", f"💾  Checkpoint: {cp} (step {progress.current_step})")

        test_info = f"Step {progress.current_step}/{progress.total_steps_estimate} | Phase: {progress.phase.name.lower()} | Tests: {result.summary()}"
        augmented = f"{output}\n\n--- evolution progress ---\n{test_info}\n\n--- test output ---\n{result.output[-400:]}"
        verdict = self.supervisor.judge(augmented)
        yield _ev("supervisor_response", verdict.raw)

        if verdict.all_targets_met:
            self._state = EvoState.ENDED_SUCCESS
            return

        safe_msg, violations = self.guard.sanitize_message(verdict.feedback)
        if violations:
            yield _ev("warn", f"Blocked out-of-workspace paths: {violations}")
        yield _ev("opencode_prompt", safe_msg)
        self.runner.send(safe_msg)

    def _do_compaction(self) -> Generator[Event, None, None]:
        yield _ev(
            "warn", f"Context at {self.ctx_monitor.fraction * 100:.0f}% — compacting."
        )
        verdict = self.supervisor.ask_for_compaction_instructions()
        yield _ev("supervisor_response", verdict.raw)
        msg, _ = self.guard.sanitize_message(verdict.feedback)
        yield _ev("opencode_prompt", msg)
        self.runner.send(msg)
        self.ctx_monitor.reset()
        yield _ev("info", "Compaction prompt sent.")

    def _handle_failure(self, last_output: str) -> Generator[Event, None, None]:
        self._failures += 1
        yield _ev(
            "warn",
            f"Empty/timeout (failure {self._failures}/{self.config.max_retries}).",
        )
        yield from self._forced_summary(last_output)

        if self._failures >= self.config.max_retries:
            yield _ev("error", "Max retries exceeded.")
            self._state = EvoState.ENDED_FAILURE
            return

        yield _ev("info", "Retrying…")
        self.runner.start(self._restart_prompt())

    def _forced_summary(self, last_output: str) -> Generator[Event, None, None]:
        yield _ev("info", "Writing summary.md…")
        report = self.supervisor.report_final_status(
            reason="forced summarization",
            opencode_output=last_output,
            workspace=self.config.workspace,
        )
        (self.config.workspace / "summary.md").write_text(report, encoding="utf-8")
        yield _ev("info", "summary.md written.")

    def _rollback(self) -> Generator[Event, None, None]:
        if self._best_cp:
            restored = self.checkpoints.restore(self._best_cp)
            yield _ev("info", f"Rolled back {len(restored)} files to: {self._best_cp}")
        else:
            yield _ev("warn", "No checkpoint to roll back to.")

    def _evolution_summary(self) -> Generator[Event, None, None]:
        success = self._state == EvoState.ENDED_SUCCESS
        yield _ev(
            "success" if success else "error",
            f"{'✅' if success else '❌'} Evolution {'completed' if success else 'failed'}.",
        )

        post_snap = snapshot_codebase(self.config.workspace)
        changed = (
            self._pre_snapshot.changed_files(post_snap) if self._pre_snapshot else []
        )

        progress = self.runner.get_step_progress()
        lines = [
            "## Self-Evolution Report\n",
            f"**Outcome:** {'SUCCESS' if success else 'FAILURE'}",
            f"**Iterations:** {self._iteration}",
            f"**Final Step Progress:** {progress.current_step}/{progress.total_steps_estimate} ({progress.percentage:.0f}%)",
            f"**Final Phase:** {progress.phase.name.lower()}",
            f"**Changed files ({len(changed)}):**",
        ] + [f"  - `{f}`" for f in changed]

        if self._step_history:
            lines.append("\n**Step History:**")
            for step_event in self._step_history[-10:]:
                if step_event.get("level") == "step":
                    lines.append(
                        f"  - {step_event.get('phase_label', 'Step')}: {step_event.get('msg', '')[:60]}"
                    )
                elif step_event.get("level") == "phase_transition":
                    lines.append(
                        f"  - ⚡ {step_event.get('from_phase', '?')} → {step_event.get('to_phase', '?')}"
                    )

        if self._baseline and self._last_result:
            lines += [
                f"\n**Baseline:** {self._baseline.summary()}",
                f"**Final:**    {self._last_result.summary()}",
                f"**Delta:**    {self._last_result.delta(self._baseline)}",
            ]
        if self._best_cp:
            lines.append(f"\n**Best checkpoint:** {self._best_cp}")

        yield _ev("info", "Asking supervisor for narrative…")
        narrative = self.supervisor.report_final_status(
            reason="self-evolution completed" if success else "self-evolution failed",
            opencode_output="\n".join(lines),
            workspace=self.config.workspace,
        )
        lines += ["\n---\n", narrative]
        report = "\n".join(lines)

        rp = self.config.workspace / "evolution_report.md"
        rp.write_text(report, encoding="utf-8")
        yield _ev("info", f"evolution_report.md written.")
        yield _ev("report", report)

    # ------------------------------------------------------------------ #

    def _codebase_preamble(self) -> str:
        snap = snapshot_codebase(self.config.workspace)
        return "\n\n## Live codebase\n" + snap.digest_for_prompt(max_files=15)

    def _init_prompt(self) -> str:
        text = self.config.protocol_path.read_text(encoding="utf-8")
        baseline_note = (
            f"\nCurrent test baseline: {self._baseline.summary()}\n"
            if self._baseline
            else ""
        )
        ws = self.config.workspace.resolve()
        return (
            "You are modifying the codebase you live in. Read the protocol carefully.\n\n"
            f"PROTOCOL:\n{text}\n"
            f"{baseline_note}"
            f"\nYour project root (cwd) is: {ws}\n"
            "All files you create or modify MUST be inside this directory.\n"
            "Use relative paths from this directory for all file operations.\n"
            "Never touch .checkpoints/ — that is reserved for the supervisor.\n"
            "Run tests after every logical change. Begin."
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
            "Resuming self-evolution after an error.\n\n"
            f"PROTOCOL:\n{text}\n\n"
            f"LAST SUMMARY:\n{summary}\n\n"
            f"Working directory: {self.config.workspace.resolve()}\n"
            "Continue from the summary."
        )


def _ev(level: str, msg: str) -> Event:
    return {"level": level, "msg": msg}
