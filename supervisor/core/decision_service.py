"""Structured decision service (jev) for bounded supervisor choices.

The supervisor's text heuristics (regex over NEXT_ACTION, keyword matching over
criteria) are brittle: a judge can order a terminal close in wording no pattern
anticipated, and the old path kept the run alive. This module delegates
bounded, evidence-based choices to a structured-decision endpoint that returns
calibrated probabilities over a caller-defined answer space.

Implemented now: the closing decision plus the four highest-ranked audited
candidates for the same client (see README "Bounded-decision audit"):

1. verdict MET-vs-negative-evidence contradiction (4×5);
2. stagnation action (4×4);
3. protocol alignment (3×5);
4. run closure / BLOCKED exit (5×2);
5. DONE-proposal ambiguity (4×2; deterministic gates remain hard vetoes);
6. the first blocked verdict's recovery-plan feasibility (4×2; the plan is
   produced by the judge from fresh web research, and a feasible ruling
   cancels the BLOCKED exit).

Remaining audited candidates, ranked by sureness × frequency:

7. direction novelty at the similarity threshold (4×2);
8. plan-mode approval (3×2);
9. Occam pass accept/rollback (4×1);
10. loop restart after repeated tools (3×1).

Never delegated: gate counts, test results, file existence, context thresholds,
worker code freshness, retry counts, protected paths, cleanup deletion safety.
Those are measurements or safety rules, not judgments.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

CLOSE_ACTIONS = (
    "close_run",
    "finish_closeout_then_close",
    "continue_design_work",
    "escalate_to_operator",
)

_SYSTEM_PROMPT = (
    "You are the final arbiter for a bounded supervisor decision: should this "
    "automated research run be closed? Decide from the evidence provided and "
    "return calibrated probabilities. Do not assume facts that are not stated."
)

_UNBLOCK_SYSTEM_PROMPT = (
    "You are the final arbiter for a bounded supervisor decision: should this "
    "recovery plan cancel a BLOCKED exit and keep the run alive? Approve only "
    "when the plan audits the executor's record, identifies the real blocker, "
    "and proposes a genuinely different, executable path. Return calibrated "
    "probabilities. Do not assume facts that are not stated."
)

_UNBLOCK_QUESTIONS = {
    "verdict": {
        "type": "choice",
        "instructions": (
            "Should this recovery plan cancel the BLOCKED exit and be sent "
            "to the executor?"
        ),
        "criteria": {
            "feasible": (
                "The plan explicitly addresses the executor record's factual "
                "errors, arbitrary decisions, or premature give-up (or "
                "explains why none apply), uses a genuinely different "
                "approach, and has concrete steps that can make the unmet "
                "target attainable."
            ),
            "not_feasible": (
                "The plan repeats the executor's claims, ignores audit "
                "findings, repeats failed approaches, lacks concrete "
                "executable steps, or cannot make the unmet target attainable."
            ),
        },
    },
}

_CLOSE_QUESTIONS = {
    "close_now": {
        "type": "noul",
        "instructions": "Should the harness close this run now?",
    },
    "terminal": {
        "type": "noul",
        "instructions": (
            "Has the study reached a terminal outcome where no further agent "
            "turn can change acceptance?"
        ),
    },
    "action": {
        "type": "choice",
        "instructions": "What should the harness do next?",
        "criteria": {
            "close_run": "Close the run now; the target is terminal or unreachable.",
            "finish_closeout_then_close": (
                "Allow the remaining evidence reconciliation, then close."
            ),
            "continue_design_work": (
                "Keep the run alive for more indicator design attempts."
            ),
            "escalate_to_operator": (
                "Pause and wait for a human or protocol decision."
            ),
        },
    },
}


@dataclass(frozen=True)
class CloseDecision:
    """Calibrated closing decision: close, grace, continue, or escalate."""

    verdict: str
    close_probability: float
    terminal_probability: float
    action: str
    actions: dict[str, float] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return (
            f"verdict={self.verdict} close={self.close_probability:.3f} "
            f"terminal={self.terminal_probability:.3f} action={self.action} "
            f"(p={self.actions.get(self.action, 0.0):.3f})"
        )


@dataclass(frozen=True)
class ChoiceDecision:
    """Arbitrated choice from a bounded action space."""

    action: str
    probability: float
    probabilities: dict[str, float] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return f"action={self.action} (p={self.probability:.3f})"


# A choice is acted on only when the winning label clears this probability.
# Binary (noul) decisions use the configured threshold instead.
CHOICE_MIN_CONFIDENCE = 0.5


def _clip(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _bounded(text: Any, limit: int) -> str:
    """Truncate while preserving line breaks (plans are structured text)."""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n... (truncated)"


class JevDecisionClient:
    """Minimal stdlib client for the structured decision API.

    ``transport`` is injectable for tests; by default it POSTs the payload as
    JSON. Any transport/parse failure returns ``None`` so the caller falls back
    to the deterministic text heuristics - a decision service being down must
    never block closure or keep a run alive.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 20.0,
        threshold: float = 0.85,
        api_key: str = "",
        transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.threshold = threshold
        self.api_key = api_key
        self._transport = transport or self._post

    def ask(
        self,
        state: str,
        questions: dict[str, Any],
        *,
        system_prompt: str = _SYSTEM_PROMPT,
    ) -> dict[str, Any] | None:
        """POST one structured-decision request; None on any failure."""
        payload = {
            "state": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": state},
            ],
            "questions": questions,
        }
        try:
            return self._transport(payload)
        except Exception as exc:  # any outage must fall back to heuristics  # noqa: BLE001 - optional external judge - failure falls back to the local path
            logger.warning("jev decision service unavailable (%s): %s", self.url, exc)
            return None

    def decide_close(self, evidence: str) -> CloseDecision | None:
        data = self.ask(evidence, _CLOSE_QUESTIONS)
        if data is None:
            return None
        try:
            answers = data["answers"]
            close = float(answers["close_now"]["noul"])
            terminal = float(answers["terminal"]["noul"])
            action_block = answers.get("action") or {}
            actions = {
                str(name): float(prob)
                for name, prob in (action_block.get("probabilities") or {}).items()
            }
            action = max(actions, key=actions.get) if actions else "continue_design_work"
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("jev close response malformed: %s", exc)
            return None
        return CloseDecision(
            verdict=self._verdict(close, action),
            close_probability=close,
            terminal_probability=terminal,
            action=action,
            actions=actions,
        )

    def _choice(self, data: dict[str, Any], name: str) -> ChoiceDecision | None:
        try:
            block = data["answers"][name]
            probabilities = {
                str(label): float(prob)
                for label, prob in (block.get("probabilities") or {}).items()
            }
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("jev choice response malformed: %s", exc)
            return None
        if not probabilities:
            return None
        action = max(probabilities, key=probabilities.get)
        return ChoiceDecision(
            action=action,
            probability=probabilities[action],
            probabilities=probabilities,
        )

    def decide_contradictions(
        self,
        pairs: list[tuple[str, str]],
        *,
        context: str,
    ) -> list[bool | None] | None:
        """Per-criterion: does the evidence contradict the judge's MET claim?"""
        pairs = pairs[:10]
        if not pairs:
            return []
        questions = {
            f"c{index}": {
                "type": "noul",
                "instructions": (
                    "Does this evidence contradict the judge's MET claim? "
                    f"Criterion: {_clip(criterion, 240)}. "
                    f"Evidence: {_clip(evidence, 400)}"
                ),
            }
            for index, (criterion, evidence) in enumerate(pairs)
        }
        data = self.ask(context, questions)
        if data is None:
            return None
        results: list[bool | None] = []
        for index in range(len(pairs)):
            try:
                probability = float(data["answers"][f"c{index}"]["noul"])
            except (KeyError, TypeError, ValueError):
                results.append(None)
                continue
            results.append(probability >= self.threshold)
        return results

    def decide_stagnation(
        self,
        *,
        stage: int,
        stagnant_turns: int,
        reason: str,
        output: str,
        state: str,
    ) -> ChoiceDecision | None:
        context = (
            "STAGNATION EVIDENCE\n"
            f"- Detector stage: {stage}; stagnant turns: {stagnant_turns}; "
            f"reason: {_clip(reason, 400)}\n"
            f"- Durable state: {_clip(state, 300)}\n"
            f"- Agent last output: {_clip(output, 900)}"
        )
        questions = {
            "action": {
                "type": "choice",
                "instructions": (
                    "What should the supervisor do about this stagnant turn?"
                ),
                "criteria": {
                    "continue": (
                        "The turn was legitimate analysis or verification; "
                        "no intervention."
                    ),
                    "nudge": "Remind the agent to make a file change next turn.",
                    "strategy_change": (
                        "Require a different approach before continuing."
                    ),
                    "restart": (
                        "Restart the agent session; the current approach is stuck."
                    ),
                },
            },
        }
        data = self.ask(context, questions)
        if data is None:
            return None
        return self._choice(data, "action")

    def decide_protocol_violation(
        self,
        *,
        violations: list[str],
        output: str,
        restrictions: str,
    ) -> ChoiceDecision | None:
        context = (
            "PROTOCOL VIOLATION REVIEW\n"
            f"- Restrictions: {_clip(restrictions, 900)}\n"
            "- Flagged by the deterministic scanner: "
            + _clip("; ".join(violations), 500)
            + "\n"
            f"- Agent output: {_clip(output, 900)}"
        )
        questions = {
            "verdict": {
                "type": "choice",
                "instructions": (
                    "Does the agent's output genuinely violate the protocol "
                    "restrictions?"
                ),
                "criteria": {
                    "violation": "A restriction is actually breached.",
                    "no_violation": (
                        "The flagged text is benign or a false positive."
                    ),
                    "uncertain": (
                        "The evidence is not sufficient to decide."
                    ),
                },
            },
        }
        data = self.ask(context, questions)
        if data is None:
            return None
        return self._choice(data, "verdict")

    def decide_done(
        self,
        *,
        mandate: str,
        criteria: list[tuple[str, bool, str]],
        changed_files: list[str],
        output: str,
    ) -> ChoiceDecision | None:
        lines = [
            f"  [{'MET' if met else 'UNMET'}] {_clip(name, 180)} — "
            f"{_clip(evidence, 300)}"
            for name, met, evidence in criteria[:10]
        ]
        context = (
            "DONE PROPOSAL REVIEW\n"
            f"- Mandate: {_clip(mandate, 700)}\n"
            "- Criteria:\n"
            + ("\n".join(lines) if lines else "  (none)")
            + "\n"
            f"- Changed files: {_clip(', '.join(changed_files[:20]), 500)}\n"
            f"- Agent last output: {_clip(output, 900)}"
        )
        questions = {
            "verdict": {
                "type": "choice",
                "instructions": (
                    "Should this DONE completion proposal be accepted?"
                ),
                "criteria": {
                    "accept": "The evidence supports every target.",
                    "reject": (
                        "The completion claim is not supported by the evidence."
                    ),
                    "need_evidence": (
                        "More evidence is required before accepting."
                    ),
                },
            },
        }
        data = self.ask(context, questions)
        if data is None:
            return None
        return self._choice(data, "verdict")

    def decide_unblock_plan(
        self,
        *,
        plan: str,
        evidence: str,
    ) -> ChoiceDecision | None:
        """Is the judge's web-researched recovery plan feasible?

        A ``feasible`` ruling cancels the BLOCKED exit; anything else (or an
        outage, handled by the caller) closes the run.
        """
        context = (
            "BLOCKED RUN UNBLOCK PLAN REVIEW\n"
            f"{_bounded(evidence, 4500)}\n"
            "- Proposed plan:\n"
            f"{_bounded(plan, 4500)}"
        )
        data = self.ask(
            context, _UNBLOCK_QUESTIONS, system_prompt=_UNBLOCK_SYSTEM_PROMPT,
        )
        if data is None:
            return None
        return self._choice(data, "verdict")

    def _verdict(self, close: float, action: str) -> str:
        if action == "continue_design_work":
            return "continue"
        if action == "escalate_to_operator":
            return "escalate"
        if close < self.threshold:
            return "continue"
        return "close" if action == "close_run" else "grace"

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


