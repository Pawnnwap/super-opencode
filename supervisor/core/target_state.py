"""Durable TARGET state for long self-evolution runs.

This is separate from the human-facing protocol. The protocol remains source of
truth; this module records observed evidence, directions, stalls, and rejected
candidate archives so rollback never erases useful temporary work.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_STATE_DIR = ".opencode"
_STATE_FILE = "target_state.json"
_STAGED_FILE = "staged_improvements.jsonl"
_MAX_DIRECTIONS = 12
_STAGNATION_LIMIT = 2
_TOKEN_RE = re.compile(r"[a-z0-9_./-]{3,}", re.IGNORECASE)
_STOP_WORDS = {
    "agent", "and", "before", "candidate", "code", "current", "from",
    "into", "must", "output", "please", "should", "that", "the", "this",
    "with", "without",
}


@dataclass
class TargetState:
    target: str
    success_criteria: list[str] = field(default_factory=list)
    failure_conditions: list[str] = field(default_factory=list)
    status: str = "running"
    iteration: int = 0
    evidence: list[str] = field(default_factory=list)
    tried_directions: list[str] = field(default_factory=list)
    rejected_directions: list[str] = field(default_factory=list)
    stagnation_count: int = 0
    staged_improvements: int = 0
    last_reason: str = ""
    updated_at: str = ""


def target_state_path(workspace: Path) -> Path:
    return Path(workspace) / _STATE_DIR / _STATE_FILE


def staged_improvements_path(workspace: Path) -> Path:
    return Path(workspace) / _STATE_DIR / _STAGED_FILE


def initialise_target_state(
    workspace: Path,
    *,
    target: str,
    success_criteria: list[str] | None = None,
    failure_conditions: list[str] | None = None,
) -> TargetState:
    """Create fresh state for a run. Never deletes previous staged records."""
    state = TargetState(
        target=target.strip(),
        success_criteria=list(success_criteria or _criteria_from_target(target)),
        failure_conditions=list(failure_conditions or _failure_conditions_from_target(target)),
        updated_at=_now(),
    )
    _write_state(Path(workspace), state)
    return state


def load_target_state(workspace: Path) -> TargetState | None:
    path = target_state_path(Path(workspace))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("target"), str):
        return None
    fields = {field.name for field in TargetState.__dataclass_fields__.values()}
    return TargetState(**{key: value for key, value in payload.items() if key in fields})


def record_target_iteration(
    workspace: Path,
    *,
    iteration: int,
    direction: str,
    evidence: str,
    accepted: bool,
    reason: str,
) -> TargetState:
    """Record evaluator result and expose repeated rejected work as stagnation."""
    state = load_target_state(workspace)
    if state is None:
        state = initialise_target_state(workspace, target="Unnamed TARGET")
    state.iteration = max(state.iteration, iteration)
    normalized = direction_fingerprint(direction)
    if normalized and normalized not in state.tried_directions:
        state.tried_directions.append(normalized)
        state.tried_directions = state.tried_directions[-_MAX_DIRECTIONS:]
    if evidence:
        state.evidence.append(evidence.strip())
        state.evidence = state.evidence[-12:]
    if accepted:
        state.stagnation_count = 0
        state.status = "running"
    else:
        state.status = "replan_required" if state.stagnation_count + 1 >= _STAGNATION_LIMIT else "retry_with_new_direction"
        state.stagnation_count += 1
        if normalized and normalized not in state.rejected_directions:
            state.rejected_directions.append(normalized)
            state.rejected_directions = state.rejected_directions[-_MAX_DIRECTIONS:]
    state.last_reason = reason.strip()
    state.updated_at = _now()
    _write_state(Path(workspace), state)
    return state


def stage_temporary_improvement(
    workspace: Path,
    *,
    iteration: int,
    direction: str,
    changed_files: list[str],
    archive_path: Path | None,
    test_evidence: str,
    reason: str,
    output_excerpt: str,
) -> Path:
    """Append a rejected candidate record; its archive remains recoverable."""
    path = staged_improvements_path(Path(workspace))
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "recorded_at": _now(),
        "iteration": iteration,
        "direction": direction_fingerprint(direction),
        "changed_files": list(changed_files),
        "archive_path": str(archive_path) if archive_path else "",
        "test_evidence": test_evidence,
        "reason": reason,
        "output_excerpt": " ".join(output_excerpt.split())[:600],
        "status": "rolled_back_but_preserved",
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    state = load_target_state(workspace)
    if state is not None:
        state.staged_improvements += 1
        state.updated_at = _now()
        _write_state(Path(workspace), state)
    return path


def read_staged_improvements(workspace: Path, max_records: int = 10) -> list[dict[str, Any]]:
    path = staged_improvements_path(Path(workspace))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines[-max_records:]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def similar_rejected_direction(state: TargetState, direction: str) -> str | None:
    """Return matching rejected direction when candidate repeats its approach."""
    candidate = set(direction_fingerprint(direction).split())
    if not candidate:
        return None
    for previous in reversed(state.rejected_directions):
        prior = set(previous.split())
        union = candidate | prior
        if union and len(candidate & prior) / len(union) >= 0.75:
            return previous
    return None


def target_state_context(workspace: Path) -> str:
    """Compact state injected into restarts; never raw candidate output."""
    state = load_target_state(workspace)
    if state is None:
        return ""
    lines = ["--- TARGET state ---", f"Status: {state.status}"]
    if state.success_criteria:
        lines.append("Acceptance: " + "; ".join(state.success_criteria[:3]))
    if state.rejected_directions:
        lines.append("Avoid repeated directions: " + " | ".join(state.rejected_directions[-3:]))
    if state.last_reason:
        lines.append("Last evaluator reason: " + state.last_reason[:240])
    if state.staged_improvements:
        lines.append(f"Preserved temporary candidates: {state.staged_improvements}")
    return "\n".join(lines)


def direction_fingerprint(text: str) -> str:
    tokens = {
        token.lower()
        for token in _TOKEN_RE.findall(text)
        if token.lower() not in _STOP_WORDS
    }
    return " ".join(sorted(tokens)[:24])


def _criteria_from_target(target: str) -> list[str]:
    lines = [line.strip(" -0123456789.)") for line in target.splitlines()]
    return [line for line in lines if line][:5]


def _failure_conditions_from_target(target: str) -> list[str]:
    lowered = target.lower()
    conditions = []
    if "test" in lowered or "pytest" in lowered:
        conditions.append("Test failure blocks acceptance.")
    if "regression" in lowered:
        conditions.append("Regression requires rollback or replanning.")
    return conditions or ["Evaluator rejection requires a new direction."]


def _write_state(workspace: Path, state: TargetState) -> None:
    path = target_state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".target_state_", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(state), handle, indent=2, sort_keys=True)
        Path(tmp_name).replace(path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _now() -> str:
    return datetime.now(UTC).isoformat()
