from __future__ import annotations

import re
from dataclasses import dataclass, field

from supervisor.utils.filesystem.path_filters import (
    DEFAULT_IGNORE_DIRS,
    DEFAULT_IGNORE_PREFIXES,
)


def _get_model_token_limit(model: str) -> int:
    """Get maximum input token limit for given model."""
    return 128_000


_TOKEN_LIMIT_ERROR_MARKERS = (
    "range of input length",
    "input length",
    "input token count",
    "maximum number of tokens",
    "maximum context length",
    "context length exceeded",
    "context_length_exceeded",
    "max tokens",
    "exceeds the maximum",
    "too many tokens",
    "token count exceeds",
    "prompt is too long",
    "request too large",
    "reduce the length",
)


def _is_token_limit_error(exc: Exception) -> bool:
    """Return True when error indicates prompt exceeded token limit."""
    message = str(exc).lower()
    if any(marker in message for marker in _TOKEN_LIMIT_ERROR_MARKERS):
        return True
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        code = str(err.get("code", "")) if isinstance(err, dict) else ""
        status = str(err.get("status", "")) if isinstance(err, dict) else ""
        msg = str(err.get("message", "")).lower() if isinstance(err, dict) else ""
        if status == "INVALID_ARGUMENT" and any(
            marker in msg for marker in _TOKEN_LIMIT_ERROR_MARKERS
        ):
            return True
        if code in {"context_length_exceeded", "string_above_max_length"}:
            return True
    return False


def _check_completion_phrases(reply: str, phrases: list[str]) -> bool:
    """Check if completion phrase appears positively in reply."""
    reply_lower = reply.lower()
    negation_prefixes = [
        r"not\s+",
        r"never\s+",
        r"failed\s+to\s+",
        r"unable\s+to\s+",
        r"did\s+not\s+",
        r"does\s+not\s+",
        r"don't\s+",
        r"doesn't\s+",
        r"won't\s+",
        r"cannot\s+",
        r"can't\s+",
    ]

    for phrase in phrases:
        pattern = r"\b" + re.escape(phrase) + r"\b"
        for match in re.finditer(pattern, reply_lower):
            start = match.start()
            prefix_context = reply_lower[max(0, start - 50):start]
            if not any(re.search(neg, prefix_context) for neg in negation_prefixes):
                return True
    return False


_OPENCODE_GENERATED_MD: set[str] = {
    "summary.md",
    "failure_report.md",
    "evolution_report.md",
}

_SKIP_DIRS = DEFAULT_IGNORE_DIRS | {".opencode", "opencode_supervisor.egg-info"}
_SKIP_DIR_PREFIXES = DEFAULT_IGNORE_PREFIXES

_DONE_PHRASES = [
    "all targets met",
    "all targets are met",
    "targets achieved",
    "task complete",
    "task is complete",
    "objectives met",
    "protocol satisfied",
]

# Structured verdict markers the judge is asked to emit. Parsing is best-effort:
# weak/free models often omit them, so callers must fall back to done-phrase
# matching when the structured flag is absent.
_CRITERIA_LINE_RE = re.compile(
    r"^\s*[-*]?\s*\[(?P<state>MET|UNMET)\]\s*(?P<rest>.+)$", re.IGNORECASE,
)
_DONE_LINE_RE = re.compile(r"^\s*DONE\s*:\s*(?P<flag>yes|no|true|false)\s*$", re.IGNORECASE)
_NEXT_ACTION_RE = re.compile(r"^\s*NEXT_ACTION\s*:\s*(?P<action>.+)$", re.IGNORECASE)
_EVIDENCE_REQUEST_RE = re.compile(
    r"^\s*NEED_EVIDENCE\s*:\s*(?P<request>.+)$", re.IGNORECASE,
)
_LOAD_SKILL_RE = re.compile(
    r"^\s*LOAD_SKILL\s*:\s*(?P<name>.+)$", re.IGNORECASE,
)

# Evidence text that contradicts a [MET] claim: weak judges occasionally mark
# a criterion MET while their own evidence says the opposite ("[MET] create
# greet.py — no greet.py found"). Each pattern here was observed in the wild.
# Deliberately contradiction-*shaped* phrases only: a bare "<negation> <word>"
# alternative also matches compliance phrasings ("no duplicated scripts",
# "no pre-made .mat consumed", "0 rows missing id") and vetoed legitimate
# DONE verdicts (run_8bff451f), so negation words must bind to an
# existence/verification verb.
_ISSUE_WORD = (
    r"(?!issues?\b|problems?\b|errors?\b|violations?\b|warnings?\b|"
    r"findings?\b|crashes?\b|regressions?\b|conflicts?\b|duplicates?\b|"
    r"changes?\b|modifications?\b|updates?\b)"
)
_NEGATIVE_EVIDENCE_RE = re.compile(
    r"\bnot\s+(?:found|created|modified|implemented|fixed|written|updated|installed|added|present|available)\b"
    r"|\bdoes\s+not\s+(?:exist|appear|run|pass|work)\b"
    r"|\bdoesn'?t\s+exist\b"
    r"|\bno\s+" + _ISSUE_WORD + r"[\w.\-]+\s+found\b"
    r"|\bno\s+(?:changes?|evidence|such|sign|trace|output|results?)\b"
    r"|\bstill\s+(?:missing|absent|empty|failing|failed|broken)\b"
    r"|\bunable\s+to\b"
    r"|\b(?:cannot|can't|could\s+not|couldn't)\s+(?:find|locate|verify|confirm|access)\b"
    r"|\b(?:missing|absent)\s+from\b"
    r"|\bis\s+(?:still\s+)?empty\b",
    re.IGNORECASE,
)