def build_close_evidence(
    *,
    mandate: str,
    status: str,
    iteration: int,
    stagnation: int,
    passing_gates: int | None,
    verdict: Any,
    output: str,
) -> str:
    """Compact evidence block for the closing decision.

    The structured-decision model sees only this text. It carries the durable
    state, the judge's structured verdict and the agent's posture; it
    deliberately excludes raw files and long prose.
    """

    def clip(text: Any, limit: int) -> str:
        return _clip(text, limit)

    criteria = []
    for item in getattr(verdict, "criteria_results", None) or []:
        state = "MET" if getattr(item, "met", False) else "UNMET"
        criteria.append(f"[{state}] {clip(getattr(item, 'criterion', ''), 160)}")
    gates = str(passing_gates) if passing_gates is not None else "unknown"
    lines = [
        "RUN EVIDENCE",
        f"- Mandate: {clip(mandate, 700)}",
        (
            f"- Durable state: status={status}; judged turns={iteration}; "
            f"stagnant streak={stagnation}; passing gates={gates}."
        ),
        "- Judge criteria: " + (" | ".join(criteria[:10]) if criteria else "(none)"),
        f"- Judge NEXT_ACTION: {clip(getattr(verdict, 'next_action', ''), 600)}",
        f"- Judge feedback: {clip(getattr(verdict, 'feedback', ''), 600)}",
        f"- Agent last output: {clip(output, 900)}",
    ]
    return "\n".join(lines)


__all__ = [
    "CHOICE_MIN_CONFIDENCE",
    "CLOSE_ACTIONS",
    "ChoiceDecision",
    "CloseDecision",
    "JevDecisionClient",
    "build_close_evidence",
]
