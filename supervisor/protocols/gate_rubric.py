"""supervisor/protocols/gate_rubric.py — trust layer for the gate rubric.

Ontology-×-LLM discipline (Engineering Ontology ch. 8): the LLM understands
and expresses; the rubric owns facts and constraints. Machine-checkable
gates (command / metric) are evaluated deterministically here — the judge is
never asked about them (controlled channel) — and every DONE proposal passes
entity / assertion / consistency exit checks before it can end the run.
Results carry PROV tuples (conclusion, fact, source, timestamp) so decisions
replay after the fact; missing evidence is reported as ERROR ("no record"),
never guessed.
"""

from __future__ import annotations

import json
import logging
import operator
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from supervisor.protocols.feasibility_gate import Gate, parse_gates
from supervisor.protocols.feasibility_gate import (
    protocol_fingerprint as _protocol_fingerprint,
)

logger = logging.getLogger(__name__)

# Guarded transition: FAIL streak at which a blocked status is PROPOSED to
# the operator (never auto-applied — ending pursuit of a goal is the
# irreversible action of this system and needs a confirmation upgrade).
BLOCK_PROPOSAL_STREAK = 3

_OPS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "=": operator.eq,
    "!=": operator.ne,
}


@dataclass(frozen=True)
class GateEvaluation:
    """One deterministic gate result with its PROV tuple."""

    name: str
    outcome: str  # "PASS" | "FAIL" | "ERROR" (no record)
    fact: str
    source: str
    kind: str
    timestamp: float

    def to_prov(self) -> dict:
        return {
            "gate": self.name,
            "conclusion": self.outcome,
            "fact": self.fact,
            "source": self.source,
            "kind": self.kind,
            "timestamp": self.timestamp,
        }


# --------------------------------------------------------------------------- #
# Layer 2 — controlled channel: deterministic evaluation                      #
# --------------------------------------------------------------------------- #


def evaluate_gate(
    gate: Gate,
    workspace: Path,
    *,
    timeout: float = 120.0,
) -> GateEvaluation | None:
    """Evaluate a machine-checkable gate; None for manual (judge) gates."""
    check = gate.check if isinstance(gate.check, dict) else None
    if not check:
        return None
    kind = str(check.get("kind", "")).strip().lower()
    if kind == "command":
        return _evaluate_command(gate, check, workspace, timeout)
    if kind == "metric":
        return _evaluate_metric(gate, check, workspace)
    return GateEvaluation(
        name=gate.name,
        outcome="ERROR",
        fact=f"unknown check kind {kind!r} — fix or remove the check",
        source="rubric",
        kind=kind or "?",
        timestamp=time.time(),
    )


def evaluate_rubric(
    gates: tuple[Gate, ...],
    workspace: Path,
    *,
    timeout: float = 120.0,
) -> dict[str, GateEvaluation]:
    """Name → evaluation for every machine gate; manual gates are absent."""
    out: dict[str, GateEvaluation] = {}
    for gate in gates:
        evaluation = evaluate_gate(gate, workspace, timeout=timeout)
        if evaluation is not None:
            out[gate.name] = evaluation
    return out