def flag_contradicted_criteria(
    criteria: list[CriterionResult],
) -> tuple[list[CriterionResult], list[str]]:
    """Downgrade [MET] criteria whose evidence itself says the opposite.

    Deterministic pre-check on the judge's structured output (defense in
    depth: validate the validator). Returns the possibly-downgraded list plus
    human-readable notes for the discrepancies found.
    """
    notes: list[str] = []
    adjusted: list[CriterionResult] = []
    for result in criteria:
        if result.met and result.evidence and _NEGATIVE_EVIDENCE_RE.search(
            result.evidence,
        ):
            adjusted.append(
                CriterionResult(
                    criterion=result.criterion,
                    met=False,
                    evidence=result.evidence,
                ),
            )
            notes.append(
                f"'{result.criterion}' marked MET but evidence reads negative: "
                f"'{result.evidence}' — treated as UNMET.",
            )
        else:
            adjusted.append(result)
    return adjusted, notes


@dataclass
class CriterionResult:
    """Per-criterion verdict from the judge, with the evidence it relied on."""

    criterion: str
    met: bool
    evidence: str = ""


def parse_verdict_structure(
    reply: str,
) -> tuple[bool | None, list[CriterionResult], str, list[str]]:
    """Extract the structured verdict block from a judge reply.

    Returns ``(done, criteria, next_action, evidence_requests)`` where ``done``
    is ``None`` when the reply carries no structured DONE marker (caller falls
    back to done-phrase matching), otherwise True/False. ``evidence_requests``
    are the judge's NEED_EVIDENCE resource asks (names, first token each).
    """
    done: bool | None = None
    criteria: list[CriterionResult] = []
    next_action = ""
    evidence_requests: list[str] = []

    for line in (reply or "").splitlines():
        done_match = _DONE_LINE_RE.match(line)
        if done_match:
            done = done_match.group("flag").lower() in ("yes", "true")
            continue
        criteria_match = _CRITERIA_LINE_RE.match(line)
        if criteria_match:
            rest = criteria_match.group("rest").strip()
            met = criteria_match.group("state").upper() == "MET"
            criterion, _, evidence = rest.partition("—")
            if not evidence.strip():
                criterion, _, evidence = rest.partition("-")
            criteria.append(
                CriterionResult(
                    criterion=criterion.strip().rstrip("-").strip(),
                    met=met,
                    evidence=evidence.strip(),
                ),
            )
            continue
        request_match = _EVIDENCE_REQUEST_RE.match(line)
        if request_match:
            name = request_match.group("request").strip().split()
            if name:
                evidence_requests.append(name[0])
            continue
        action_match = _NEXT_ACTION_RE.match(line)
        if action_match and not next_action:
            next_action = action_match.group("action").strip()

    return done, criteria, next_action, evidence_requests


def parse_skill_requests(reply: str) -> list[str]:
    """Extract LOAD_SKILL names from a judge reply (first token each).

    Separate from ``parse_verdict_structure`` so that function's 4-tuple
    signature — reused by history digests and verdict parsing — stays
    untouched. Names are lowercased and de-duplicated; the skill bank
    validates them against its catalog.
    """
    requests: list[str] = []
    for line in (reply or "").splitlines():
        match = _LOAD_SKILL_RE.match(line)
        if match:
            tokens = match.group("name").strip().split()
            if tokens and tokens[0].lower() not in requests:
                requests.append(tokens[0].lower())
    return requests


@dataclass
class SupervisorVerdict:
    raw: str
    all_targets_met: bool
    feedback: str
    criteria_results: list[CriterionResult] = field(default_factory=list)
    next_action: str = ""
    evidence_requests: list[str] = field(default_factory=list)
    skill_requests: list[str] = field(default_factory=list)
    validation_notes: list[str] = field(default_factory=list)

    @property
    def unmet_criteria(self) -> list[CriterionResult]:
        return [c for c in self.criteria_results if not c.met]


@dataclass
class StepContext:
    current_step: int = 0
    total_steps_estimate: int = 5
    phase: str = "unknown"
    completed_phases: list[str] = field(default_factory=list)
