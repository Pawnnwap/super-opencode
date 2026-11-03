"""Parsing of pi ``-p --mode json`` event lines.

pi's print mode emits one JSON object per line (JSONL). The envelope below
was verified live (pi 1.4.0)::

    {"type":"session","version":3,"id":"01a19a52-...","cwd":"..."}
    {"type":"agent_start"} / {"type":"turn_start"}
    {"type":"message_update","assistantMessageEvent":{
        "type":"toolcall_end","contentIndex":1,
        "toolCall":{"type":"toolCall","name":"bash",
                    "arguments":{"command":"ls -l"}}}}
    {"type":"message_end","message":{"role":"assistant","content":[
        {"type":"thinking","thinking":"..."},
        {"type":"toolCall","name":"write","arguments":{...}},
        {"type":"text","text":"..."}],
        "usage":{"input":3947,"output":130,"cacheRead":8192,
                 "cacheWrite":0,"reasoning":46,"totalTokens":12269},
        "stopReason":"stop"}}
    {"type":"turn_end","message":{...same shape as message_end...}}
    {"type":"agent_end"} / {"type":"agent_settled","aborted":false}

This module mirrors ``opencode_support.stream`` / ``codex_support.stream``
and reuses its ``LineEvent`` + ``build_output`` so the shared process driver
can:

  * keep assistant ``text`` blocks (from ``message_end``) as the real turn
    output — the ``role`` filter matters: pi also emits ``message_end`` for
    the SYSTEM message (the entire system prompt) and for user/tool-result
    messages, which must never enter the supervisor's context,
  * reduce each ``toolcall_end`` to a compact ``[tool] <intent>`` marker via
    the shared ``tool_marker_from`` (verbose arguments dropped; the marker is
    live-log only, NOT supervisor context),
  * read REAL context tokens off ``turn_end``'s ``usage.totalTokens``. pi
    reports per-model-request usage (unlike codex's turn aggregation), and
    the last request in a turn sees the full context, so the value is used
    as-is — no averaging,
  * capture the ``session`` event's id as the resumable session id,
  * drop ``thinking`` blocks and all streaming deltas (``*_start`` /
    ``*_delta``) as model bookkeeping.

Anything unrecognised, or non-JSON output, falls back to raw text so a
working pi run still surfaces *something* even if a field name drifts.
"""

from __future__ import annotations

import json

from supervisor.runners.opencode_support.stream import (
    LineEvent,
    build_output,
    tool_marker_from,
)

__all__ = [
    "LineEvent",
    "build_output",
    "classify_line",
]


def _assistant_text(message: dict) -> str:
    """Concatenate the ``text`` blocks of one assistant message."""
    parts: list[str] = []
    content = message.get("content")
    for block in content if isinstance(content, list) else []:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            parts.append(block["text"])
    return "".join(parts)


def _usage_total_tokens(message: dict) -> int:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return 0
    total = usage.get("totalTokens")
    if isinstance(total, (int, float)) and total > 0:
        return int(total)
    return 0


def classify_line(line: str | None) -> LineEvent | None:
    """Classify one pi ``--mode json`` event line.

    Returns ``None`` for blank lines. Non-JSON / unexpected shapes return a
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

    if etype == "session":
        sid = event.get("id")
        return LineEvent(kind="session", session_id=sid if isinstance(sid, str) else "")

    if etype == "message_end":
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            # system / user / tool-result echoes — never turn output.
            return LineEvent(kind="other")
        return LineEvent(kind="text", text=_assistant_text(message))

    if etype == "message_update":
        update = event.get("assistantMessageEvent")
        if isinstance(update, dict) and update.get("type") == "toolcall_end":
            call = update.get("toolCall")
            if isinstance(call, dict):
                return LineEvent(
                    kind="tool",
                    marker=tool_marker_from(call.get("name"), call.get("arguments")),
                )
        # thinking_* / text_* / toolcall_start / toolcall_delta — streaming
        # bookkeeping; final state arrives via message_end / turn_end.
        return LineEvent(kind="other")

    if etype == "turn_end":
        message = event.get("message")
        total = _usage_total_tokens(message) if isinstance(message, dict) else 0
        # Per-request usage: the last turn_end in the run carries the final
        # (fullest) context size, and the stream driver keeps the latest
        # value — exactly what the context monitor wants.
        return LineEvent(kind="tokens", tokens_total=total)

    # agent_start / turn_start / agent_end / agent_settled and any other
    # recognised-but-uninteresting event.
    return LineEvent(kind="other")
