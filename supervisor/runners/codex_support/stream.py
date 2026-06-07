"""Parsing of codex ``exec --json`` thread-event lines.

codex's ``exec --json`` emits one JSON object per line (JSONL). The envelope
below was verified live (codex CLI 0.135.0)::

    {"type":"thread.started","thread_id":"019e9f5d-..."}
    {"type":"turn.started"}
    {"type":"turn.failed","error":{"message":"..."}}
    {"type":"error","message":"..."}

The content-bearing events follow the OpenAI Codex non-interactive schema::

    {"type":"item.completed","item":{"id":"...","type":"agent_message","text":"..."}}
    {"type":"item.completed","item":{"id":"...","type":"command_execution",
        "command":"echo hi","aggregated_output":"hi\\n","exit_code":0,
        "status":"completed"}}
    {"type":"turn.completed","usage":{"input_tokens":24763,
        "cached_input_tokens":24448,"output_tokens":122,
        "reasoning_output_tokens":0}}

This module mirrors ``opencode_support.stream`` and reuses its ``LineEvent`` +
``build_output`` so the process layer can:

  * keep ``agent_message`` prose as the real turn output,
  * reduce each tool-ish item (command_execution / file_change / mcp_tool_call
    / web_search / unknown) to a compact ``[tool] <intent>`` marker — the
    verbose ``aggregated_output`` and other I/O is dropped,
  * read REAL context tokens off ``turn.completed`` (input + output),
  * capture ``thread_id`` as the resumable session id,
  * drop ``reasoning`` / ``plan_update`` (model bookkeeping, not output).

IMPORTANT: the item.* / usage shapes come from the published schema, not a
captured live turn (the local probe could not complete a codex turn against
LM Studio's ``/v1/responses`` endpoint). The parser is therefore defensive:
any unrecognised event or non-JSON line falls back to raw text so a working
codex run still surfaces *something* even if a field name drifts.
"""

from __future__ import annotations

import json

from supervisor.runners.opencode_support.stream import LineEvent, build_output

__all__ = ["LineEvent", "build_output", "classify_line"]

# Cap the intent detail kept in a tool marker (a breadcrumb, not a transcript).
_MAX_TOOL_DETAIL = 200

# item.type -> (friendly label, ordered candidate detail fields).
_ITEM_TOOL_LABELS: dict[str, tuple[str, tuple[str, ...]]] = {
    "command_execution": ("shell", ("command",)),
    "file_change": ("edit", ("path", "file_path", "files")),
    "file_changes": ("edit", ("path", "file_path", "files")),
    "patch_apply": ("edit", ("path", "file_path", "files")),
    "mcp_tool_call": ("mcp", ("tool", "name", "server")),
    "web_search": ("search", ("query",)),
}

# item.type values that are model bookkeeping, not turn output — dropped.
_DROP_ITEM_TYPES = frozenset({"reasoning", "plan_update", "todo_list"})


def _stringify_detail(val) -> str:
    """Best-effort short string for a tool-detail field of unknown shape."""
    if isinstance(val, str):
        return val
    if isinstance(val, list) and val:
        first = val[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            for key in ("path", "file_path", "name"):
                inner = first.get(key)
                if isinstance(inner, str) and inner:
                    return inner
    return ""


def _tool_marker(item: dict) -> str:
    """Build a compact ``[label] <intent>`` marker from a tool-ish item."""
    itype = item.get("type") or "tool"
    if not isinstance(itype, str) or not itype.strip():
        itype = "tool"
    label, keys = _ITEM_TOOL_LABELS.get(itype, (itype, ()))

    detail = ""
    for key in keys:
        cand = _stringify_detail(item.get(key))
        if cand.strip():
            detail = cand.strip()
            break

    if detail:
        detail = " ".join(detail.split())[:_MAX_TOOL_DETAIL]
        return f"[{label}] {detail}"
    return f"[{label}]"


def _error_message(event: dict) -> str:
    """Pull the human message off an ``error`` / ``turn.failed`` event."""
    msg = event.get("message")
    if isinstance(msg, str) and msg.strip():
        return msg.strip()
    err = event.get("error")
    if isinstance(err, dict):
        inner = err.get("message")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    return ""


def classify_line(line: str | None) -> LineEvent | None:
    """Classify one codex ``exec --json`` event line.

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

    if etype == "thread.started":
        tid = event.get("thread_id")
        return LineEvent(kind="session", session_id=tid if isinstance(tid, str) else "")

    if etype == "turn.completed":
        usage = event.get("usage")
        total = 0
        if isinstance(usage, dict):
            inp = usage.get("input_tokens")
            out = usage.get("output_tokens")
            inp = inp if isinstance(inp, (int, float)) else 0
            out = out if isinstance(out, (int, float)) else 0
            total = int(inp + out)
        return LineEvent(kind="tokens", tokens_total=total if total > 0 else 0)

    if etype in ("turn.failed", "error"):
        return LineEvent(kind="error", text=_error_message(event))

    if etype == "item.completed":
        item = event.get("item")
        if not isinstance(item, dict):
            return LineEvent(kind="other")
        itype = item.get("type", "")
        if itype == "agent_message":
            txt = item.get("text", "")
            return LineEvent(kind="text", text=txt if isinstance(txt, str) else "")
        if itype in _DROP_ITEM_TYPES:
            return LineEvent(kind="other")
        # command_execution / file_change / mcp_tool_call / web_search and any
        # unknown tool item -> compact marker (verbose I/O dropped).
        return LineEvent(kind="tool", marker=_tool_marker(item))

    # turn.started, item.started, item.updated, etc. — not turn output.
    return LineEvent(kind="other")
