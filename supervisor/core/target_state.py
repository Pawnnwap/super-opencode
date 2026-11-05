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

from supervisor.utils.text_utils import head_tail_excerpt

_STATE_DIR = ".opencode"
_STATE_FILE = "target_state.json"
# The goal audit journal. Written by three modules (the guard appends, the
# judge harness reads its tail, this module scans it for terminal assertions),
# so the name lives here as the single definition they all import.
AUDIT_FILENAME = "goal_audit.jsonl"
# The agent's own terminal assertions. Studies write these into the audit
# trail when they hit a certified ceiling; they used to be inert side-notes
# that the harness ignored while it kept buying turns.
_TERMINAL_EVENTS = frozenset(
    {
        "goal_terminal",
        "terminal_gap",
        "run_closed_by_agent_on_director_directive",
        "run_closed_by_agent",
    }
)
_TERMINAL_EVENT_MARKERS = ("_closed", "terminal")
# Closure documents the study agents actually write. The audit journal is the
# machine-readable channel, but agents close a study in prose far more often
# (ESCALATION.md carried "TERMINAL RULING ... CLOSED ... NO REOPENING" while
# the harness kept paying for 100+ turns). Only a line that states both
# "terminal ruling" and "closed" counts, so ordinary discussion of a closure
# does not stop a live run.
_CLOSURE_DOCS = (".opencode/closure.md", "ESCALATION.md")
_TERMINAL_RULING_RE = re.compile(r"terminal\s+ruling", re.IGNORECASE)
_CLOSED_WORD_RE = re.compile(r"\bclosed\b", re.IGNORECASE)
_STAGED_FILE = "staged_improvements.jsonl"
_MAX_DIRECTIONS = 12
_MAX_LESSONS = 8
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
    lessons: list[str] = field(default_factory=list)
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
    """Create fresh state for a run. Never deletes previous staged records.

    A state file already marked ``closed`` is left alone: the previous run
    reached a terminal outcome and a fresh cycle must not silently re-open it.
    """
    workspace = Path(workspace)
    existing = load_target_state(workspace)
    if existing is not None and existing.status == "closed":
        return existing
    state = TargetState(
        target=target.strip(),
        success_criteria=list(success_criteria or _criteria_from_target(target)),
        failure_conditions=list(failure_conditions or _failure_conditions_from_target(target)),
        updated_at=_now(),
    )
    _write_state(workspace, state)
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
    accepted: bool | None,
    reason: str,
) -> TargetState:
    """Record evaluator result and expose repeated rejected work as stagnation.

    ``accepted=None`` means the turn's attainment could not be measured (the
    judge emitted no structured criteria): the turn is journaled, but it must
    not reset the stagnation counter on file churn nor inflate it on a
    measurement that was never made.
    """
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
    if accepted is None:
        pass
    elif accepted:
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
        "output_excerpt": head_tail_excerpt(
            " ".join(output_excerpt.split()), 600, end_ratio=0.5,
        ),
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


def audit_journal_size(workspace: Path) -> int:
    """Byte size of the goal audit journal, 0 when absent.

    A loop captures this at start; terminal events written before the offset
    belong to earlier cycles. Unlike the timestamp filter this also scopes
    agent-written events that carry no ``ts`` field at all.
    """
    path = Path(workspace) / _STATE_DIR / AUDIT_FILENAME
    try:
        return path.stat().st_size
    except OSError:
        return 0


def read_terminal_assertion(
    workspace: Path,
    *,
    since_ts: float | None = None,
    baseline: frozenset[str] | None = None,
    offset: int | None = None,
) -> str:
    """The agent's own latest terminal assertion, '' when it made none.

    Reads ``<workspace>/.opencode/goal_audit.jsonl`` — the journal the study
    agents write themselves — and returns a one-line description of the most
    recent terminal event (``goal_terminal``, ``terminal_gap``, any
    ``*_closed_*``). These events were previously inert: the loop ignored them
    and relaunched, resetting ``target_state.json`` to ``running`` each time.

    When the journal carries no terminal event, the workspace closure
    documents (``.opencode/closure.md``, ``ESCALATION.md``) are scanned for an
    explicit ruling line — ``terminal ruling`` and ``closed`` on one line.
    ``since_ts`` restricts that scan to documents written during this run, and
    ``baseline`` (see :func:`closure_ruling_lines`) drops ruling lines that
    already existed when the run started. The baseline is what makes the
    gate robust: appending an unrelated note to ``ESCALATION.md`` refreshes
    its mtime, and without it an old ruling would become "current" again.
    ``offset`` (see :func:`audit_journal_size`) skips journal bytes written
    before this run, so a timeless old event cannot re-close a relaunch.

    The WHOLE journal (from ``offset`` to EOF) is scanned, line by line, with
    a cheap substring prefilter before any JSON parsing. The earlier 2 MB
    tail window silently dropped terminal events once a long study's audit
    grew past it — exactly the runs where an infinite-relaunch bug is most
    expensive. A per-line substring scan of even a tens-of-MB JSONL costs
    milliseconds (same pattern as the job-log session-id scan), so no bound
    is worth the miss risk here.
    """
    path = Path(workspace) / _STATE_DIR / AUDIT_FILENAME
    latest = ""
    try:
        if path.is_file():
            size = path.stat().st_size
            start = offset if offset is not None and 0 <= offset <= size else 0
            with path.open("rb") as handle:
                if start:
                    handle.seek(start)
                for raw in handle:
                    line = raw.decode("utf-8", "replace")
                    if "terminal" not in line and "_closed" not in line:
                        continue
                    latest = _parse_terminal_line(line, since_ts) or latest
    except OSError:
        latest = ""
    if latest:
        return latest
    return _read_closure_document(workspace, since_ts, baseline)