def _evaluate_command(
    gate: Gate,
    check: dict,
    workspace: Path,
    timeout: float,
) -> GateEvaluation:
    cmd = str(check.get("cmd", "")).strip()
    if not cmd:
        return _error(gate, "command check has no cmd", "rubric")
    expect = check.get("expect_exit", 0)
    try:
        expect_code = int(expect)
    except (TypeError, ValueError):
        expect_code = 0
    argv = shlex.split(cmd, posix=sys.platform != "win32")
    try:
        proc = subprocess.run(
            argv,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _error(gate, f"command timed out after {timeout:.0f}s", cmd)
    except (OSError, ValueError) as exc:
        return _error(gate, f"command could not run: {exc}", cmd)
    passed = proc.returncode == expect_code
    tail = (proc.stdout or proc.stderr or "").strip()[-200:]
    return GateEvaluation(
        name=gate.name,
        outcome="PASS" if passed else "FAIL",
        fact=(
            f"exit={proc.returncode} (expected {expect_code})"
            + (f"; {tail}" if tail else "")
        ),
        source=cmd,
        kind="command",
        timestamp=time.time(),
    )


def _evaluate_metric(gate: Gate, check: dict, workspace: Path) -> GateEvaluation:
    name = str(check.get("metric", "")).strip()
    op_name = str(check.get("op", "")).strip()
    value = check.get("value")
    source_spec = str(check.get("source", "")).strip()
    if not (name and op_name and value is not None and source_spec):
        return _error(
            gate,
            "metric check needs metric/op/value/source",
            "rubric",
        )
    compare = _OPS.get(op_name)
    if compare is None:
        return _error(gate, f"unknown comparator {op_name!r}", "rubric")
    file_part, _, key_part = source_spec.partition(":")
    if not file_part or not key_part:
        return _error(gate, f"bad source spec {source_spec!r}", "rubric")
    path = Path(workspace) / file_part
    if not path.is_file():
        return _error(
            gate, f"no record: {file_part} not found", source_spec, honest=True,
        )
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return _error(
            gate, f"no record: {file_part} unreadable ({exc})", source_spec,
            honest=True,
        )
    node: object = doc
    for key in key_part.split("."):
        if not isinstance(node, dict) or key not in node:
            return _error(
                gate, f"no record: key {key_part!r} missing in {file_part}",
                source_spec, honest=True,
            )
        node = node[key]
    try:
        observed = float(node)
    except (TypeError, ValueError):
        return _error(
            gate, f"no record: {key_part!r} in {file_part} is not numeric",
            source_spec, honest=True,
        )
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return _error(gate, f"threshold {value!r} is not numeric", "rubric")
    passed = compare(observed, threshold)
    return GateEvaluation(
        name=gate.name,
        outcome="PASS" if passed else "FAIL",
        fact=f"{name}={observed} {'satisfies' if passed else 'fails'} "
        f"{name} {op_name} {threshold}",
        source=source_spec,
        kind="metric",
        timestamp=time.time(),
    )


def _error(
    gate: Gate, fact: str, source: str, *, honest: bool = False,
) -> GateEvaluation:
    if not honest:
        logger.warning("gate %s: %s", gate.name, fact)
    return GateEvaluation(
        name=gate.name,
        outcome="ERROR",
        fact=fact,
        source=source,
        kind="check",
        timestamp=time.time(),
    )


# --------------------------------------------------------------------------- #
# Layer 1 — closed-vocabulary scorecard injection                             #
# --------------------------------------------------------------------------- #


def render_scorecard(
    gates: tuple[Gate, ...],
    evaluations: dict[str, GateEvaluation],
    *,
    last_harvest: dict | None = None,
) -> str:
    """The gate block shown to judge/agent: closed list, determined facts first."""
    if not gates:
        return ""
    prior: dict = {}
    if isinstance(last_harvest, dict):
        for entry in last_harvest.get("results", []):
            if isinstance(entry, dict) and entry.get("gate"):
                prior[str(entry["gate"])] = entry
    lines = [
        "## GATE SCORECARD (closed list — judge exactly these gates; "
        "DONE requires every gate PASS)",
    ]
    determined = [
        (g, evaluations.get(g.name)) for g in gates if g.name in evaluations
    ]
    if determined:
        lines.append("Determined by the system — do NOT re-judge these:")
        for gate, evaluation in determined:
            lines.append(
                f"- {gate.name}: {evaluation.outcome} ({evaluation.fact}; "
                f"{evaluation.source})",
            )
    manual = [g for g in gates if g.name not in evaluations]
    if manual:
        lines.append(
            "Adjudicate these with evidence (report PASS or UNMET by exact "
            "name; cite the artifact you used; if you cannot evidence one, "
            'report UNMET with evidence "unknown — <what is missing>"):',
        )
        for gate in manual:
            last = prior.get(gate.name)
            last_note = (
                f" (last run: {last.get('conclusion')}; {last.get('fact')})"
                if last
                else ""
            )
            lines.append(f"- {gate.name} [{gate.status}]:{last_note}")
    lines.append(
        "Gates outside this list do not exist; numeric claims must come from "
        "the determined results above.",
    )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Layer 3 — exit validation                                                    #
# --------------------------------------------------------------------------- #


def verdict_violations(
    criteria_results: list,
    gates: tuple[Gate, ...],
    evaluations: dict[str, GateEvaluation],
) -> list[str]:
    """Entity / assertion checks on a judge verdict against the rubric.

    ``criteria_results`` items expose ``.criterion`` (text) and ``.met``.
    Consistency (DONE requires all PASS) is enforced by the goal guard; these
    checks catch fabrication and unevidenced claims.
    """
    if not gates:
        return []
    violations: list[str] = []
    rubric_names = {g.name for g in gates}
    claimed: dict[str, bool] = {}
    for result in criteria_results or []:
        text = str(getattr(result, "criterion", "") or "")
        met = bool(getattr(result, "met", False))
        for name in rubric_names:
            if name and name in text:
                claimed[name] = claimed.get(name, False) or met
    # Entity: gate names the judge used that the rubric does not define.
    unknown: list[str] = []
    for result in criteria_results or []:
        text = str(getattr(result, "criterion", "") or "").strip()
        if not text:
            continue
        if not any(name in text for name in rubric_names):
            unknown.append(text[:60])
    if unknown:
        violations.append(
            "fabricated gate(s) outside the rubric: " + "; ".join(unknown[:3]),
        )
    # Assertion: PASS claimed for a machine gate the system measured otherwise.
    for name, evaluation in evaluations.items():
        if name in claimed and claimed[name] and evaluation.outcome != "PASS":
            violations.append(
                f"gate {name} claimed PASS but measured {evaluation.outcome} "
                f"({evaluation.fact})",
            )
    # Coverage: machine gates not adjudicated at all.
    missing = [
        name for name in evaluations if name not in claimed
    ]
    if missing:
        violations.append(
            "machine-determined gate(s) not adjudicated: " + ", ".join(missing),
        )
    return violations


# --------------------------------------------------------------------------- #
# PROV harvest + guarded transitions                                           #
# --------------------------------------------------------------------------- #


def build_harvest(
    gates: tuple[Gate, ...],
    evaluations: dict[str, GateEvaluation],
    manual_results: dict[str, tuple[bool, str]],
    *,
    run_id: str,
    protocol_text: str,
) -> dict:
    """PROV record of one run's gate outcomes (conclusion/fact/source/time)."""
    results: list[dict] = [e.to_prov() for e in evaluations.values()]
    for name, (met, evidence) in manual_results.items():
        results.append(
            {
                "gate": name,
                "conclusion": "PASS" if met else "UNMET",
                "fact": evidence or ("adjudicated by judge" if met else "not evidenced"),
                "source": "judge verdict",
                "kind": "manual",
                "timestamp": time.time(),
            },
        )
    return {
        "run_id": run_id,
        "protocol_revision": _protocol_fingerprint(protocol_text),
        "generated_at": time.time(),
        "results": results,
    }


def apply_harvest(facts: dict, harvest: dict) -> dict:
    """Guarded transitions: PASS→passing (deterministic); FAIL streaks propose
    blocked into ``pending_blocked`` for operator confirmation — never applied.
    """
    gates = [g for g in facts.get("gates", []) if isinstance(g, dict)]
    if not gates:
        return facts
    pending = list(facts.get("pending_blocked", []))
    pending_names = {p.get("gate") for p in pending if isinstance(p, dict)}
    by_name = {str(g.get("name", "")): g for g in gates}
    for entry in harvest.get("results", []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("gate", ""))
        gate = by_name.get(name)
        if gate is None:
            continue
        conclusion = str(entry.get("conclusion", ""))
        provenance = (
            f"{entry.get('fact', '')}; {entry.get('source', '')}; "
            f"run {harvest.get('run_id', '?')} rev "
            f"{harvest.get('protocol_revision', '?')}"
        ).strip("; ")
        if conclusion == "PASS":
            gate["status"] = "passing"
            gate["evidence"] = provenance
            gate["fail_streak"] = 0
            if name in pending_names:
                pending = [
                    p for p in pending
                    if not (isinstance(p, dict) and p.get("gate") == name)
                ]
                pending_names.discard(name)
        elif conclusion in {"FAIL", "UNMET"}:
            streak = int(gate.get("fail_streak", 0) or 0) + 1
            gate["fail_streak"] = streak
            if streak >= BLOCK_PROPOSAL_STREAK and name not in pending_names:
                pending.append(
                    {
                        "gate": name,
                        "streak": streak,
                        "evidence": provenance,
                    },
                )
                pending_names.add(name)
        elif conclusion == "ERROR":
            # "No record" is not a transition — the honest state stays open.
            continue
    facts["gates"] = gates
    facts["pending_blocked"] = pending
    return facts


# --------------------------------------------------------------------------- #
# Drift guard                                                                  #
# --------------------------------------------------------------------------- #


def validate_refs(
    gates: tuple[Gate, ...], target_count: int | None,
) -> list[str]:
    """Refs must point inside the current protocol's TARGET numbering."""
    if not gates or target_count is None:
        return []
    problems: list[str] = []
    seen: set[int] = set()
    for gate in gates:
        if gate.ref is None:
            continue
        if not 1 <= gate.ref <= target_count:
            problems.append(
                f"{gate.name}: ref #{gate.ref} outside TARGET items 1..{target_count}",
            )
        elif gate.ref in seen:
            problems.append(f"duplicate ref #{gate.ref}")
        else:
            seen.add(gate.ref)
    return problems


__all__ = [
    "BLOCK_PROPOSAL_STREAK",
    "GateEvaluation",
    "evaluate_gate",
    "evaluate_rubric",
    "render_scorecard",
    "verdict_violations",
    "build_harvest",
    "apply_harvest",
    "validate_refs",
    "parse_gates",
]
