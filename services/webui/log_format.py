"""Framework-free log-line formatting helpers shared by the web UI.

Moved out of the old Streamlit log_ui so tests can exercise them without a UI
framework installed.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from supervisor.utils.text_utils import sanitize_event_message

# Noise levels (heartbeat / step-progress / token pings) — never rendered.
DEFAULT_HIDDEN_LEVELS = ("heartbeat", "step_progress", "tokens")

# Large-block headline events. The log panel's "Agent speech" toggle governs
# opencode_output; prompts and supervisor verdicts always render.
VERBOSE_ONLY_LEVELS = (
    "opencode_prompt",
    "opencode_output",
    "supervisor_response",
    "supervisor_suggestions",
)

# Phase pipeline shown as a breadcrumb above the live log. Plan happens once
# (the plan-mode stage, or the agent's first look around); then the agent's
# code -> test turn and the judge's review repeat as rounds until the verdict
# says DONE.
LOOP_PHASES = ("code", "test", "review")
PHASE_ORDER = ("plan", *LOOP_PHASES, "done")


def safe_logs(status: dict) -> list[dict]:
    return status.get("logs") or []


def last_event_line(status_or_job: dict, limit: int = 120) -> str:
    """First line of the newest non-heartbeat event, '' when there is none.

    Shared by the board card, the board snapshot and the live screen — each
    had its own copy of this loop, and they had already drifted apart on
    which levels count as noise.
    """
    status = status_or_job.get("status", status_or_job)
    for event in reversed(safe_logs(status)):
        if not isinstance(event, dict) or event.get("level") in DEFAULT_HIDDEN_LEVELS:
            continue
        msg = (event.get("msg") or "").strip()
        if msg:
            return msg.splitlines()[0][:limit]
    return ""


def attention_count(status: dict) -> int:
    """Number of ``error`` events in a job's log tail.

    Rendered as the ❌ badge on a board card; the same count was open-coded in
    two places.
    """
    return sum(
        1
        for event in safe_logs(status)
        if isinstance(event, dict) and event.get("level") == "error"
    )


def workspace_name(status_or_job: dict) -> str:
    """Basename of a job's workspace, '' when the record has none."""
    status = status_or_job.get("status", status_or_job)
    workspace = status.get("config", {}).get("workspace", "")
    return Path(workspace).name if workspace else ""


def workspace_path(status_or_job: dict) -> str:
    """Full workspace path recorded in a job, '' when absent."""
    status = status_or_job.get("status", status_or_job)
    return status.get("config", {}).get("workspace", "")


def fmt_ts(event: dict) -> str:
    """Format an event's logged timestamp as HH:MM:SS, or '' if absent."""
    ts = event.get("ts")
    if not isinstance(ts, (int, float)) or ts <= 0:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(ts))


def fmt_elapsed(ts, start_ts) -> str:
    """Format ts relative to start_ts as +Ns / +Mm SSs / +Hh MMm, or ''."""
    if not isinstance(ts, (int, float)) or not isinstance(start_ts, (int, float)):
        return ""
    if ts <= 0 or start_ts <= 0:
        return ""
    sec = int(max(0, ts - start_ts))
    if sec < 60:
        return f"+{sec}s"
    mins, secs = divmod(sec, 60)
    if mins < 60:
        return f"+{mins}m{secs:02d}s"
    hours, mins = divmod(mins, 60)
    return f"+{hours}h{mins:02d}m"


def event_counts(events: list) -> dict:
    """Count error / warn / tool events for the health badge."""
    counts = {"error": 0, "warn": 0, "tool": 0}
    for event in events:
        if isinstance(event, dict):
            level = event.get("level")
            if level in counts:
                counts[level] += 1
    return counts


def start_ts(events: list) -> float | None:
    """Earliest event timestamp (run start) for relative-time display."""
    start = None
    for event in events:
        if not isinstance(event, dict):
            continue
        ts = event.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            start = ts if start is None else min(start, ts)
    return start


def filter_events(events: list, search: str, skip: set | None) -> list[dict]:
    """Filter events by hidden levels (skip) and a case-insensitive search."""
    out: list[dict] = []
    needle = (search or "").lower()
    skip = skip or set()
    for event in events:
        if not isinstance(event, dict):
            continue
        if (event.get("level") or "info") in skip:
            continue
        if needle:
            msg = sanitize_event_message(event.get("msg") or "")
            if needle not in msg.lower():
                continue
        out.append(event)
    return out


def latest_token_current(logs: list) -> int | None:
    """Latest structured token count from `tokens` events (None if none)."""
    current = None
    for event in logs:
        if not isinstance(event, dict) or event.get("level") != "tokens":
            continue
        value = event.get("current")
        if isinstance(value, (int, float)) and value > 0:
            current = int(value)
    return current


