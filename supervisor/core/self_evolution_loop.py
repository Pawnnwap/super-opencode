"""supervisor/core/self_evolution_loop.py

Self-evolution loop using the CLI-based opencode runner.
Each turn: opencode -p "<prompt>" runs, exits, output is captured.
Tests run after each turn; regressions trigger rollback.
Versions are archived in the .archive/ directory.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from pathlib import Path

from supervisor.analyzers.codebase_analyzer import CodebaseSnapshot, snapshot_codebase
from supervisor.core.loop_base import BaseLoop, Event, LoopState, _ev
from supervisor.runners.test_runner import OcTestRunner, RunTestResult
from supervisor.utils.config import SupervisorConfig
from supervisor.utils.experience_tracker import (
    get_experience_context,
    update_experience,
)
from supervisor.workspace.workspace_archiver import ArchiveResult

logger = logging.getLogger(__name__)


class SelfEvolutionLoop(BaseLoop):
    def __init__(self, config: SupervisorConfig):
        super().__init__(config)

        self._setup_core_services()
        self.supervisor = self._create_supervisor()
        self.test_runner = OcTestRunner(config.workspace)

        self._baseline: RunTestResult | None = None
        self._last_result: RunTestResult | None = None
        self._best_archive: Path | None = None
        self._iteration = 0
        self._pre_snapshot: CodebaseSnapshot | None = None
        self._current_archive_result: ArchiveResult | None = None
        self._evolution_logs: list[str] = []

    # ------------------------------------------------------------------ #

    def _run(self) -> Generator[Event]:
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

        yield _ev("info", "📦  Saving pre-evolution archive…")
        pre_archive = self.archiver.archive_workspace(label="pre-evolution baseline")
        self._best_archive = pre_archive.archive_path
        self._current_archive_result = pre_archive
        yield _ev("info", f"Archive saved: {pre_archive.archive_path}")

        yield from self._apply_protection()

        closing = False
        try:
            yield _ev("info", "🚀  Starting opencode for self-evolution…")
            init_prompt = self._init_prompt()
            yield _ev("opencode_prompt", init_prompt)
            yield from self.runner.start(init_prompt)
            self._last_step_time = time.time()
            output, timed_out = self.runner.read_output()

            yield from self._run_loop(output, timed_out)
        except GeneratorExit:
            closing = True
            self._cleanup_after_generator_close()
            raise
        finally:
            if not closing:
                yield from self._remove_protection()

                self.runner.stop()
                yield from self._evolution_summary()

    def _on_successful_output(self, output: str) -> Generator[Event]:
        self._iteration += 1
        yield _ev(
            "info",
            f"[iter {self._iteration}] opencode output ({len(output)} chars)",
        )
        yield from super()._on_successful_output(output)
        yield from self._refresh_supervisor_snapshot()
        update_experience(
            self.config.workspace,
            worked=[f"Iteration {self._iteration} output processed successfully"],
        )

    def _pre_judge(self, output: str, progress) -> Generator[Event, None, tuple[str | None, bool]]:
        yield _ev("info", f"🧪  Running tests (step {progress.current_step})…")
        result = self.test_runner.run()
        self._last_result = result
        yield _ev("info", f"Tests: {result.summary()}")

        experience_ctx = get_experience_context(self.config.workspace)
        if experience_ctx.strip():
            yield _ev("info", "Experience context loaded for judgment.")

        if self._baseline and result.is_regression_vs(self._baseline):
            yield _ev("warn", "⚠️  Regression — rolling back.")
            yield from self._rollback()
            insights = self._extract_regression_insights(experience_ctx)
            msg = (
                f"Regression detected. {insights}\n"
                f"Delta: {result.delta(self._baseline)}\n"
                f"Step progress: {progress.current_step}/{progress.total_steps_estimate}\n"
                f"Output:\n{result.output[-800:]}\n\n"
                "Files rolled back. Fix the regression before continuing."
            )
            safe_rollback, _ = self.guard.sanitize_message(msg)
            yield _ev("opencode_prompt", safe_rollback)
            yield from self.runner.send(safe_rollback)
            update_experience(self.config.workspace, failed=[f"Regression at step {progress.current_step}: {result.delta(self._baseline)}"])
            return None, True

        archive_label = f"iter-{self._iteration}-step-{progress.current_step}"
        archive_result = self.archiver.archive_workspace(
            label=archive_label,
        )
        self._best_archive = archive_result.archive_path
        self._current_archive_result = archive_result
        yield _ev(
            "success",
            f"📦  Archive saved: {archive_result.archive_path} (step {progress.current_step})",
        )

        test_info = f"Step {progress.current_step}/{progress.total_steps_estimate} | Phase: {progress.phase.name.lower()} | Tests: {result.summary()}"
        augmented = f"{output}\n\n--- evolution progress ---\n{test_info}\n\n--- test output ---\n{result.output[-400:]}"
        if experience_ctx.strip():
            augmented += f"\n\n--- experience context ---\n{experience_ctx}"
        return augmented, False

    def _get_verdict(self, output: str, progress) -> SupervisorVerdict:
        return self.supervisor.judge(output)

    def _handle_failure(self, last_output: str) -> Generator[Event]:
        yield from super()._handle_failure(last_output)

    def _rollback(self) -> Generator[Event]:
        if self._best_archive:
            restored = self.archiver.restore_archive(self._best_archive)
            yield _ev(
                "info", f"Rolled back {len(restored)} files from: {self._best_archive}",
            )
        else:
            yield _ev("warn", "No archive to roll back to.")

    def _extract_regression_insights(self, experience_ctx: str) -> str:
        if not experience_ctx.strip():
            return ""
        lines = experience_ctx.splitlines()
        relevant = []
        for line in lines:
            if "failed" in line.lower() or "regression" in line.lower() or "violation" in line.lower():
                relevant.append(line.strip())
        if not relevant:
            return ""
        return "Modifying previous changes caused issues. " + " | ".join(relevant[:3]) + ". Try alternative approach."

    def _evolution_summary(self) -> Generator[Event]:
        success = self._state == LoopState.ENDED_SUCCESS
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
                        f"  - {step_event.get('phase_label', 'Step')}: {step_event.get('msg', '')[:60]}",
                    )
                elif step_event.get("level") == "phase_transition":
                    lines.append(
                        f"  - ⚡ {step_event.get('from_phase', '?')} → {step_event.get('to_phase', '?')}",
                    )

        if self._baseline and self._last_result:
            lines += [
                f"\n**Baseline:** {self._baseline.summary()}",
                f"**Final:**    {self._last_result.summary()}",
                f"**Delta:**    {self._last_result.delta(self._baseline)}",
            ]
        if self._best_archive:
            lines.append(f"\n**Best archive:** {self._best_archive}")

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
        )
        lines += ["\n---\n", narrative]
        report = "\n".join(lines)

        if self._write(report, "evolution_report.md"):
            yield _ev("info", "evolution_report.md written.")
        else:
            yield _ev("warn", "Could not write evolution_report.md due to permission errors.")
        yield _ev("report", report)

    # ------------------------------------------------------------------ #

    def _init_prompt(self) -> str:
        from supervisor.prompts import SELF_EVOLUTION_INIT_PROMPT_TEMPLATE

        text = self.config.protocol_path.read_text(encoding="utf-8")
        baseline_note = (
            f"\nCurrent test baseline: {self._baseline.summary()}\n"
            if self._baseline
            else ""
        )
        ws = self.config.workspace.resolve()
        protected_files_desc = ""

        experience_ctx = get_experience_context(self.config.workspace)
        experience_note = ""
        if experience_ctx.strip():
            experience_note = f"\n\n--- Previous Evolution Experience ---\n{experience_ctx}\n"

        prompt = SELF_EVOLUTION_INIT_PROMPT_TEMPLATE.format(
            protocol_text=text,
            baseline_note=baseline_note + experience_note,
            workspace=ws,
            protected_files_desc=protected_files_desc,
        )
        return prompt

    def _restart_prompt(self) -> str:
        summary, text = self._get_restart_context()
        return (
            "Resuming self-evolution after an error.\n\n"
            f"PROTOCOL:\n{text}\n\n"
            f"LAST SUMMARY:\n{summary}\n\n"
            f"Working directory: {self.config.workspace.resolve()}\n"
            "All versions are automatically archived in .archive/. Do NOT delete version files manually.\n"
            "Continue from the summary."
        )
