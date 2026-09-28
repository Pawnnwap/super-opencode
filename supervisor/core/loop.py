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
import time
from collections.abc import Generator

from supervisor.analyzers.opencode_step_detector import StepProgress
from supervisor.core.llm_supervisor import StepContext, SupervisorVerdict
from supervisor.core.loop_base import BaseLoop, Event, LoopState, _ev
from supervisor.core.occam_razor import OccamRazorStage
from supervisor.utils.config import SupervisorConfig
from supervisor.utils.filesystem.file_ops import safe_read_text

logger = logging.getLogger(__name__)


class SupervisorLoop(BaseLoop):
    def __init__(self, config: SupervisorConfig):
        super().__init__(config)
        _setup_logging(config.log_level)

        self._setup_core_services(agent="build")
        self.supervisor = self._create_supervisor(
            max_protected_files_for_suggestions=(
                config.max_protected_files_for_suggestions
            ),
        )
        self._step_detector_initialized = False
        self._plan_context: str = (
            ""  # populated by _run_plan_mode, carried into _init_prompt
        )
        self._last_plan: str = ""
        self._last_supervisor_feedback: str = ""

        # Goal guard: the judge's DONE is a proposal validated against
        # deterministic workspace evidence before the run may end.
        self._goal_guard = None
        if getattr(config, "enable_goal_guard", True):
            from supervisor.core.goal_guard import GoalGuard

            self._goal_guard = GoalGuard(config, self.protocol)
            self._goal_guard.start()
            self.supervisor.set_goal_context_provider(
                self._goal_guard.judge_context,
            )

        # Judge evidence harness: auto workspace facts + on-demand resources
        # the judge can request via NEED_EVIDENCE (open registry).
        self._judge_harness = None
        if getattr(config, "enable_judge_harness", True):
            from supervisor.core.judge_harness import (
                JudgeHarness,
                audit_tail_block,
                make_builtin_resources,
            )

            self._judge_harness = JudgeHarness()
            for resource in make_builtin_resources(
                config.workspace,
                changed_files=lambda: sorted(self._run_changed_files),
                workspace_tree=self._workspace_tree_block,
                audit_tail=lambda: audit_tail_block(config.workspace),
                run_tests=self._run_test_resource,
            ):
                self._judge_harness.register(resource)
            self.supervisor.register_context_provider(
                "evidence",
                self._judge_harness.render_auto_block,
            )

        # Pre-execution feasibility probe rendered for the judge every turn;
        # returns "" when no facts file exists.
        self.supervisor.register_context_provider(
            "feasibility",
            self._feasibility_section,
        )

        # Supervisor skill bank: catalog in every judge prompt, bodies loaded
        # on the judge's LOAD_SKILL request (bounded, judge-context only).
        self._setup_skill_bank()

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

    def _run(self) -> Generator[Event]:
        yield from self._archive_before_new_run()

        if self.config.plan_mode_rounds > 0:
            yield from self._run_plan_mode()
            if self._state != LoopState.RUNNING:
                return

        yield from self._apply_protection()

        closing = False
        try:
            yield _ev("info", f"Running initial prompt with {self._engine_name}…")

            init_prompt = self._init_prompt()
            yield _ev("opencode_prompt", init_prompt)  # ← full prompt visible
            self.runner.reset_step_detector()
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

        if self._state == LoopState.ENDED_SUCCESS:
            if self.config.enable_occam_razor:
                yield from self._run_occam_razor()
            self._harvest_gates()
            yield _ev("success", "All targets met — run finished successfully.")
        else:
            self._harvest_gates()
            yield _ev(
                "error", "Run ended with failures. See failure_report.md in workspace.",
            )

    def _run_occam_razor(self) -> Generator[Event]:
        """Run optional post-success reduction pass against a copy only."""
        yield _ev(
            "info",
            "[occam] Occam Razor enabled. Starting copy-only reduction pass after live success.",
        )
        protocol_text = safe_read_text(self.config.protocol_path)
        try:
            stage = OccamRazorStage(self.config, protocol_text)
            yield from stage.run()
        except Exception as exc:
            logger.exception("Occam Razor stage crashed")
            yield _ev(
                "warn",
                f"[occam] Stage failed after live success; original final code untouched: {exc}",
            )

    def _run_plan_mode(self) -> Generator[Event]:
        """Run the plan phase for the configured number of rounds.

        A dedicated ``OpencodeRunner`` with ``agent="plan"`` handles all plan
        invocations so opencode stays in read-only mode.  After all rounds the
        final supervisor feedback is stored in ``self._plan_context`` and
        prepended to the build-mode initial prompt by ``_init_prompt()``, giving
        opencode full context of the agreed plan when it starts writing code.

        All supervisor↔opencode exchanges are emitted as ``log-plan_phase``
        events so they appear as a distinct section in the UI event stream.
        """
        from supervisor.runners.factory import create_runner

        total = self.config.plan_mode_rounds
        yield _ev(
            "info",
            f"[plan mode] Starting plan phase ({total} round{'s' if total != 1 else ''})…",
        )

        # Dedicated runner locked to the plan agent — the main self.runner
        # stays untouched and will be used for build mode.
        plan_runner = create_runner(
            self.config,
            agent="plan",
        )

        protocol_text = safe_read_text(self.config.protocol_path)
        ws = self.config.workspace.resolve()
        protected_files_desc = self.guard.get_all_protected_files_description()
        # Known gate ceilings / blocked gates shape a realistic plan as much
        # as the protocol does; the run prompt injects the same block.
        feasibility_block = self._feasibility_section()
        feasibility_desc = f"\n{feasibility_block}\n" if feasibility_block else ""
        plan_prompt = (
            "@explore PLAN MODE. Do NOT create, modify, or delete any files.\n\n"
            "Read protocol below and produce detailed implementation plan:\n"
            "  1. Break work into concrete, ordered steps.\n"
            "  2. Identify dependencies between steps.\n"
            "  3. Flag any ambiguities or risks in the requirements.\n"
            "  4. Do NOT write or edit any source files during this phase.\n\n"
            f"PROTOCOL:\n{protocol_text}\n\n"
            f"Project root (cwd) is: {ws}\n"
            f"{protected_files_desc}\n"
            f"{feasibility_desc}"
            "Output plan now."
        )

        last_feedback: str = ""
        last_plan_output: str = ""

        for round_num in range(1, total + 1):
            yield _ev(
                "log-plan_phase",
                f"[plan mode] Round {round_num}/{total} — sending prompt to {self._engine_name}…",
            )

            # Round 1: full plan prompt.  Subsequent rounds: supervisor feedback only.
            if last_feedback:
                prompt = (
                    f"[plan mode — round {round_num}/{total}]\n\n"
                    "Supervisor feedback on previous plan:\n"
                    f"{last_feedback}\n\n"
                    "Revise plan accordingly. Do NOT modify any files."
                )
            else:
                prompt = plan_prompt

            yield _ev("opencode_prompt", prompt)
            plan_runner.reset_step_detector()
            if round_num > 1:
                plan_runner.enable_continuation(True)
            yield from plan_runner.start(prompt)
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
                "Implement above plan. Create and modify files."
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

    def _on_successful_output(self, output: str) -> Generator[Event]:
        yield from super()._on_successful_output(output)
        yield from self._refresh_supervisor_snapshot()

    def _get_verdict(self, output: str, progress) -> SupervisorVerdict:
        step_context = self._get_step_context(progress)
        self._fulfill_pending_skills()
        verdict = self.supervisor.judge_with_step_context(output, step_context)
        # Kept for the run-end gate harvest (PROV record of outcomes).
        self._last_verdict = verdict
        loaded_skills = self._resolve_skill_requests(verdict)
        evidence = ""
        if self._judge_harness and verdict.evidence_requests:
            # One bounded resolution round: gather the requested resources
            # and let the judge re-evaluate with real evidence in hand.
            evidence = self._judge_harness.fulfill(verdict.evidence_requests)
            if evidence:
                logger.info(
                    "Judge requested evidence: %s",
                    ", ".join(verdict.evidence_requests[:3]),
                )
        if evidence or loaded_skills:
            parts = [output]
            if evidence:
                parts.append(
                    "--- HARNESS EVIDENCE (resolved on request) ---\n"
                    + evidence
                    + "\n--- end evidence ---\n"
                    "Re-evaluate the targets using this evidence."
                )
            if loaded_skills:
                # Bodies arrive via the "skills" context provider rendered
                # into every judge prompt; the augmentation just tells the
                # judge to apply them now.
                parts.append(
                    "--- SUPERVISOR SKILLS LOADED ---\n"
                    f"Newly loaded into your context: {', '.join(loaded_skills)}.\n"
                    "Re-evaluate applying their guidance.\n--- end skills ---"
                )
            verdict = self.supervisor.judge_with_step_context(
                "\n\n".join(parts), step_context,
            )
            # No load chains: requests seen in the re-judge wait for next turn.
            self._queue_deferred_skill_requests(verdict)
        return verdict

    def _workspace_tree_block(self) -> str:
        snapshot = self._cached_snapshot
        if snapshot is None:
            return ""
        lines = snapshot.tree().splitlines()
        return "Workspace tree:\n" + "\n".join(lines[:40])

    def _run_test_resource(self) -> str:
        from supervisor.runners.test_runner import OcTestRunner

        result = OcTestRunner(self.config.workspace).run()
        return f"Test suite: {result.summary()}\n{result.output[-800:]}"

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

    def _handle_failure(self, last_output: str) -> Generator[Event]:
        time.sleep(3)
        yield from super()._handle_failure(last_output)

    def _on_final_failure(self, output: str) -> Generator[Event]:
        yield from super()._on_final_failure(output)
        report = self.supervisor.report_final_status(
            reason=f"{self._engine_name} failed {self._failures} consecutive times",
            opencode_output=output,
        )
        self._write(report, "failure_report.md")
        yield _ev(
            "error",
            f"All {self.config.max_retries} {'retry' if self.config.max_retries == 1 else 'retries'} exhausted. "
            f"Run terminated after {self._failures} failures.\n\n{report}",
        )

    def _verify_success(
        self,
        verdict: SupervisorVerdict,
        output: str,
    ) -> str | None:
        if self._goal_guard is None:
            return None
        decision = self._goal_guard.evaluate_done(
            verdict,
            sorted(self._run_changed_files),
        )
        if decision.accepted:
            return None
        return self._goal_guard.blocked_feedback(decision, verdict)

    def _turn_guidance(self, verdict: SupervisorVerdict, output: str) -> str:
        if self._goal_guard is None:
            return ""
        decision = self._goal_guard.evaluate_turn(
            verdict,
            worktree_sig=self._last_snapshot_signature,
            worktree_changed=bool(self._last_changed_files),
            output=output,
        )
        return decision.guidance

    def _guard_restart_section(self) -> str:
        if self._goal_guard is None:
            return ""
        context = self._goal_guard.restart_context()
        return f"{context}\n\n" if context else ""

    def _init_prompt(self) -> str:
        from supervisor.prompts import INIT_PROMPT_TEMPLATE

        text = safe_read_text(self.config.protocol_path)
        ws = self.config.workspace.resolve()
        protected_files_desc = self.guard.get_all_protected_files_description()
        goal_section = self._goal_checklist_section()
        feasibility_block = self._feasibility_section()
        if feasibility_block:
            goal_section = (
                goal_section + "\n\n" + feasibility_block
                if goal_section
                else feasibility_block
            )
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
            protocol_text=text,
            goal_section=goal_section,
            plan_section=plan_section,
            plan_output_section=plan_output_section,
            workspace=ws,
            protected_files_desc=protected_files_desc,
        )

    def _feasibility_section(self) -> str:
        from pathlib import Path

        from supervisor.protocols.feasibility_gate import (
            check_feasibility,
            default_facts_path,
            load_feasibility_facts,
            parse_gates,
            render_feasibility_block,
        )
        from supervisor.protocols.gate_rubric import (
            render_scorecard,
            validate_refs,
        )

        cfg_path = getattr(self.config, "feasibility_facts_path", "") or ""
        path = Path(cfg_path) if cfg_path else default_facts_path(self.config.workspace)
        facts = load_feasibility_facts(path)
        if not facts:
            return ""
        gates = parse_gates(facts)
        if gates:
            # Drift guard: refs must still point inside the current TARGET.
            from supervisor.protocols.feasibility_wizard import count_target_gates

            drift = validate_refs(
                gates,
                count_target_gates(
                    safe_read_text(self.config.protocol_path),
                ),
            )
            if drift and drift != getattr(self, "_rubric_drift_logged", None):
                logger.warning(
                    "gate rubric drift (protocol changed?): %s", "; ".join(drift),
                )
                self._rubric_drift_logged = drift
        assessment = check_feasibility(facts, facts_source=str(path))
        logger.info(
            "feasibility gate: reachable=%s required_gates=%s "
            "known_ceiling=%s blocked=%s",
            assessment.reachable,
            assessment.required_gates,
            assessment.known_ceiling,
            ", ".join(assessment.blocked_gates),
        )
        block = render_feasibility_block(
            assessment,
            max_chars=int(
                getattr(self.config, "max_feasibility_block_chars", 2200),
            ),
        )
        if gates:
            # Layer-1 injection: closed-vocabulary scorecard; determined
            # facts come from the last harvest (live evaluation happens at
            # the DONE exit check, not per turn).
            block += "\n" + render_scorecard(
                gates, {}, last_harvest=self._last_gate_harvest(),
            )
        return block

    def _last_gate_harvest(self) -> dict | None:
        import json

        path = self.config.workspace / "logs" / "gate_results_last.json"
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            pass
        return None

    def _harvest_gates(self) -> None:
        """Run end: PROV record of gate outcomes + guarded status transitions.

        Machine gates are re-evaluated once (bounded); manual results come
        from the final judge verdict. PASS flips gates to passing with
        provenance; FAIL streaks only *propose* blocked for the operator.
        """
        import json

        from supervisor.protocols.feasibility_gate import (
            default_facts_path,
            load_feasibility_facts,
            parse_gates,
            write_feasibility_facts,
        )
        from supervisor.protocols.gate_rubric import (
            apply_harvest,
            build_harvest,
            evaluate_rubric,
        )

        try:
            path = default_facts_path(self.config.workspace)
            facts = load_feasibility_facts(path)
            gates = parse_gates(facts or {})
            if not gates:
                return
            evaluations = evaluate_rubric(gates, self.config.workspace)
            verdict = getattr(self, "_last_verdict", None)
            manual: dict[str, tuple[bool, str]] = {}
            if verdict is not None:
                for result in verdict.criteria_results:
                    text = str(getattr(result, "criterion", "") or "")
                    for gate in gates:
                        if gate.name and gate.name in text:
                            manual[gate.name] = (
                                bool(getattr(result, "met", False)),
                                str(getattr(result, "evidence", "") or ""),
                            )
            harvest = build_harvest(
                gates,
                evaluations,
                manual,
                run_id=time.strftime("%Y%m%d-%H%M%S"),
                protocol_text=safe_read_text(self.config.protocol_path),
            )
            logs_dir = self.config.workspace / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            (logs_dir / "gate_results_last.json").write_text(
                json.dumps(harvest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            updated = apply_harvest(dict(facts or {}), harvest)
            write_feasibility_facts(updated, path, delete_when_empty=False)
            logger.info(
                "gate harvest: %d result(s) recorded, pending_blocked=%s",
                len(harvest["results"]),
                [p.get("gate") for p in updated.get("pending_blocked", [])],
            )
        except Exception:  # noqa: BLE001 — harvesting must never fail the run
            logger.warning("gate harvest failed", exc_info=True)

    def _goal_checklist_section(self) -> str:
        """Layer-1 constraint injection: the concrete definition of done."""
        if self._goal_guard is None:
            return ""
        criteria = self._goal_guard.criteria_from_protocol()
        if not criteria:
            return ""
        lines = ["## GOAL CHECKLIST (the run ends only when ALL of these hold)"]
        lines.extend(f"{i}. {c}" for i, c in enumerate(criteria, start=1))
        lines.append("")
        return "\n".join(lines)

    def _restart_prompt(self) -> str:
        summary, text = self._get_restart_context()
        tail = self._restart_task_state()
        journal_block = f"PROGRESS JOURNAL:\n{tail}\n\n" if tail else ""
        return (
            "Resuming previous session. Context was cleared.\n\n"
            f"PROTOCOL:\n{text}\n\n"
            f"LAST SUMMARY:\n{summary}\n\n"
            f"{journal_block}"
            f"Working directory: {self.config.workspace.resolve()}\n"
            "Continue from where the summary left off."
        )


def _setup_logging(level: str) -> None:
    # threadName lets concurrent jobs be distinguished on shared stderr.
    # JobManager sets worker-thread names to "job-<id>" for this reason.
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