def regex_token_current(logs: list) -> tuple[int, float] | None:
    """Fallback: scrape 'N / M tokens' out of context-usage warning strings."""
    latest_current, latest_fraction, found = 0, 0.0, False
    for event in logs:
        if not isinstance(event, dict):
            continue
        msg = event.get("msg") or ""
        if "context usage" not in msg.lower():
            continue
        match = re.search(r"(\d[\d,]*)\s*/\s*(\d[\d,]*)\s*tokens", msg)
        if not match:
            continue
        current = int(match.group(1).replace(",", ""))
        max_t = int(match.group(2).replace(",", ""))
        fraction = current / max_t if max_t > 0 else 0
        if fraction >= latest_fraction:
            latest_fraction, latest_current, found = fraction, current, True
    return (latest_current, latest_fraction) if found else None


@dataclass(frozen=True)
class PhaseState:
    """Where a run is in plan -> [code -> test -> review] x N -> done."""

    phase: str = ""  # "" (nothing yet) | plan | code | test | review | done
    round: int = 0  # code/test/review rounds opened so far
    exact: bool = True  # False: the event window lost the run start, round is a floor
    visited: frozenset = frozenset()  # loop phases seen in the current round
    end: str = ""  # "" while live; success | blocked | failed once the job ended


_PROMPT_HEADLINE = re.compile(r"^→\s*(\w+) prompt\b")
_TOOL_MARKER = re.compile(r"^\[(\w+)\]\s*(.*)", re.S)
# File-mutating tools by name (opencode/pi edit|write|patch, codex file_change -> edit).
_WRITE_TOOLS = frozenset(
    {"edit", "write", "patch", "multiedit", "apply_patch", "create", "file_change"},
)
_SHELL_TOOLS = frozenset(
    {"shell", "bash", "sh", "exec", "command", "run", "powershell", "cmd", "terminal"},
)
_TEST_COMMAND = re.compile(
    r"\b(?:pytest|unittest|nose2?|tox|jest|vitest|mocha|cargo test|go test"
    r"|npm (?:run )?test|yarn test|pnpm test|mvn test|gradle test|phpunit|rspec"
    r"|ctest|make (?:test|check)|ruff|flake8|pylint|mypy|pyright|eslint|tsc)\b"
    r"|\btest_\w+\.py\b",
    re.IGNORECASE,
)
# Shell-side file writes: PowerShell cmdlets, patch tools, in-place sed, and
# redirects into source/config files (not logs: `pytest > out.txt` is a test).
_WRITE_COMMAND = re.compile(
    r"\b(?:Set-Content|Out-File|Add-Content|apply_patch|git apply|sed -i)\b"
    r"|(?:^|[|;&]\s*)cat\s*>>?\s*\S"
    r"|>>?\s*['\"]?[\w./\\-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|rs|go|java|kt|c|h|cpp|hpp"
    r"|cs|rb|php|sh|ps1|toml|ya?ml|cfg|ini)\b",
    re.IGNORECASE,
)
_JOB_END = {"SUCCESS": "success", "BLOCKED": "blocked", "FAILED": "failed", "CANCELLED": "failed"}


def tool_phase(msg: str) -> str:
    """'code' / 'test' for a `[tool] detail` marker, '' when it says neither.

    Reads, searches and plain script runs are neutral: they never move the
    breadcrumb, so an agent reading around between edits stays in `code`.
    """
    match = _TOOL_MARKER.match(msg or "")
    if not match:
        return ""
    tool, detail = match.group(1).lower(), match.group(2)
    if tool in _WRITE_TOOLS:
        return "code"
    if tool in _SHELL_TOOLS:
        if _WRITE_COMMAND.search(detail):
            return "code"
        if _TEST_COMMAND.search(detail):
            return "test"
    return ""


def _prompt_kind(event: dict) -> str:
    # Headline mode folds prompt_kind into the message ("→ turn prompt …");
    # full mode keeps the field.
    kind = event.get("prompt_kind")
    if isinstance(kind, str) and kind:
        return kind
    match = _PROMPT_HEADLINE.match(event.get("msg") or "")
    return match.group(1) if match else "prompt"


def _verdict_is_done(event: dict) -> bool:
    text = event.get("digest") or event.get("msg") or ""
    return str(text).lstrip().startswith("⚖ DONE")