def closure_ruling_lines(workspace: Path) -> frozenset[str]:
    """Terminal-ruling lines present in the closure documents right now.

    A run captures this set at start; any line in it belongs to an earlier
    cycle and cannot close the new run, even if a later unrelated edit
    refreshes the file's mtime.
    """
    lines: set[str] = set()
    for rel, text in _closure_documents(workspace, None):
        for line in text.splitlines():
            key = _closure_line_key(rel, line)
            if key:
                lines.add(key)
    return frozenset(lines)


def _read_closure_document(
    workspace: Path,
    since_ts: float | None,
    baseline: frozenset[str] | None,
) -> str:
    """One-line terminal ruling from a closure document, '' when absent."""
    for rel, text in _closure_documents(workspace, since_ts):
        for line in text.splitlines():
            key = _closure_line_key(rel, line)
            if key and (not baseline or key not in baseline):
                return key
    return ""


def _closure_documents(workspace: Path, since_ts: float | None):
    """Yield ``(relative_path, text)`` for readable closure documents."""
    for rel in _CLOSURE_DOCS:
        path = Path(workspace) / rel
        try:
            stat = path.stat()
            if since_ts is not None and stat.st_mtime + 1.0 < since_ts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        yield rel, text


def _closure_line_key(rel: str, line: str) -> str:
    """Normalized ruling-line key, '' when the line is not a ruling."""
    compact = " ".join(line.split())
    if _TERMINAL_RULING_RE.search(compact) and _CLOSED_WORD_RE.search(compact):
        return f"{rel}: {compact[:200]}"
    return ""


def _parse_terminal_line(line: str, since_ts: float | None = None) -> str:
    line = line.strip()
    if not line:
        return ""
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return ""
    if not isinstance(record, dict):
        return ""
    if since_ts is not None:
        # A terminal event recorded before this run began belongs to a
        # previous cycle; an explicit relaunch must not be re-closed by it.
        raw_ts = record.get("ts")
        if raw_ts:
            try:
                moment = datetime.fromisoformat(str(raw_ts))
            except ValueError:
                moment = None
            if moment is not None and moment.timestamp() + 1.0 < since_ts:
                return ""
    event = str(record.get("event", "")).strip().lower()
    if not event:
        return ""
    if event not in _TERMINAL_EVENTS and not any(
        marker in event for marker in _TERMINAL_EVENT_MARKERS
    ):
        return ""
    reason = " ".join(
        " ".join(
            str(record.get(key, "") or "").split()
            if not isinstance(record.get(key), (list, tuple))
            else " ".join(str(v) for v in record.get(key) or ())
        )
        for key in ("reason", "new_status", "study")
    ).strip()
    return f"{event}" + (f": {reason[:200]}" if reason else "")


def mark_target_closed(
    workspace: Path,
    reason: str,
    *,
    status: str = "closed",
) -> TargetState | None:
    """Write the durable terminal status the harness could never express.

    ``record_target_iteration`` only ever wrote ``running`` /
    ``retry_with_new_direction`` / ``replan_required``, so an agent asking the
    harness for ``status="closed"`` got ``running`` back on the next relaunch.
    The BLOCKED exit calls this so the durable state agrees with the run's
    terminal outcome. ``status="blocked"`` is the reopenable variant: the run
    is over, but the evidence base contradicted a permanent closure.
    """
    fd_state = load_target_state(workspace)
    if fd_state is None:
        return None
    fd_state.status = status
    fd_state.last_reason = " ".join((reason or "").split())[:400]
    fd_state.updated_at = _now()
    _write_state(Path(workspace), fd_state)
    return fd_state


def record_lesson(workspace: Path, lesson: str) -> TargetState | None:
    """Append a bounded Reflexion-style lesson for injection into later prompts."""
    if not lesson:
        return load_target_state(workspace)
    state = load_target_state(workspace)
    if state is None:
        return None
    if lesson not in state.lessons:
        state.lessons.append(lesson)
        state.lessons = state.lessons[-_MAX_LESSONS:]
    state.updated_at = _now()
    _write_state(Path(workspace), state)
    return state


def target_state_context(workspace: Path) -> str:
    """Compact state injected into restarts; never raw candidate output."""
    state = load_target_state(workspace)
    if state is None:
        return ""
    lines = ["--- TARGET state ---", f"Status: {state.status}"]
    if state.success_criteria:
        lines.append("Acceptance: " + "; ".join(state.success_criteria[:5]))
    if state.lessons:
        lines.append("Lessons so far: " + " | ".join(state.lessons[-3:]))
    if state.rejected_directions:
        lines.append("Avoid repeated directions: " + " | ".join(state.rejected_directions[-3:]))
    if state.last_reason:
        # Evaluator reasons end with the verdict — keep both ends.
        lines.append(
            "Last evaluator reason: "
            + head_tail_excerpt(state.last_reason, 240, end_ratio=0.5),
        )
    if state.staged_improvements:
        lines.append(f"Preserved temporary candidates: {state.staged_improvements}")
    lines.append(
        "To end the run when the target is unreachable, append "
        '{"event": "goal_terminal", "reason": "<why>"} to '
        ".opencode/goal_audit.jsonl (machine-readable, honored by the harness).",
    )
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
