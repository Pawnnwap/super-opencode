"""supervisor/protocols/feasibility_gate.py - pre-execution feasibility probe.

Structural design item 5: transcribe the acceptance gates and any known
empirical ceiling into one tiny bounded markdown block shown to both the coding
agent and the judge, plus a deterministic verdict. When an operator records a
known gate ceiling (for example "15 attempts capped at 8/10, long_shrp_gt_3
never met"), the run surfaces that ceiling up front instead of rediscovering
it over another 15-20 pre-registered attempts.

This module is self-contained and deterministic: no LLM calls, no shared state.
It reads an optional JSON facts file (default: <workspace>/logs/feasibility_facts.json)
and renders a bounded block.

JSON schema (all keys optional)::

    {
      "required_gates": 10,
      "gate_thresholds": {"long_shrp_gt_3": "page.shrp > 3.0"},
      "known_ceiling": {
        "count": 8,
        "of": 10,
        "blocked_gates": ["long_shrp_gt_3", "best_worst_pair_1_20"],
        "evidence": "sibling runs capped at 8/10 after 15 attempts on this data",
        "notes": "single-idea ratio constructions saturate below the ceiling"
      },
      "referenced_files": ["harness/remote_handlers/ab_tools/criteria.py"]
    }
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_FACTS_NAME = "feasibility_facts.json"
MAX_BLOCK_CHARS = 2200


@dataclass(frozen=True)
class FeasibilityAssessment:
    """Deterministic verdict derived from recorded facts, never an LLM call."""

    required_gates: int | None
    known_ceiling: int | None
    blocked_gates: tuple[str, ...]
    evidence: str
    notes: str
    facts_source: str
    reachable: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "required_gates": self.required_gates,
            "known_ceiling": self.known_ceiling,
            "blocked_gates": list(self.blocked_gates),
            "evidence": self.evidence,
            "notes": self.notes,
            "facts_source": self.facts_source,
            "reachable": self.reachable,
            "reason": self.reason,
        }

    def render(self, max_chars: int = MAX_BLOCK_CHARS) -> str:
        lines = ["## FEASIBILITY GATE"]
        if self.required_gates is not None:
            lines.append(f"- required_gates: {self.required_gates}")
        if self.known_ceiling is not None:
            lines.append(f"- known_ceiling: {self.known_ceiling}")
        if self.blocked_gates:
            lines.append("- blocked_gates: " + ", ".join(self.blocked_gates))
        if self.evidence:
            lines.append("- evidence: " + _compact(self.evidence))
        if self.notes:
            lines.append("- notes: " + _compact(self.notes))
        lines.append("- reachable: " + ("yes" if self.reachable else "no"))
        if self.reason:
            lines.append("- reason: " + _compact(self.reason))
        if self.facts_source:
            lines.append("- facts_source: " + _compact(self.facts_source))
        return _bounded("\n".join(lines), max_chars)


def load_feasibility_facts(path: Path | None) -> dict | None:
    """Read the facts JSON; tolerate missing/unparseable files by returning None."""
    if path is None:
        return None
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        logger.warning(
            "Could not parse feasibility facts at %s", path, exc_info=True,
        )
        return None
    if not isinstance(data, dict):
        logger.warning("Feasibility facts at %s is not a JSON object", path)
        return None
    return data


def check_feasibility(
    facts: dict | None,
    *,
    facts_source: str = "",
) -> FeasibilityAssessment:
    """Compute the deterministic assessment.

    A known ceiling strictly below the required gate count means the done
    condition is unreachable under the current constraints; the block says so
    up front instead of being rediscovered over many pre-registered attempts.
    """
    if not facts:
        return FeasibilityAssessment(
            required_gates=None,
            known_ceiling=None,
            blocked_gates=(),
            evidence="",
            notes="",
            facts_source=facts_source,
            reachable=True,
            reason="No feasibility facts recorded.",
        )
    required = _as_int(facts.get("required_gates"))
    ceiling_raw = facts.get("known_ceiling")
    ceiling = None
    blocked: tuple[str, ...] = ()
    evidence = ""
    notes = ""
    if isinstance(ceiling_raw, dict):
        count = _as_int(ceiling_raw.get("count"))
        of = _as_int(ceiling_raw.get("of"))
        if count is not None and of is not None:
            ceiling = of if count >= of else count
        elif count is not None:
            ceiling = count
        raw_blocked = ceiling_raw.get("blocked_gates")
        if isinstance(raw_blocked, list):
            blocked = tuple(str(g) for g in raw_blocked if str(g).strip())
        evidence = str(ceiling_raw.get("evidence", ""))
        notes = str(ceiling_raw.get("notes", ""))
    if required is None or ceiling is None or ceiling >= required:
        reachable = True
    else:
        reachable = False
    if not reachable:
        reason = (
            f"Known ceiling {ceiling} < required {required} gate(s); the "
            "done condition cannot be met under the current constraints."
        )
    elif ceiling is not None and required is not None:
        reason = (
            f"Known ceiling {ceiling}/{required}; requiring all {required} "
            "gates is still nominal; treat the ceiling as prior evidence."
        )
    elif ceiling is not None:
        reason = f"Known ceiling recorded at {ceiling} gates."
    else:
        reason = "No known ceiling recorded against the required gate count."
    return FeasibilityAssessment(
        required_gates=required,
        known_ceiling=ceiling,
        blocked_gates=blocked,
        evidence=evidence,
        notes=notes,
        facts_source=facts_source,
        reachable=reachable,
        reason=reason,
    )


def render_feasibility_block(
    assessment: FeasibilityAssessment | None,
    *,
    max_chars: int = MAX_BLOCK_CHARS,
) -> str:
    """Render the bounded markdown block, or \"\" when there is nothing to show."""
    if assessment is None:
        return ""
    block = assessment.render(max_chars=max_chars)
    if not block.strip():
        return ""
    return block.rstrip() + "\n"


