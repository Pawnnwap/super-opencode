"""Parsing of opencode ``--format json`` event lines.

opencode's ``run --format json`` emits one JSON object per line. The shapes we
care about (verified against opencode 2.x output) are::

    {"type":"step_start", "part":{"type":"step-start", ...}}
    {"type":"text",       "part":{"type":"text","text":"...", ...}}
    {"type":"tool_use",   "part":{"type":"tool","tool":"bash",
                                  "state":{"status":"completed",
                                           "input":{...},"output":"...big..."}}}
    {"type":"step_finish","part":{"type":"step-finish",
                                  "tokens":{"total":16531, ...}, ...}}

This module is deliberately pure (no I/O, no subprocess) so it can be unit
tested against captured event lines.  The supervisor consumes it to:

  * keep the model's prose (``text`` events) as the real turn output,
  * reduce each ``tool_use`` to a compact ``[tool] <intent>`` marker — the
    verbose tool input/output is dropped entirely, and the marker itself is
    kept only for live logging, NOT placed in the supervisor's context
    (intermediate tool use is "not so important" for judging). See
    ``build_output`` for the exact policy and the tool-only-turn fallback.
  * read REAL context-token counts off ``step_finish`` instead of estimating.

Anything unrecognised, or non-JSON output (e.g. a plain-text provider error),
falls back to raw text so behaviour degrades gracefully if opencode ever
changes its event schema.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# Cap how much of a tool's intent we keep in its compact marker. The marker is
# a breadcrumb of *what the agent did*, not a transcript — so a short slice is
# plenty and keeps history lean.
_MAX_TOOL_DETAIL = 200

# Tool-input fields, most human-readable first, used to describe a tool call.
_TOOL_DETAIL_KEYS = (
    "description",
    "command",
    "query",
    "pattern",
    "url",
    "filePath",
    "file_path",
    "path",
)


@dataclass
class LineEvent:
    """Normalised result of classifying a single json event line.

    Shared by the opencode and codex parsers. opencode never populates
    ``session_id`` (it captures sessions out-of-band); codex sets it from the
    ``thread.started`` event and uses the ``"session"`` / ``"error"`` kinds.
    """

    kind: str  # "text" | "tool" | "tokens" | "session" | "error" | "other" | "raw"
    text: str = ""  # populated for kind in ("text", "error")
    marker: str = ""  # populated for kind == "tool"
    tokens_total: int = 0  # populated for kind == "tokens"
    raw: str = ""  # populated for kind == "raw" (non-JSON fallback)
    session_id: str = ""  # populated for kind == "session"


def _tool_marker(part: dict) -> str:
    """Build a compact ``[tool] <intent>`` marker from a tool_use part."""
    tool = part.get("tool") or "tool"
    if not isinstance(tool, str) or not tool.strip():
        tool = "tool"

    state = part.get("state")
    inp = state.get("input") if isinstance(state, dict) else None

    detail = ""
    if isinstance(inp, dict):
        for key in _TOOL_DETAIL_KEYS:
            val = inp.get(key)
            if isinstance(val, str) and val.strip():
                detail = val.strip()
                break

    if detail:
        detail = " ".join(detail.split())[:_MAX_TOOL_DETAIL]
        return f"[{tool}] {detail}"
    return f"[{tool}]"


def classify_line(line: str | None) -> LineEvent | None:
    """Classify one opencode json event line.

    Returns ``None`` for blank lines.  Non-JSON / unexpected shapes return a
    ``raw`` event so the caller can still surface the text.
    """
    if line is None:
        return None
    line = line.strip()
    if not line:
        return None

    try:
        event = json.loads(line)
    except (json.JSONDecodeError, TypeError, ValueError):
        return LineEvent(kind="raw", raw=line)

    if not isinstance(event, dict):
        return LineEvent(kind="raw", raw=line)

    etype = event.get("type", "")
    part = event.get("part")
    if not isinstance(part, dict):
        part = {}

    if etype == "text":
        txt = part.get("text", "")
        return LineEvent(kind="text", text=txt if isinstance(txt, str) else "")

    if etype == "tool_use":
        return LineEvent(kind="tool", marker=_tool_marker(part))

    if etype in ("step_finish", "step-finish"):
        tokens = part.get("tokens")
        total = tokens.get("total") if isinstance(tokens, dict) else None
        n = int(total) if isinstance(total, (int, float)) and total > 0 else 0
        return LineEvent(kind="tokens", tokens_total=n)

    # step_start and any other recognised-but-uninteresting event.
    return LineEvent(kind="other")


def build_output(
    *,
    text_parts: list[str],
    markers: list[str],
    saw_json: bool,
    raw_parts: list[str],
    include_markers: bool = False,
) -> str:
    """Compose the compacted turn output fed back to the supervisor.

    Intermediate tool-use markers are *not* in the supervisor's context by
    default: only the model's prose is returned. The markers are still emitted
    as live events for logging/UI, so nothing is lost operationally — they just
    don't clutter the judge's context.

    The marker trail is used as a fallback only when the turn produced no prose
    at all, so a tool-only turn still surfaces *something* instead of empty
    output (which the loop would treat as a no-op failure). Pass
    ``include_markers=True`` to prepend the trail in-band (e.g. for tooling
    that wants the breadcrumb).

    When no json events were seen, fall back to the raw lines (covers
    plain-text errors or a future schema change).
    """
    if saw_json:
        prose = "".join(text_parts).strip()
        if prose:
            if include_markers and markers:
                return (" ".join(markers) + "\n\n" + prose).strip()
            return prose
        # No prose this turn — fall back to the marker trail so a tool-only
        # turn is not surfaced as empty output.
        if markers:
            return " ".join(markers).strip()
        return ""
    return "\n".join(raw_parts).strip()
