"""Headline + sidecar event recording for job logs.

The loop event stream used to persist every big block verbatim: full agent
prompts, full agent output, full judge verdicts, full suggestions — a single
run could produce a 10 MB ``.logs`` file where the readable signal (one key
line per event) drowned in repeated protocol text.

This module rewrites each event into a one-line *headline* before it reaches
the job log. The full body of large blocks is preserved in a per-job sidecar
file (``<job_id>.full`` JSONL) and referenced by an integer ``ref``:

    {"level": "opencode_output", "msg": "← output (8,234 chars) ... #3",
     "ref": 3, "detail_len": 8234}

Policy (``mode="headline"``, the default):

- ``error`` events are ALWAYS written in full, inline — errors must never be
  truncated or banished to a sidecar.
- Single-line levels (``info`` / ``tool`` / ``success`` / ``report`` / …)
  pass through verbatim.
- Large blocks (``opencode_prompt`` / ``opencode_output`` /
  ``supervisor_response`` / ``supervisor_suggestions``) become headline
  digests with a sidecar ref.
- ``heartbeat`` events are dropped outright; the job state file already
  carries ``heartbeat_at``.
- Repeated identical ``warn`` texts (the every-turn token warnings with
  their "Files loaded" lists) pass once, then resurface only at count
  milestones (×2, ×5, ×10, …) as a one-line marker.

``mode="full"`` restores the historical behaviour verbatim (debugging).
"""

from __future__ import annotations

import logging
from typing import Any

from services.jobs.state_store import JobStateStore

logger = logging.getLogger(__name__)

# Levels whose bodies move to the sidecar, keeping only a headline inline.
BIG_BLOCK_LEVELS = frozenset({
    "opencode_prompt",
    "opencode_output",
    "supervisor_response",
    "supervisor_suggestions",
})

# Warn texts repeat at these total counts (every turn for token warnings).
_WARN_MILESTONES = (2, 5, 10, 20, 50, 100, 200, 500, 1000)

# Cap on distinct warn texts tracked for coalescing (bounded memory).
_WARN_TRACK_LIMIT = 32

_HEAD_LINES = 2
_TAIL_LINES = 2
# Collapse head+tail into one line when the body is short enough to show
# mostly in full anyway.
_JOIN_THRESHOLD_LINES = _HEAD_LINES + _TAIL_LINES + 2
# A single pathological line (minified JSON, one huge token dump) must not
# smuggle the whole body back into the headline.
_HEADLINE_LINE_LIMIT = 200

_SUGGESTION_HEAD_CHARS = 300


def _first_nonempty_lines(text: str, count: int) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        if line.strip():
            out.append(_shorten(line, _HEADLINE_LINE_LIMIT))
            if len(out) >= count:
                break
    return out


def _last_nonempty_lines(text: str, count: int) -> list[str]:
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return [_shorten(ln, _HEADLINE_LINE_LIMIT) for ln in lines[-count:]]


def _shorten(line: str, limit: int = 120) -> str:
    line = line.strip()
    return line if len(line) <= limit else line[: limit - 1] + "…"


class EventRecorder:
    """Rewrites loop events into headline form before persistence.

    One instance per job worker; call :meth:`record` for every event and
    persist whatever it returns (``None`` = drop).
    """

    def __init__(self, store: JobStateStore, job_id: str, mode: str = "headline"):
        self.store = store
        self.job_id = job_id
        self.mode = "full" if str(mode).strip().lower() == "full" else "headline"
        # warn text -> {"count": int, "announced": int} for milestone markers
        self._warn_counts: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------ API

    def record(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Return the event to append to the main log, or None to drop it."""
        if not isinstance(event, dict):
            return event
        if self.mode == "full":
            return event

        level = event.get("level") or "info"
        if level == "heartbeat":
            return None
        if level == "error":
            # Errors keep their full body inline, always.
            return {k: v for k, v in event.items() if k not in ("ref", "detail_len")}
        if level == "warn":
            return self._record_warn(event)

        if level in BIG_BLOCK_LEVELS:
            return self._record_big_block(event)
        return event

    # ---------------------------------------------------------------- warns

    def _record_warn(self, event: dict[str, Any]) -> dict[str, Any] | None:
        msg = event.get("msg") or ""
        # Key on the FIRST LINE, not the whole text: token warnings repeat
        # every turn with a churned "Files loaded: …" list appended — the
        # stable headline identifies the repeat, the churned detail does not.
        key = msg.splitlines()[0] if msg.splitlines() else msg
        entry = self._warn_counts.get(key)
        if entry is None:
            if len(self._warn_counts) >= _WARN_TRACK_LIMIT:
                # Drop the oldest tracked text; if it repeats later it just
                # starts a fresh count instead of being coalesced.
                self._warn_counts.pop(next(iter(self._warn_counts)))
            self._warn_counts[key] = {"count": 1, "announced": 1}
            return event

        entry["count"] += 1
        if entry["count"] <= entry["announced"]:
            return None
        milestone = next(
            (m for m in _WARN_MILESTONES if entry["count"] >= m > entry["announced"]),
            None,
        )
        if milestone is None:
            return None
        entry["announced"] = milestone
        return {
            "level": "warn",
            "msg": f"⚠ repeated warning ×{milestone}: {_shorten(key)}",
        }

    # ------------------------------------------------------------ big blocks

    def _record_big_block(self, event: dict[str, Any]) -> dict[str, Any]:
        msg = event.get("msg")
        if not isinstance(msg, str) or not msg.strip():
            return event

        ref = self.store.append_full(self.job_id, {
            "level": event.get("level"),
            "body": msg,
        })
        level = event.get("level")
        if level == "opencode_prompt":
            headline = self._headline_prompt(event, msg)
        elif level == "opencode_output":
            headline = self._headline_output(msg)
        elif level == "supervisor_response":
            headline = self._headline_verdict(event, msg)
        else:  # supervisor_suggestions
            headline = self._headline_suggestions(msg)

        out = {k: v for k, v in event.items() if k not in ("msg", "prompt_kind", "digest")}
        out["msg"] = f"{headline} — full text in event #{ref}"
        out["ref"] = ref
        out["detail_len"] = len(msg)
        return out

    def _headline_prompt(self, event: dict[str, Any], msg: str) -> str:
        kind = event.get("prompt_kind") or "prompt"
        return f"→ {kind} prompt ({len(msg):,} chars)"

    def _headline_output(self, msg: str) -> str:
        total = len(msg.splitlines())
        head = _first_nonempty_lines(msg, _HEAD_LINES)
        tail = _last_nonempty_lines(msg, _TAIL_LINES)
        if total <= _JOIN_THRESHOLD_LINES:
            body = " ⏎ ".join(head + [ln for ln in tail if ln not in head])
        else:
            gap = " … " if head and tail else ""
            body = " ⏎ ".join(head) + gap + " ⏎ ".join(tail)
        return f"← output ({len(msg):,} chars, {total} lines): {body}"

    def _headline_verdict(self, event: dict[str, Any], msg: str) -> str:
        digest = event.get("digest")
        if isinstance(digest, str) and digest.strip():
            return digest.strip()
        head = _first_nonempty_lines(msg, _HEAD_LINES)
        tail = _last_nonempty_lines(msg, _TAIL_LINES)
        return "⚖ verdict: " + " ⏎ ".join(head + [ln for ln in tail if ln not in head])

    def _headline_suggestions(self, msg: str) -> str:
        first = ""
        for line in msg.splitlines():
            if line.strip():
                first = _shorten(line, _SUGGESTION_HEAD_CHARS)
                break
        return f"💡 suggestions ({len(msg):,} chars): {first}"
