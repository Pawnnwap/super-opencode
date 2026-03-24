"""
supervisor/self_evolution_loop.py

Self-evolution loop using the CLI-based opencode runner.
Each turn: opencode -p "<prompt>" runs, exits, output is captured.
Tests run after each turn; regressions trigger rollback.
Versions are archived in the .archive/ directory.
"""

from __future__ import annotations

import logging
import time
from enum import Enum, auto
from typing import Generator

from supervisor.analyzers.codebase_analyzer import snapshot_codebase, CodebaseSnapshot
from supervisor.utils.checkpoint import CheckpointManager, Checkpoint
from supervisor.utils.config import SupervisorConfig
from supervisor.monitoring.context_monitor import ContextMonitor
from supervisor.core.llm_supervisor import LLMSupervisor, StepContext
from supervisor.core.loop_base import BaseLoop, Event, _ev
from supervisor.runners.opencode_runner import OpencodeRunner
from supervisor.analyzers.opencode_step_detector import OpencodeStepDetector
from supervisor.protocols.protocol import load_protocol
from supervisor.runners.test_runner import RunTestResult, OcTestRunner
from supervisor.workspace.workspace_archiver import WorkspaceArchiver, ArchiveResult
from supervisor.workspace.workspace_guard import WorkspaceGuard

logger = logging.getLogger(__name__)

class EvoState(Enum):
    RUNNING = auto()
    ENDED_SUCCESS = auto()
    ENDED_FAILURE = auto()


class SelfEvolutionLoop(BaseLoop):
    def __init__(self, config: SupervisorConfig):
        super().__init__()
        self.config = config
        self.protocol = load_protocol(config.protocol_path)
        self._cached_snapshot = snapshot_codebase(self.config.workspace)
        self.supervisor = LLMSupervisor(
            self.protocol,
            config.workspace,
            config.supervisor_model,
            extra_system=self._codebase_preamble(),
            read_external_feedback=config.read_external_feedback,
            max_tokens=config.max_tokens,
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
        self.checkpoints = CheckpointManager(config.workspace)
        self.test_runner = OcTestRunner(config.workspace)
        self.archiver = WorkspaceArchiver(config.workspace)
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
        self._current_archive_result: ArchiveResult | None = None
        self._evolution_logs: list[str] = []

    # ------------------------------------------------------------------ #

    def _run(self) -> Generator[Event, None, None]:
        yield _ev("info", "📸  Snapshotting codebase…")
        self._pre_snapshot = self._cached_snapshot

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

        yield _ev("info", "📦  Creating initial archive…")
        self._current_archive_result = self.archiver.archive_workspace(
            label="pre-evolution",
        )
        yield _ev("info", f"Archive created: {self._current_archive_result.archive_path}")

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
            yield from self._update_context_monitor()

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
                yield from self._emit_heartbeat(current_progress)

            if self.ctx_monitor.should_compact:
                self.supervisor.compact_history()
                yield from self._do_compaction()
                output, timed_out = self.runner.read_output()
                continue

            yield from self._do_judgement(output)
            if self._state != EvoState.RUNNING:
                break
            output, timed_out = self.runner.read_output()

        self.runner.stop()
        yield from self._evolution_summary()

    # ------------------------------------------------------------------ #

    def _do_judgement(self, output: str) -> Generator[Event, None, None]:
        progress = self.runner.get_step_progress()
        yield _ev("info", f"🧪  Running tests (step {progress.current_step})…")
        result = self.test_runner.run()
        self._last_result = result
        yield _ev("info", f"Tests: {result.summary()}")

        yield from self._emit_token_warnings()

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

        archive_label = f"iter-{self._iteration}-step-{progress.current_step}"
        archive_result = self.archiver.archive_workspace(
            label=archive_label,
        )
        self._current_archive_result = archive_result
        yield _ev("success", f"📦  Archive saved: {archive_result.archive_path}")

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

        suggestions = self.supervisor.generate_suggestions(
            opencode_output=augmented,
        )
        if suggestions and "no suggestions" not in suggestions.lower():
            yield _ev("supervisor_suggestions", suggestions)

    def _handle_failure(self, last_output: str) -> Generator[Event, None, None]:
        self._failures += 1
        retries_remaining = max(0, self.config.max_retries - self._failures)
        
        yield _ev(
            "warn",
            f"Empty/timeout (failure {self._failures}/{self.config.max_retries}, "
            f"{retries_remaining} {'retry' if retries_remaining == 1 else 'retries'} remaining).",
        )
        yield from self._forced_summary(last_output)

        if self._failures >= self.config.max_retries:
            yield _ev(
                "error",
                f"All {self.config.max_retries} {'retry' if self.config.max_retries == 1 else 'retries'} exhausted. "
                f"Self-evolution terminated after {self._failures} failures."
            )
            self._state = EvoState.ENDED_FAILURE
            return

        yield _ev(
            "info",
            f"Retrying… (attempt {self._failures}/{self.config.max_retries})"
        )
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

        # Reuse cached snapshot for the "before" state; only snapshot once for "after"
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

        yield _ev("info", "📦  Creating final archive…")
        final_archive_result = self.archiver.archive_workspace(
            label=f"final-{self._iteration}-iterations",
        )
        yield _ev("info", f"Final archive created: {final_archive_result.archive_path}")

        all_archives = self.archiver.list_archives()
        lines.append(f"\n**Archives saved:** {len(all_archives)}")
        for arch in all_archives[-5:]:
            lines.append(f"  - {arch.get('name', arch)}")

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
        return "\n\n## Live codebase\n" + self._cached_snapshot.digest_for_prompt(max_files=15)

    def _init_prompt(self) -> str:
        text = self.config.protocol_path.read_text(encoding="utf-8")
        baseline_note = (
            f"\nCurrent test baseline: {self._baseline.summary()}\n"
            if self._baseline
            else ""
        )
        ws = self.config.workspace.resolve()
        protected_files_desc = self.guard.get_all_protected_files_description()
        return (
            "You are modifying the codebase you live in. Read the protocol carefully.\n\n"
            f"PROTOCOL:\n{text}\n"
            f"{baseline_note}"
            f"\nYour project root (cwd) is: {ws}\n"
            "All files you create or modify MUST be inside this directory.\n"
            "Use relative paths from this directory for all file operations.\n"
            "Never touch .checkpoints/ — that is reserved for the supervisor.\n"
            "All versions are automatically archived in the .archive/ directory.\n"
            "Do NOT delete or manually manage version files — the archive system handles this.\n"
            "Do NOT delete or modify the .opencode directory or its contents.\n"
            f"{protected_files_desc}\n"
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
            "All versions are automatically archived in .archive/. Do NOT delete version files manually.\n"
            "Continue from the summary."
        )
