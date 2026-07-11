"""Deterministic preflight for protocol TARGET sections.

Targets must make four things observable: deliverable, acceptance evidence,
completion state, and what counts as failure. This deliberately rejects vague
"improve" language before a long-running agent can optimise the wrong thing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ACTION_RE = re.compile(
    r"\b(add|build|create|deliver|fix|implement|migrate|produce|refactor|remove|replace|test|update|write)\b",
    re.IGNORECASE,
)
_EVIDENCE_RE = re.compile(
    r"\b(assert|acceptance|benchmark|check|coverage|lint|metric|pass(?:es|ed)?|pytest|test(?:s|ed|ing)?|validate|verify)\b",
    re.IGNORECASE,
)
_FAILURE_RE = re.compile(
    r"\b(block(?:ed|er)?|error|fail(?:ed|ure|s)?|must not|never|no regression|regression|stop)\b",
    re.IGNORECASE,
)
_COMPLETION_RE = re.compile(
    r"\b(complete(?:d)?|deliver(?:ed|able)?|done|implemented|pass(?:es|ed)?|produce(?:d)?|ready|ship(?:ped)?|verified)\b",
    re.IGNORECASE,
)
_VAGUE_CHANGE_RE = re.compile(r"\b(enhance|improve|optimise|optimize)\b", re.IGNORECASE)


@dataclass(frozen=True)
class TargetAudit:
    """Whether a target can guide an agent and be independently checked."""

    has_deliverable: bool
    has_acceptance_evidence: bool
    has_completion_state: bool
    has_failure_condition: bool
    rejects_target: bool
    issues: tuple[str, ...]

    @property
    def is_actionable(self) -> bool:
        return not self.rejects_target and not self.issues


def audit_target(target: str, restrictions: str = "") -> TargetAudit:
    """Audit a TARGET without an LLM or side effects."""
    target = target.strip()
    combined = f"{target}\n{restrictions}".strip()
    evidence = bool(_EVIDENCE_RE.search(target))
    deliverable = bool(_ACTION_RE.search(target))
    completion = bool(_COMPLETION_RE.search(target)) or (deliverable and evidence)
    failure = bool(_FAILURE_RE.search(combined)) or evidence
    vague_change = bool(_VAGUE_CHANGE_RE.search(target))

    issues: list[str] = []
    if not deliverable:
        issues.append("TARGET needs a concrete deliverable: file, behavior, or artifact to produce.")
    if not evidence:
        issues.append("TARGET needs acceptance evidence: test, assertion, benchmark, or measurable check.")
    if not completion:
        issues.append("TARGET needs a completion state: what observable result means finished.")
    if not failure:
        issues.append("TARGET needs a failure condition: what result blocks acceptance or requires replanning.")
    if vague_change and (not evidence or not completion):
        issues.append('Vague change target rejected: replace "improve" with evidence and completion criteria.')

    return TargetAudit(
        has_deliverable=deliverable,
        has_acceptance_evidence=evidence,
        has_completion_state=completion,
        has_failure_condition=failure,
        rejects_target=vague_change and (not evidence or not completion),
        issues=tuple(issues),
    )