def phase_state(events: list, state: str = "") -> PhaseState:
    """Rebuild the pipeline position from the job's structural events.

    The regex step detector's `step_progress` is ignored on purpose: it keys
    on prose, every agent turn ends in a summary, so it read "review" almost
    permanently and its completed list was an unordered history. The loop's
    own events are exact: a plan-mode stage (`log-plan_phase` / plan prompts),
    then per round an initial/turn/restart prompt, live `tool` markers
    (write -> code, test runner -> test), the turn's `opencode_output`
    (review begins) and the judge's `supervisor_response` (CONTINUE closes
    the round, DONE finishes).

    Context compaction interrupts a round without judging it: the judge's
    deletion-permission answer is logged as a `supervisor_response` too, so a
    response is only a verdict when the next prompt is not a `compaction`
    one, and everything from that compaction prompt to the `restart` prompt
    is housekeeping, not a new round.
    """
    events = [event for event in events if isinstance(event, dict)]
    next_prompt = [""] * len(events)
    upcoming = ""
    for index in range(len(events) - 1, -1, -1):
        next_prompt[index] = upcoming
        if events[index].get("level") == "opencode_prompt":
            upcoming = _prompt_kind(events[index])

    phase = ""
    stage = "idle"  # idle | plan | main
    rounds = 0
    saw_initial = False
    open_round = True  # the next main-loop prompt starts a new round
    compacting = False
    visited: set[str] = set()

    def enter_main() -> None:
        # Agent/judge activity with no prompt seen first: the window starts
        # mid-run, so we are inside some round already.
        nonlocal stage, rounds, open_round
        if stage != "main":
            stage = "main"
            if open_round:
                rounds += 1
                open_round = False

    for index, event in enumerate(events):
        level = event.get("level") or ""
        if level == "log-plan_phase":
            if stage != "main":
                stage, phase = "plan", "plan"
        elif level == "opencode_prompt":
            kind = _prompt_kind(event)
            if kind == "plan":
                if stage != "main":
                    stage, phase = "plan", "plan"
                continue
            planned = stage == "plan"
            if kind == "initial":
                saw_initial = True
            stage = "main"
            if kind == "compaction":
                compacting = True
                phase = "code"
                continue
            compacting = False
            if open_round:
                rounds += 1
                open_round = False
                visited = set()
                # First turn without a plan stage: the agent looks around
                # before its first edit, and that is its planning.
                phase = "plan" if kind == "initial" and not planned else "code"
            else:
                phase = "code"
        elif stage == "plan" or compacting:
            continue
        elif level == "tool":
            enter_main()
            cls = tool_phase(event.get("msg") or "")
            if cls:
                phase = cls
                visited.add(cls)
            elif phase not in ("plan", "code", "test"):
                phase = "code"
        elif level == "opencode_output":
            enter_main()
            phase = "review"
            visited.add("review")
        elif level == "supervisor_response":
            if next_prompt[index] == "compaction":
                continue
            enter_main()
            open_round = True
            visited.add("review")
            phase = "done" if _verdict_is_done(event) else "review"

    end = _JOB_END.get(state or "", "")
    if end == "success":
        phase = "done"
    return PhaseState(
        phase=phase,
        round=rounds,
        exact=saw_initial or stage != "main",
        visited=frozenset(visited),
        end=end,
    )


def phase_breadcrumb(ps: PhaseState) -> str:
    """Breadcrumb `plan → [code → test → review] ×N → done` as inline HTML.

    The only consumer renders it via ui.html, so phases are real spans
    instead of markdown — a plain-text label would show the asterisks
    literally. Each phase is classed phase-current / phase-done /
    phase-skipped / phase-todo, arrows phase-arrow-lit / -dim up to the
    current phase, the loop phases sit in a phase-loop bracket followed by a
    phase-round badge (×N, or ×N+ when the event window lost the run start),
    and a finished job tints the chip via phase-end-success / -blocked /
    -failed. Labels are constants, so no escaping is needed.
    """
    current = PHASE_ORDER.index(ps.phase) if ps.phase in PHASE_ORDER else -1
    finished = ps.phase == "done"

    def span(name: str) -> str:
        index = PHASE_ORDER.index(name)
        if index == current:
            cls = "phase-current" + (f" phase-end-{ps.end}" if ps.end else "")
        elif index < current:
            # An earlier loop phase is only done if this round actually hit
            # it; a turn that never ran tests shows test as skipped.
            skipped = name in LOOP_PHASES and not finished and name not in ps.visited
            cls = "phase-skipped" if skipped else "phase-done"
        else:
            cls = "phase-todo"
        return f'<span class="{cls}">{name}</span>'

    def arrow(into: str) -> str:
        lit = "phase-arrow-lit" if PHASE_ORDER.index(into) <= current else "phase-arrow-dim"
        return f'<span class="phase-arrow {lit}">→</span>'

    loop = (
        '<span class="phase-loop">'
        + span("code") + arrow("test") + span("test") + arrow("review") + span("review")
        + "</span>"
    )
    badge = ""
    if ps.round and ps.phase != "plan":
        badge = f'<span class="phase-round">×{ps.round}{"" if ps.exact else "+"}</span>'
    return (
        '<span class="phase-crumb">'
        + span("plan") + arrow("code") + loop + badge + arrow("done") + span("done")
        + "</span>"
    )


# Inline markers whose unbalanced leftovers would show literally once a
# markdown renderer sees them without their closing pair.
_MD_PAIR_MARKS = ("**", "~~", "__", "`")


def markdown_preview(text: str, limit: int) -> str:
    """Truncate a markdown document for a preview without literal markup.

    A hard slice can land inside an emphasis run, so the renderer meets an
    unclosed ** / ~~ / ` and prints it as-is. Cut at the last blank-line
    boundary within ``limit``, then drop any trailing fragment left with an
    odd marker count. Text already within ``limit`` is returned unchanged.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    preview = text[:limit]
    # Prefer ending at a paragraph edge, but only when that still keeps at
    # least half the budget — otherwise the hard cut + marker cleanup below
    # preserves more content.
    boundary = preview.rfind("\n\n")
    if boundary >= limit // 2:
        preview = preview[:boundary]
    for mark in _MD_PAIR_MARKS:
        if preview.count(mark) % 2:
            preview = preview[:preview.rfind(mark)]
    return preview