def default_facts_path(workspace: Path) -> Path:
    """Conventional location next to the run's other state files."""
    return Path(workspace) / "logs" / DEFAULT_FACTS_NAME


def protocol_fingerprint(text: str) -> str:
    """Short stable id for a protocol text (constant text is never re-sent)."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _as_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _compact(text: str) -> str:
    return " ".join(str(text).split())


def _bounded(text: str, cap: int) -> str:
    text = text.rstrip()
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + " ..."


__all__ = [
    "DEFAULT_FACTS_NAME",
    "MAX_BLOCK_CHARS",
    "feasibility_facts_from_kv",
    "write_feasibility_facts",
    "FeasibilityAssessment",
    "check_feasibility",
    "default_facts_path",
    "load_feasibility_facts",
    "protocol_fingerprint",
    "render_feasibility_block",
]



# Convert plain form values into the feasibility facts schema.
# "key: value" lines for thresholds; comma-separated lists for
# blocked gates and referenced files. None when nothing was given.
def feasibility_facts_from_kv(
    *,
    required_gates=None,
    ceiling_count=None,
    ceiling_of=None,
    blocked_gates: str = "",
    thresholds: str = "",
    evidence: str = "",
    notes: str = "",
    referenced_files: str = "",
) -> dict | None:
    ceiling: dict = {}
    count = _as_int(ceiling_count)
    of = _as_int(ceiling_of)
    if count is not None:
        ceiling["count"] = count
    if of is not None:
        ceiling["of"] = of
    blocked = _split_list(blocked_gates)
    if blocked:
        ceiling["blocked_gates"] = blocked
    for key, value in (("evidence", evidence), ("notes", notes)):
        value = str(value).strip()
        if value:
            ceiling[key] = value
    thresh = _kv_lines(thresholds)
    refs = _split_list(referenced_files)
    facts: dict = {}
    required = _as_int(required_gates)
    if required is not None:
        facts["required_gates"] = required
    if ceiling:
        facts["known_ceiling"] = ceiling
    if thresh:
        facts["gate_thresholds"] = thresh
    if refs:
        facts["referenced_files"] = refs
    return facts or None


# Persist the auto-generated facts file; an empty form clears it.
def write_feasibility_facts(
    facts: dict | None,
    path: Path,
    *,
    delete_when_empty: bool = True,
) -> Path | None:
    path = Path(path)
    if not facts:
        if delete_when_empty:
            path.unlink(missing_ok=True)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(facts, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _split_list(text: str) -> list[str]:
    return [part.strip() for part in str(text).split(",") if part.strip()]


def _kv_lines(text: str) -> dict:
    result: dict = {}
    for line in str(text).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip():
            result[key.strip()] = value.strip()
    return result
