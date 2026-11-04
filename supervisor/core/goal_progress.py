"""Monotone goal-progress evidence for the BLOCKED exit.

The BLOCKED exit used to key on "no workspace file change". An agent that
rewrites ``summary.md`` or trims a docstring every turn produces a diff
forever, so the streak never grew: two studies re-judged the same unmet
criteria for 549 and 271 judged turns while their deliverables sat frozen.

File churn is not progress. What this tracker records instead is *attainment*:

* the obligations the judge has ever marked MET, matched by token overlap so
  the judge rewording "T4 tier" into "Target 4 (tier ...)" is the same
  obligation rather than a new one;
* the highest passing-gate count ever observed.

Both marks are monotone. Cosmetic edits, re-worded verdicts, and criteria
flipping MET -> UNMET -> MET cannot reset the streak; only satisfying
something never satisfied before, or winning a gate that had not passed
before, can.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_MARKUP_RE = re.compile(r"[`*_\[\](){}<>|~]+")
_LEAD_ID_RE = re.compile(
    r"^(?:target\s*)?(?:t\s*)?(?:#\s*)?\d+\b[\s.:)\-]*", re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-z0-9\u4e00-\u9fff]+")

# Match threshold for "the same obligation, worded differently". Measured on
# the two churning studies: 0.5 keeps one identity across every rewording of
# the same TARGET item, while genuinely different TARGET items stay apart.
MATCH_THRESHOLD = 0.5

_STOP_WORDS = frozenset(
    """
    a an and any are as at be been being both but by can could did do does
    each either every for from had has have if in into is it its may might
    more most must no nor not of off on once only or other our out over
    same shall should so some such than that the their them then there these
    they this those through to too under until up upon us use used was we
    were what when where which while who will with within without would yes
    yet you your
    evidence clause clauses fail failed fails false pass passes passed true
    unknown missing
    """.split(),
)


def criterion_tokens(text: str) -> frozenset[str]:
    """Content tokens of one criterion, with markup and numbering removed."""
    cleaned = _MARKUP_RE.sub(" ", str(text or "").lower()).strip()
    cleaned = _LEAD_ID_RE.sub("", cleaned)
    return frozenset(
        token
        for token in _TOKEN_RE.findall(cleaned)
        if len(token) > 1 and token not in _STOP_WORDS
    )


def criterion_number(text: str) -> str:
    """Leading TARGET id (``T4`` / ``Target 4`` / ``#4``), '' when unnumbered."""
    cleaned = _MARKUP_RE.sub(" ", str(text or "")).strip()
    match = _LEAD_ID_RE.match(cleaned.lower())
    if not match:
        return ""
    digits = re.search(r"\d+", match.group(0))
    return digits.group(0) if digits else ""


def _overlap(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


@dataclass
class _Obligation:
    """One judged acceptance criterion and whether it was ever satisfied."""

    number: str
    tokens: frozenset[str]
    met: bool


@dataclass(frozen=True)
class ProgressSignal:
    """One judged turn's verdict on whether anything new was attained."""

    progressed: bool
    streak: int
    detail: str


@dataclass
class GoalProgressTracker:
    """Per-run high-water marks over judged attainment.

    ``record`` is called once per judged (non-DONE) turn. ``streak`` counts
    consecutive turns that attained nothing new.
    """

    match_threshold: float = MATCH_THRESHOLD
    _obligations: list[_Obligation] = field(default_factory=list)
    _best_gates: int | None = None
    _streak: int = 0
    _last_detail: str = ""

    @property
    def streak(self) -> int:
        return self._streak

    @property
    def satisfied(self) -> int:
        """Obligations ever reported MET (monotone counter)."""
        return sum(1 for known in self._obligations if known.met)

    def reset(self) -> None:
        """Clear every mark after a DONE verdict or an external restart."""
        self._obligations.clear()
        self._best_gates = None
        self._streak = 0
        self._last_detail = ""

    def record(self, criteria, passing_gates: int | None = None) -> ProgressSignal:
        """Record one judged turn; return whether anything new was attained.

        ``criteria`` items expose ``.criterion`` and ``.met`` (the judge's
        ``CriterionResult``); plain strings are accepted as unmet criteria for
        callers that only carry the unmet list.

        A turn with no structured criteria at all (a compaction or
        deletion-permission reply, not a verdict) is unmeasurable: it neither
        advances nor counts against the streak.
        """
        pairs = list(_iter_criteria(criteria))
        if not pairs:
            return ProgressSignal(
                progressed=False,
                streak=self._streak,
                detail="no structured criteria in this turn",
            )
        was_known = self._known_count()
        progressed = self._absorb_obligations(pairs)
        if was_known == 0:
            # Cold start: the first verdict can only register identities, so
            # "nothing was attained" is an artifact of having no baseline yet.
            return ProgressSignal(
                progressed=True,
                streak=0,
                detail="baseline criteria registered",
            )
        gate_note = ""
        if passing_gates is not None:
            if self._best_gates is None or passing_gates > self._best_gates:
                if self._best_gates is not None:
                    progressed = True
                    gate_note = (
                        f"passing gates rose {self._best_gates} -> {passing_gates}"
                    )
                self._best_gates = passing_gates
        self._streak = 0 if progressed else self._streak + 1
        if progressed:
            self._last_detail = gate_note or "a previously unmet obligation was satisfied"
        return ProgressSignal(
            progressed=progressed,
            streak=self._streak,
            detail=self._last_detail,
        )

    def _absorb_obligations(self, criteria) -> bool:
        """Fold this turn's criteria into the identity registry.

        Only an *existing* obligation flipping UNMET -> MET counts as
        attainment. A criterion the judge has never reported before is
        registered silently: otherwise every rewording would create a new
        identity already marked MET and reset the streak, which is exactly the
        churn this tracker exists to see through.
        """
        new_entries: list[_Obligation] = []
        progressed = False
        for criterion, met in criteria:
            tokens = criterion_tokens(criterion)
            if not tokens:
                continue
            number = criterion_number(criterion)
            index = self._match(number, tokens)
            if index is None:
                # Deferred registration: within one turn two reworded copies
                # of the same criterion must not both become identities.
                if any(_overlap(tokens, other.tokens) >= self.match_threshold
                       for other in new_entries):
                    continue
                new_entries.append(
                    _Obligation(number=number, tokens=tokens, met=met),
                )
                continue
            known = self._obligations[index]
            if met and not known.met:
                known.met = True
                progressed = True
        self._obligations.extend(new_entries)
        return progressed

    def _known_count(self) -> int:
        return len(self._obligations)

    def _match(self, number: str, tokens: frozenset[str]) -> int | None:
        """Index of the same obligation, or None when this one is new.

        A leading TARGET id (``T4``, ``Target 4``, ``#4``) is authoritative
        when both sides carry one — the judge renumbers prose freely but keeps
        pointing at the same TARGET item. Unnumbered criteria fall back to
        token overlap.
        """
        if number:
            for index, known in enumerate(self._obligations):
                if known.number == number:
                    return index
        best_score = 0.0
        best_index: int | None = None
        for index, known in enumerate(self._obligations):
            if number and known.number and known.number != number:
                continue
            score = _overlap(tokens, known.tokens)
            if score >= self.match_threshold and score > best_score:
                best_score = score
                best_index = index
        return best_index


def _iter_criteria(criteria):
    """Yield ``(text, met)`` pairs from verdict criteria or plain strings."""
    for item in criteria or ():
        if isinstance(item, str):
            text = item.strip()
            if text:
                yield text, False
            continue
        text = str(getattr(item, "criterion", "") or "").strip()
        if text:
            yield text, bool(getattr(item, "met", False))


__all__ = [
    "MATCH_THRESHOLD",
    "GoalProgressTracker",
    "ProgressSignal",
    "criterion_number",
    "criterion_tokens",
]
