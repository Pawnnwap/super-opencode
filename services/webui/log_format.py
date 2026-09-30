"""Framework-free log-line formatting helpers shared by the web UI.

Moved out of the old Streamlit log_ui so tests can exercise them without a UI
framework installed.
"""

from __future__ import annotations

import re
import time

from supervisor.utils.text_utils import sanitize_event_message

# Levels hidden from the log box unless the user opts in ("Show noise").
DEFAULT_HIDDEN_LEVELS = ("heartbeat", "step_progress", "tokens")

# Phase pipeline shown as a breadcrumb above the live log.
PHASE_SEQUENCE = [
    ("plan", "planning"),
    ("code", "coding"),
    ("test", "testing"),
    ("review", "review"),
    ("done", "completing"),
]


def safe_logs(status: dict) -> list[dict]:
    return status.get("logs") or []


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


def phase_breadcrumb(current_phase: str, completed_phases: list | None) -> str:
    """Breadcrumb 'plan -> code -> test -> review' as inline HTML.

    The only consumer renders it via ui.html, so phases are marked up with
    real tags (<strong> current, <s> completed) instead of markdown — a
    plain-text label would show the asterisks/tildes literally.
    Labels come from the constant PHASE_SEQUENCE, so no escaping is needed.
    """
    current = (current_phase or "").lower()
    done = {str(p).lower() for p in (completed_phases or [])}
    parts: list[str] = []
    for label, name in PHASE_SEQUENCE:
        if name == current:
            parts.append(f"<strong>{label}</strong>")
        elif name in done:
            parts.append(f"<s>{label}</s>")
        else:
            parts.append(label)
    return " → ".join(parts)


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
