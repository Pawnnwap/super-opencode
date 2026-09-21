"""Goal-level guardrail for supervised runs: propose -> validate -> execute -> audit.

The judge's "all targets met" is treated as a *proposal*, never an acceptance
(SWE-agent submit semantics). Before the loop may end, the guard validates the
proposal against deterministic evidence (book ch.8 exit validation):

1. Criteria consistency — a structured verdict that still lists [UNMET]
   targets cannot be DONE (contradiction check).
2. Workspace evidence — the run must have produced observable file changes
   (excluding supervisor-generated markdown), unless the goal explicitly
   allows zero-change completion.
3. Optional test gate — when enabled, the workspace test suite must pass.

Every proposal — approved or blocked — is appended to
``.opencode/goal_audit.jsonl`` with the evidence snapshot, so a run can be
replayed and audited after the fact. Between DONE proposals the guard tracks
turn-level progress: workspace stagnation escalates through a nudge ladder and
repeated rejected directions surface as replan signals (via target_state), and
Reflexion-style lessons are recorded for injection into later prompts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from supervisor.analyzers.stagnation_detector import (
    StagnationSignal,
    WorkspaceStagnationDetector,
)
from supervisor.core.llm_support.models import SupervisorVerdict
from supervisor.core.target_state import (
    initialise_target_state,
    load_target_state,
    record_lesson,
    record_target_iteration,
)
from supervisor.protocols.protocol import Protocol
from supervisor.utils.config import SupervisorConfig
from supervisor.utils.text_utils import head_tail_excerpt

logger = logging.getLogger(__name__)

_AUDIT_FILE = "goal_audit.jsonl"
_MAX_AUDIT_FILES_LISTED = 8
_MAX_LESSON_LEN = 240


def _looks_like_test_file(path: str) -> bool:
    """Heuristic: does this changed path look like a test/verification file?"""
    name = Path(path).name.lower()
    stem = Path(path).stem.lower()
    return (
        name.startswith("test_")
        or "_test." in name
        or ".test." in name
        or stem in {"conftest", "tests"}
        or "spec" in stem
    )


@dataclass
class DoneDecision:
    """Outcome of validating a DONE proposal."""

    accepted: bool
    reason: str
    unverified: bool = False  # accepted after blocked-stop cap, evidence incomplete
    unmet_criteria: list[str] = field(default_factory=list)


@dataclass
class TurnDecision:
    """Guidance produced for a normal (continue) judged turn."""

    guidance: str = ""  # prepended to the agent feedback
    stagnation: StagnationSignal | None = None


class GoalGuard:
    """Owns goal criteria, the DONE verification gate, and the audit trail."""

    def __init__(self, config: SupervisorConfig, protocol: Protocol):
        self._config = config
        self._protocol = protocol
        self._workspace = Path(config.workspace)
        self._stagnation = WorkspaceStagnationDetector()
        self._blocked_stops = 0
        self._turn = 0
        self._baseline_files: set[str] = set()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Derive success criteria from the TARGET and open the audit trail."""
        criteria = self.criteria_from_protocol()
        initialise_target_state(
            self._workspace,
            target=self._protocol.target_section.strip(),
            success_criteria=criteria,
        )
        self._audit(
            "goal_start",
            decision="initialized",
            reason=f"{len(criteria)} acceptance criteria derived from TARGET",
        )

    def criteria_from_protocol(self) -> list[str]:
        """Numbered/bulleted lines of the TARGET section become the checklist."""
        lines: list[str] = []
        for raw in self._protocol.target_section.splitlines():
            line = raw.strip()
            if not line:
                continue
            cleaned = line.lstrip("-*•").strip()
            cleaned = cleaned.lstrip("0123456789.").strip()
            if not cleaned:
                continue
            # A TARGET is usually one paragraph; keep the checklist compact by
            # splitting only on explicit list items, one criterion per line.
            # Criteria often end with the measurable condition — keep both ends.
            if len(cleaned) > 300:
                cleaned = head_tail_excerpt(cleaned, 300, end_ratio=0.5)
            lines.append(cleaned)
        return lines[:8]

    # ------------------------------------------------------------------ #
    # DONE verification gate (exit validation)                            #
    # ------------------------------------------------------------------ #

    def evaluate_done(
        self,
        verdict: SupervisorVerdict,
        changed_files: list[str],
    ) -> DoneDecision:
        """Validate a DONE proposal. Returns accept or a blocking reason."""
        self._turn += 1
        unmet = [c.criterion for c in verdict.unmet_criteria]

        max_blocked = max(0, int(getattr(self._config, "max_blocked_stops", 2)))
        if self._blocked_stops >= max_blocked:
            # Cap reached — accept rather than loop forever; flag as unverified.
            self._blocked_stops += 1
            decision = DoneDecision(
                accepted=True,
                reason=(
                    f"accepted after {self._blocked_stops - 1} blocked DONE "
                    "proposals — judge insistence outweighs remaining doubt"
                ),
                unverified=True,
                unmet_criteria=unmet,
            )
            logger.warning(
                "Goal guard accepting UNVERIFIED completion after %d blocked "
                "proposal(s): %s",
                self._blocked_stops - 1,
                decision.reason,
            )
            self._audit(
                "done_accepted_unverified",
                decision="accepted",
                reason=decision.reason,
                all_targets_met=True,
                unmet_criteria=unmet,
                changed_files=self._evidence_files(changed_files),
            )
            return decision

        if unmet:
            decision = self._block(
                verdict,
                changed_files,
                unmet=unmet,
                reason="verdict claims DONE while criteria remain UNMET",
            )
            return decision

        evidence_files = self._evidence_files(changed_files)
        if getattr(self._config, "goal_require_changes", True) and not evidence_files:
            decision = self._block(
                verdict,
                changed_files,
                unmet=[],
                reason=(
                    "no observable workspace change since run start — "
                    "completion without evidence is not accepted"
                ),
            )
            return decision

        if getattr(self._config, "goal_verify_tests", False):
            ok, detail = self._run_test_gate()
            if not ok:
                decision = self._block(
                    verdict,
                    changed_files,
                    unmet=[],
                    reason=f"test gate failed: {detail}",
                )
                return decision

        self._stagnation.reset()
        evidence_files = self._evidence_files(changed_files)
        red_flags = [f for f in evidence_files if _looks_like_test_file(f)]
        reason = "all criteria met with workspace evidence"
        if red_flags:
            reason += (
                f" — NOTE: verification files were modified ({', '.join(red_flags[:3])});"
                " confirm they were extended, not weakened"
            )
            logger.warning(
                "Goal guard red flag: DONE accepted while verification files "
                "were modified: %s",
                ", ".join(red_flags),
            )
        decision = DoneDecision(
            accepted=True,
            reason=reason,
            unmet_criteria=unmet,
        )
        record_target_iteration(
            self._workspace,
            iteration=self._turn,
            direction=verdict.feedback or "done",
            evidence="; ".join(evidence_files[:_MAX_AUDIT_FILES_LISTED]),
            accepted=True,
            reason=decision.reason,
        )
        self._audit(
            "done_accepted",
            decision="accepted",
            reason=decision.reason,
            all_targets_met=True,
            unmet_criteria=unmet,
            changed_files=evidence_files,
            test_file_edits=red_flags,
        )
        return decision

    def _block(
        self,
        verdict: SupervisorVerdict,
        changed_files: list[str],
        *,
        unmet: list[str],
        reason: str,
    ) -> DoneDecision:
        self._blocked_stops += 1
        decision = DoneDecision(
            accepted=False,
            reason=reason,
            unmet_criteria=unmet,
        )
        self._audit(
            "done_blocked",
            decision="blocked",
            reason=reason,
            all_targets_met=verdict.all_targets_met,
            unmet_criteria=unmet,
            changed_files=self._evidence_files(changed_files),
            blocked_stops=self._blocked_stops,
            next_action=verdict.next_action,
        )
        return decision

    def blocked_feedback(self, decision: DoneDecision, verdict: SupervisorVerdict) -> str:
        """Message sent to the agent when a DONE proposal is blocked.

        Follows the refusal-phrase discipline from the book: state exactly
        what evidence is missing and what to do next — no vague rejection.
        """
        parts = [
            "--- COMPLETION BLOCKED BY SUPERVISOR ---",
            "Your completion claim was not accepted. Reason: " + decision.reason + ".",
        ]
        if decision.unmet_criteria:
            parts.append(
                "These targets are still unresolved: "
                + "; ".join(decision.unmet_criteria[:5])
                + ".",
            )
        parts.append(
            "Do not simply restate completion — produce the missing evidence "
            "(file changes, passing command output) first, then claim completion again.",
        )
        if verdict.next_action:
            parts.append("Next action: " + verdict.next_action)
        parts.append("--- END BLOCK NOTICE ---")
        return "\n".join(parts)

    # ------------------------------------------------------------------ #
    # Turn-level tracking (continue path)                                 #
    # ------------------------------------------------------------------ #

    def evaluate_turn(
        self,
        verdict: SupervisorVerdict,
        *,
        worktree_sig: str,
        worktree_changed: bool,
        output: str,
    ) -> TurnDecision:
        """Record one judged continue-turn; return nudge guidance when stagnant.

        ``worktree_sig`` must be a fingerprint of workspace content (equal
        fingerprints mean an unchanged workspace); ``worktree_changed`` is the
        loop's own snapshot-diff verdict used for direction tracking.
        """
        self._turn += 1
        stagnation = self._stagnation.record(
            worktree_sig=worktree_sig,
            output_sig=output,
        )

        record_target_iteration(
            self._workspace,
            iteration=self._turn,
            direction=verdict.feedback or head_tail_excerpt(output, 200),
            evidence=f"worktree_changed={worktree_changed}",
            accepted=worktree_changed,
            reason=head_tail_excerpt(
                verdict.next_action or verdict.feedback or "", 240,
            ),
        )
        if verdict.next_action:
            record_lesson(
                self._workspace,
                head_tail_excerpt(verdict.next_action, _MAX_LESSON_LEN),
            )

        guidance = ""
        if stagnation is not None:
            guidance = self._stagnation_guidance(stagnation)
            self._audit(
                "stagnation",
                decision=f"stage-{stagnation.stage}",
                reason=stagnation.reason,
                all_targets_met=False,
                changed_files=[],
            )

        state = load_target_state(self._workspace)
        if state is not None and state.status == "replan_required" and not guidance:
            guidance = (
                "DIRECTION DIVERSITY REQUIRED: the current approach repeated "
                "without progress. Re-diagnose the problem and pick a different "
                "implementation direction before editing."
            )
        return TurnDecision(guidance=guidance, stagnation=stagnation)

    @staticmethod
    def _stagnation_guidance(signal: StagnationSignal) -> str:
        if signal.stage == 1:
            return (
                f"NOTE: you have {signal.reason}. If this turn was deliberate "
                "analysis or a test run, say so explicitly in your output; "
                "otherwise make an actual file change next."
            )
        if signal.stage == 2:
            return (
                "STRATEGY CHANGE REQUIRED: " + signal.reason + ". List three "
                "alternative approaches, then continue with the one you have "
                "not tried yet."
            )
        return "No further progress is possible with this approach; " + signal.reason + "."

    # ------------------------------------------------------------------ #
    # Prompt context                                                      #
    # ------------------------------------------------------------------ #

    def judge_context(self) -> str:
        """Bounded goal context for judge prompts (criteria + lessons)."""
        state = load_target_state(self._workspace)
        if state is None:
            return ""
        lines: list[str] = ["--- Goal Checklist ---"]
        criteria = state.success_criteria or self.criteria_from_protocol()
        for index, criterion in enumerate(criteria, start=1):
            lines.append(f"{index}. {criterion}")
        if state.lessons:
            lines.append("Guidance already given (do not repeat):")
            lines.extend(f"- {lesson}" for lesson in state.lessons[-3:])
        if state.rejected_directions:
            lines.append(
                "Directions already tried and rejected: "
                + " | ".join(state.rejected_directions[-2:]),
            )
        return "\n".join(lines) + "\n"

    def restart_context(self) -> str:
        """Target-state block for restart prompts (agent-facing)."""
        from supervisor.core.target_state import target_state_context

        return target_state_context(self._workspace)

    # ------------------------------------------------------------------ #
    # Evidence + audit                                                    #
    # ------------------------------------------------------------------ #

    def _evidence_files(self, changed_files: list[str]) -> list[str]:
        """Changed files minus supervisor-generated markdown."""
        from supervisor.core.llm_support.models import _OPENCODE_GENERATED_MD

        generated = set(_OPENCODE_GENERATED_MD) | {"TASK_STATE.md"}
        return [f for f in changed_files if Path(f).name not in generated]

    def _run_test_gate(self) -> tuple[bool, str]:
        from supervisor.runners.test_runner import OcTestRunner

        try:
            result = OcTestRunner(self._workspace).run()
        except Exception as exc:  # noqa: BLE001 — a broken gate must not crash the loop
            logger.warning("goal test gate crashed: %s", exc)
            return True, f"gate error ignored: {exc}"
        return bool(result.ok), result.summary()

    def _audit(self, event: str, **fields: object) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "turn": self._turn,
            "event": event,
        }
        record.update(fields)
        path = self._workspace / ".opencode" / _AUDIT_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, sort_keys=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            logger.warning("Could not append to goal audit log", exc_info=True)
