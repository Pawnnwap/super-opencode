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

JSON schema — gate list (preferred)::

    {
      "gates": [
        {"name": "long_shrp_gt_3", "definition": "page.shrp > 3.0",
         "status": "open" | "passing" | "blocked",
         "evidence": "one line: where this status was observed"}
      ],
      "notes": "optional overall note"
    }

    Any gate with status "blocked" makes the done condition unreachable.

Legacy count-based schema (still read for older files)::

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

# Per-gate status vocabulary. "blocked" marks a gate that failed in every
# recorded attempt — the run's done condition cannot be met while it holds.
GATE_STATUSES = ("open", "passing", "blocked")


@dataclass(frozen=True)
class Gate:
    """One named acceptance criterion and its known status.

    ``ref`` ties the gate to the numbered TARGET item it derives from
    (drift guard); ``check`` is the typed machine check (see gate_rubric):
    {"kind": "command", "cmd": ..., "expect_exit": 0} or
    {"kind": "metric", "metric": ..., "op": ..., "value": ...,
     "source": "file.json:dotted.key"} — absent means the judge adjudicates.
    """

    name: str
    definition: str = ""
    status: str = "open"
    evidence: str = ""
    ref: int | None = None
    check: dict | None = None

    def to_dict(self) -> dict:
        data = {
            "name": self.name,
            "definition": self.definition,
            "status": self.status,
            "evidence": self.evidence,
        }
        if self.ref is not None:
            data["ref"] = self.ref
        if self.check:
            data["check"] = dict(self.check)
        return data


def parse_gates(facts: dict) -> tuple[Gate, ...]:
    """Validated gates from a facts dict; () when it has no gate list."""
    raw = facts.get("gates") if isinstance(facts, dict) else None
    if not isinstance(raw, list):
        return ()
    gates: list[Gate] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        status = str(item.get("status", "open")).strip().lower()
        if status not in GATE_STATUSES:
            status = "open"
        check = item.get("check")
        gates.append(
            Gate(
                name=name,
                definition=str(item.get("definition", "")).strip(),
                status=status,
                evidence=str(item.get("evidence", "")).strip(),
                ref=_as_int(item.get("ref")),
                check=dict(check) if isinstance(check, dict) else None,
            ),
        )
    return tuple(gates)


def gates_to_facts(rows, notes: str = "") -> dict | None:
    """Coerce editor rows into the persisted gates schema; None when empty."""
    gates: list[Gate] = []
    for row in rows or []:
        if isinstance(row, Gate):
            gate = row
        elif isinstance(row, dict):
            check = row.get("check")
            gate = Gate(
                name=str(row.get("name", "")),
                definition=str(row.get("definition", "")),
                status=str(row.get("status", "open")),
                evidence=str(row.get("evidence", "")),
                ref=_as_int(row.get("ref")),
                check=dict(check) if isinstance(check, dict) else None,
            )
        else:
            continue
        name = gate.name.strip()
        if not name:
            continue
        status = gate.status.strip().lower()
        if status not in GATE_STATUSES:
            status = "open"
        gates.append(
            Gate(
                name,
                gate.definition.strip(),
                status,
                gate.evidence.strip(),
                gate.ref,
                dict(gate.check) if gate.check else None,
            ),
        )
    if not gates:
        return None
    facts: dict = {"gates": [g.to_dict() for g in gates]}
    if str(notes).strip():
        facts["notes"] = str(notes).strip()
    return facts


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
    gates: tuple[Gate, ...] = ()

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
            "gates": [g.to_dict() for g in self.gates],
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
        for gate in self.gates:
            marker = {"blocked": "✗", "passing": "✓", "open": "?"}[gate.status]
            ref_note = f" #{gate.ref}" if gate.ref else ""
            check_note = ""
            if isinstance(gate.check, dict) and gate.check.get("kind"):
                check_note = f" {{{gate.check['kind']} checked}}"
            line = f"- gate {gate.name}{ref_note} [{marker} {gate.status}]{check_note}"
            if gate.definition:
                line += ": " + _compact(gate.definition)
            if gate.evidence:
                line += f" — {_compact(gate.evidence)}"
            lines.append(line)
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
    gates = parse_gates(facts)
    if gates:
        required = len(gates)
        blocked = tuple(g.name for g in gates if g.status == "blocked")
        passing = sum(1 for g in gates if g.status == "passing")
        ceiling = required - len(blocked)
        reachable = not blocked
        if blocked:
            reason = (
                f"{len(blocked)} of {required} gate(s) never passed "
                f"({', '.join(blocked)}); the done condition cannot be met "
                "under the current constraints."
            )
        elif passing:
            reason = (
                f"{passing} of {required} gate(s) passed in earlier attempts; "
                f"{required - passing} untested, none blocked."
            )
        else:
            reason = f"All {required} gate(s) untested; none known-blocked."
        return FeasibilityAssessment(
            required_gates=required,
            known_ceiling=ceiling,
            blocked_gates=blocked,
            evidence="; ".join(
                g.evidence for g in gates if g.status == "blocked" and g.evidence
            ),
            notes=str(facts.get("notes", "")),
            facts_source=facts_source,
            reachable=reachable,
            reason=reason,
            gates=gates,
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


def parse_llm_facts(text: str) -> dict:
    """Recover the facts JSON from an LLM reply.

    Tolerates markdown code fences and surrounding prose; raises ValueError
    when no JSON object can be recovered.
    """
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1 :] if first_newline != -1 else ""
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the model reply")
    try:
        data = json.loads(cleaned[start : end + 1])
    except ValueError as exc:
        raise ValueError(f"model reply is not valid JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


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


# Persist the facts file; no gates means the file is removed.
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


__all__ = [
    "DEFAULT_FACTS_NAME",
    "MAX_BLOCK_CHARS",
    "GATE_STATUSES",
    "Gate",
    "parse_gates",
    "gates_to_facts",
    "write_feasibility_facts",
    "FeasibilityAssessment",
    "check_feasibility",
    "default_facts_path",
    "load_feasibility_facts",
    "parse_llm_facts",
    "protocol_fingerprint",
    "render_feasibility_block",
]
