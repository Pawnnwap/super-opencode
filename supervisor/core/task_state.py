"""Persistent task-state journal (``TASK_STATE.md``) for long single tasks.

The supervisor's compaction strategy is to write ``summary.md`` and restart the
agent in a fresh session. ``summary.md`` is a single overwritten snapshot, so a
long task that crosses several context resets keeps losing the thread of *what
was already tried*.

This module maintains an append-only progress journal alongside it: one compact
entry per judge turn (turn number, time, phase/step, and a one-line headline
derived from the supervisor's existing verdict — no extra LLM call). On a
context reset the tail of the journal is injected into the restart prompt so the
agent resumes with continuity.

Pure file I/O (no LLM, no the web UI) so it is unit-testable. Inspired by the
plain-markdown memory pattern (pi-mem): readable, editable, no schema, no DB.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from supervisor.utils.text_utils import head_tail_excerpt

logger = logging.getLogger(__name__)

_FILENAME = "TASK_STATE.md"
_HEADER = "# Task State — progress journal (newest at bottom)\n\n"
_ENTRY_RE = re.compile(r"(?m)^## Turn ")
_DEFAULT_HEADLINE_CHARS = 240
_DEFAULT_NEXT_CHARS = 200
_DEFAULT_TAIL_ENTRIES = 8


def task_state_path(workspace) -> Path:
    return Path(workspace) / _FILENAME


def _headline(feedback: str, max_chars: int) -> str:
    """Collapse a verdict's feedback to one line, keeping BOTH ends.

    Verdicts put their conclusion (met/blocked + why) at the end, so a
    head-only cut would drop it; the head keeps the referenced context.
    """
    text = (feedback or "").strip()
    if not text:
        return "(no feedback)"
    line = " ".join(text.split())
    return head_tail_excerpt(line, max_chars, end_ratio=0.5)


def append_task_state(
    workspace,
    *,
    turn: int,
    phase: str,
    step: int,
    total_steps: int,
    targets_met: bool,
    feedback: str,
    next_action: str = "",
    max_chars: int = _DEFAULT_HEADLINE_CHARS,
) -> bool:
    """Append one journal entry. Returns True on success.

    The ``next_action`` line is the continuity payload for context resets —
    it tells the restarted agent what the judge asked for last, which no
    amount of re-reading the workspace recovers.
    """
    headline = "✅ ALL TARGETS MET" if targets_met else _headline(feedback, max_chars)
    next_line = ""
    if (next_action or "").strip():
        next_line = (
            "→ next: "
            + head_tail_excerpt(
                " ".join(next_action.split()), _DEFAULT_NEXT_CHARS, end_ratio=0.5,
            )
            + "\n"
        )
    stamp = time.strftime("%H:%M:%S")
    entry = (
        f"## Turn {turn} — {stamp} — {phase} (step {step}/{total_steps})\n"
        f"{headline}\n"
        f"{next_line}\n"
    )
    path = task_state_path(workspace)
    try:
        if not path.exists():
            path.write_text(_HEADER + entry, encoding="utf-8")
        else:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(entry)
        return True
    except OSError as exc:
        logger.warning("Failed to append %s: %s", _FILENAME, exc)
        return False


def read_task_state_tail(workspace, max_entries: int = _DEFAULT_TAIL_ENTRIES) -> str:
    """Return the last *max_entries* journal entries, or '' if none/unreadable."""
    path = task_state_path(workspace)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    starts = [m.start() for m in _ENTRY_RE.finditer(text)]
    if not starts:
        return ""
    tail_start = starts[-max_entries] if len(starts) > max_entries else starts[0]
    return text[tail_start:].strip()
