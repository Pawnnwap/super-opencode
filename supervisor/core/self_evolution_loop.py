"""
supervisor/core/self_evolution_loop.py

Self-evolution loop using the CLI-based opencode runner.
Each turn: opencode -p "<prompt>" runs, exits, output is captured.
Tests run after each turn; regressions trigger rollback.
Versions are archived in the .archive/ directory.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Generator

from supervisor.analyzers.codebase_analyzer import (CodebaseSnapshot,
                                                    snapshot_codebase)
from supervisor.core.llm_supervisor import LLMSupervisor
from supervisor.core.loop_base import BaseLoop, Event, LoopState, _ev
from supervisor.protocols.protocol import load_protocol
from supervisor.runners.test_runner import OcTestRunner, RunTestResult
from supervisor.utils.config import SupervisorConfig
from supervisor.utils.gitignore_utils import update_gitignore_files
from supervisor.workspace.workspace_archiver import (ArchiveResult,
                                                     WorkspaceArchiver)

logger = logging.getLogger(__name__)


class SelfEvolutionLoop(BaseLoop):
    def __init__(self, config: SupervisorConfig):
        super().__init__(config)

        # Update .gitignore files before any other operations
        modified_gitignores = update_gitignore_files(config.workspace)
        if modified_gitignores:
            logger.info(
                f"Modified {len(modified_gitignores)} .gitignore file(s): {[str(p) for p in modified_gitignores]}"
            )

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
        self._init_components()
        self.test_runner = OcTestRunner(config.workspace)
        self.archiver = WorkspaceArchiver(config.workspace)

        self._baseline: RunTestResult | None = None
        self._last_result: RunTestResult | None = None
        self._best_archive: Path | None = None
        self._iteration = 0
        self._pre_snapshot: CodebaseSnapshot | None = None
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

        yield _ev("info", "📦  Saving pre-evolution archive…")
        pre_archive = self.archiver.archive_workspace(label="pre-evolution baseline")
        self._best_archive = pre_archive.archive_path
        self._current_archive_result = pre_archive
        yield _ev("info", f"Archive saved: {pre_archive.archive_path}")

        yield from self._apply_protection()

        try:
            yield _ev("info", "🚀  Starting opencode for self-evolution…")
            init_prompt = self._init_prompt()
            yield _ev("opencode_prompt", init_prompt)
            self.runner.start(init_prompt)
            self._last_step_time = time.time()
            output, timed_out = self.runner.read_output()

            yield from self._run_loop(output, timed_out)
        finally:
            yield from self._remove_protection()

            self.runner.stop()
            yield from self._evolution_summary()

    # ------------------------------------------------------------------ #

    def _on_successful_output(self, output: str) -> Generator[Event, None, None]:
        self._iteration += 1
        yield _ev(
            "info",
            f"[iter {self._iteration}] opencode output ({len(output)} chars)",
        )

    def _pre_judge(self, output: str, progress) -> Generator[Event, None, tuple[str | None, bool]]:
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
        return augmented, False

    def _get_verdict(self, output: str, progress) -> "SupervisorVerdict":
        return self.supervisor.judge(output)

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
                f"Self-evolution terminated after {self._failures} failures.",
            )
            self._state = LoopState.ENDED_FAILURE
            return

        yield _ev(
            "info", f"Retrying… (attempt {self._failures}/{self.config.max_retries})"
        )
        self.runner.start(self._restart_prompt())

    def _rollback(self) -> Generator[Event, None, None]:
        if self._best_archive:
            restored = self.archiver.restore_archive(self._best_archive)
            yield _ev(
                "info", f"Rolled back {len(restored)} files from: {self._best_archive}"
            )
        else:
            yield _ev("warn", "No archive to roll back to.")

    def _evolution_summary(self) -> Generator[Event, None, None]:
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
        return "\n\n## Live codebase\n" + self._cached_snapshot.digest_for_prompt(
            max_files=15
        )

    def _init_prompt(self) -> str:
        from supervisor.prompts import (HASHLINE_SYSTEM_INSTRUCTIONS,
                                        SELF_EVOLUTION_INIT_PROMPT_TEMPLATE)

        text = self.config.protocol_path.read_text(encoding="utf-8")
        baseline_note = (
            f"\nCurrent test baseline: {self._baseline.summary()}\n"
            if self._baseline
            else ""
        )
        ws = self.config.workspace.resolve()
        protected_files_desc = ""
        return SELF_EVOLUTION_INIT_PROMPT_TEMPLATE.format(
            hashline_instructions=HASHLINE_SYSTEM_INSTRUCTIONS,
            protocol_text=text,
            baseline_note=baseline_note,
            workspace=ws,
            protected_files_desc=protected_files_desc,
        )

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
