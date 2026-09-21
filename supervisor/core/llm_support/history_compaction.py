"""Tiered, content-aware view of the supervisor's conversation history.

The supervisor's ``_history`` is a flat ``[{role, content}, ...]`` list where
every judged turn appends a **user** message embedding the agent's full output
and an **assistant** message holding the verdict. Once a turn has been judged,
the *verdict* is the durable signal and the agent-output narration is bulk:
it was already evaluated and only matters for continuity with the most recent
turns.

This module builds the request-time view of that history — it never mutates
``_history`` (the stored list stays the source of truth for rollup and
debugging). Turns are classified by value, not just position:

  * Tier A (verbatim) — the most recent few turns, kept intact so the judge
    keeps continuity with the immediate past. A single oversized message is
    head-tail excerpted only past a generous cap, so one huge output cannot
    eat the whole budget.
  * Tier B (digest) — older turns: the verdict is condensed to its
    CRITERIA / NEXT_ACTION / DONE block (the reusable decision record) and the
    agent output to a head-tail excerpt (conclusions live at the end).
  * Tier C (synopsis) — turns that no longer fit the token budget collapse
    into one criteria-only bullet list with an omission marker.

The walk is budget-driven and proactive: history is bounded on every judge
call instead of only after the request overflows (``fit_request_to_budget``
and the token-limit retry paths remain as safety nets).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from supervisor.core.llm_support.models import parse_verdict_structure
from supervisor.monitoring.token_estimator import estimate_tokens
from supervisor.utils.text_utils import head_tail_excerpt

logger = logging.getLogger(__name__)

__all__ = [
    "HistoryTurn",
    "build_context_messages",
    "extract_output",
    "output_digest",
    "pair_turns",
    "verdict_digest",
]

# Markers delimiting the agent output inside a stored judge prompt. The
# history copy keeps these (only the context blocks are stripped), so the
# output can be sliced out without the surrounding prompt boilerplate.
_OUTPUT_MARKERS = ("--- opencode output ---", "--- opencode plan output ---")
_OUTPUT_END_MARKER = "\n--- end ---"

# Size guards. Verbatim tier messages are only excerpted past this cap (chars);
# digests and the synopsis are bounded tighter — they are breadcrumbs, not
# transcripts.
_VERBATIM_CHAR_CAP = 30_000
_DIGEST_CHAR_CAP = 1_200
_OUTPUT_DIGEST_CHAR_CAP = 1_600
_SYNOPSIS_CHAR_CAP = 2_000
_UNSTRUCTURED_DIGEST_CAP = 600

# Adaptive-shrink floor for the verbatim cap when the recent turns alone would
# exceed the whole history budget (char counts, halved per round).
_VERBATIM_CHAR_FLOOR = 4_000


@dataclass(frozen=True)
class HistoryTurn:
    """One judged turn: the judge prompt (user) and its verdict (assistant).

    Either side may be empty — an unpaired message (e.g. an intermediate
    assistant-only turn) still becomes a turn so nothing is silently dropped.
    """

    index: int
    user_text: str
    assistant_text: str


def pair_turns(history: list[dict]) -> list[HistoryTurn]:
    """Fold the flat message list into ``(user, assistant)`` turns.

    Walks the list expecting a ``user`` followed by an ``assistant``; a lone
    ``assistant`` starts a turn with no user side, and a trailing lone ``user``
    closes with no assistant. ``index`` is the 1-based turn number.
    """
    turns: list[HistoryTurn] = []
    i = 0
    n = len(history)
    while i < n:
        role = history[i].get("role", "")
        user_text = ""
        assistant_text = ""
        if role == "user":
            user_text = history[i].get("content", "") or ""
            if i + 1 < n and history[i + 1].get("role") == "assistant":
                assistant_text = history[i + 1].get("content", "") or ""
                i += 2
            else:
                i += 1
        else:
            assistant_text = history[i].get("content", "") or ""
            i += 1
        turns.append(
            HistoryTurn(
                index=len(turns) + 1,
                user_text=user_text,
                assistant_text=assistant_text,
            ),
        )
    return turns


def extract_output(user_text: str) -> str:
    """Slice the agent output out of a stored judge prompt.

    Returns the whole text when no marker is present (e.g. a compaction
    instruction that never carried an output block) so the caller can still
    compress something meaningful.
    """
    for marker in _OUTPUT_MARKERS:
        start = user_text.find(marker)
        if start < 0:
            continue
        output_start = start + len(marker)
        end = user_text.find(_OUTPUT_END_MARKER, output_start)
        if end >= 0:
            return user_text[output_start:end].strip()
        return user_text[output_start:].strip()
    return user_text.strip()


def output_digest(user_text: str, *, max_chars: int = _OUTPUT_DIGEST_CHAR_CAP) -> str:
    """Compress one turn's agent output to a head-tail excerpt.

    Agent output states the plan up front and the result at the end, so a
    head-only cut would discard exactly the valuable half.
    """
    return head_tail_excerpt(extract_output(user_text), max_chars)


def verdict_digest(
    assistant_text: str,
    *,
    turn_index: int | None = None,
    max_chars: int = _DIGEST_CHAR_CAP,
) -> str:
    """Condense a verdict reply to its decision record.

    Reuses the structured CRITERIA/NEXT_ACTION/DONE parser the loop already
    runs on every verdict, so the digest is exactly what the supervisor
    decided — not a position-based slice of the raw text. Falls back to a
    head-tail excerpt when the reply carried no structured block.
    """
    done, criteria, next_action, _evidence = parse_verdict_structure(
        assistant_text or "",
    )
    has_structure = done is not None or criteria or next_action
    if not has_structure:
        excerpt = head_tail_excerpt(
            (assistant_text or "").strip(),
            _UNSTRUCTURED_DIGEST_CAP,
        )
        if turn_index is not None:
            return f"[turn {turn_index} — verdict, unstructured]\n{excerpt}"
        return excerpt

    label = f"[turn {turn_index} | " if turn_index is not None else "["
    done_str = "yes" if done else "no" if done is not None else "?"
    lines = [f"{label}DONE: {done_str}]"]
    if next_action:
        lines.append(f"NEXT_ACTION: {' '.join(next_action.split())}")
    for criterion in criteria:
        state = "MET" if criterion.met else "UNMET"
        name = " ".join((criterion.criterion or "").split())
        evidence = " ".join((criterion.evidence or "").split())
        if evidence:
            lines.append(f"- [{state}] {name} — {evidence}")
        else:
            lines.append(f"- [{state}] {name}")
    return head_tail_excerpt("\n".join(lines), max_chars)


def _cost(messages: list[dict]) -> int:
    return sum(estimate_tokens(m.get("content", "")) for m in messages)


def _bounded(text: str, cap: int) -> str:
    return text if len(text) <= cap else head_tail_excerpt(text, cap)


def build_context_messages(
    history: list[dict],
    *,
    max_tokens: int,
    verbatim_turns: int = 4,
    budget_fraction: float = 0.35,
) -> list[dict]:
    """Render ``history`` into the message list sent to the judge.

    Non-destructive: returns a new list; ``history`` is never mutated. The
    view is ordered oldest → newest (conversation order), with the oldest
    material most compressed. The current turn is appended by the caller and
    is never compressed here.

    Args:
        history: the supervisor's ``_history`` (flat role/content list).
        max_tokens: supervisor token ceiling; the history budget is derived
            from it so history can never starve the system prompt, the
            context blocks, the current output, or the response.
        verbatim_turns: how many recent turns stay intact (Tier A).
        budget_fraction: max share of ``max_tokens`` spent on Tier A + B.
    """
    if not history:
        return []

    turns = pair_turns(history)
    if not turns:
        return []

    verbatim_turns = max(0, min(verbatim_turns, len(turns)))
    if verbatim_turns == 0:
        recent: list[HistoryTurn] = []
        older = list(turns)
    else:
        recent = turns[-verbatim_turns:]
        older = turns[:-verbatim_turns] or []

    budget_tokens = max(0, int(max_tokens * budget_fraction))
    if budget_tokens <= 0:
        budget_tokens = 1

    # Tier A: recent turns verbatim. If they alone would blow the budget,
    # halve the per-message cap until they fit (floor keeps real continuity).
    cap = _VERBATIM_CHAR_CAP
    while True:
        recent_msgs: list[dict] = []
        for turn in recent:
            recent_msgs.append(
                {"role": "user", "content": _bounded(turn.user_text, cap)},
            )
            if turn.assistant_text:
                recent_msgs.append(
                    {
                        "role": "assistant",
                        "content": _bounded(turn.assistant_text, cap),
                    },
                )
        if _cost(recent_msgs) <= budget_tokens or cap <= _VERBATIM_CHAR_FLOOR:
            break
        cap = max(_VERBATIM_CHAR_FLOOR, cap // 2)

    remaining_budget = max(0, budget_tokens - _cost(recent_msgs))

    # Tier B: older turns newest → oldest, each as a digest pair. Walk the
    # newest first so the budget is spent on the turns closest to the present.
    digest_msgs: list[dict] = []
    emitted_indices: set[int] = set()
    spent = 0
    for turn in reversed(older):
        user_part = output_digest(turn.user_text)
        assistant_part = verdict_digest(
            turn.assistant_text,
            turn_index=turn.index,
        )
        if turn.assistant_text:
            candidate = [
                {
                    "role": "user",
                    "content": f"[earlier turn {turn.index} — output excerpt]\n"
                    + user_part,
                },
                {"role": "assistant", "content": assistant_part},
            ]
        else:
            candidate = [
                {
                    "role": "user",
                    "content": f"[earlier turn {turn.index} — no verdict]\n"
                    + user_part,
                },
            ]
        cost = _cost(candidate)
        if digest_msgs and spent + cost > remaining_budget:
            break
        digest_msgs = candidate + digest_msgs
        emitted_indices.add(turn.index)
        spent += cost

    # Turns the budget walk never reached collapse into the Tier C synopsis.
    synopsis_turns = [t for t in older if t.index not in emitted_indices]

    messages: list[dict] = []
    if synopsis_turns:
        bullets = []
        for turn in synopsis_turns:
            digest = verdict_digest(
                turn.assistant_text,
                turn_index=turn.index,
                max_chars=max(
                    200,
                    _DIGEST_CHAR_CAP // max(1, len(synopsis_turns)),
                ),
            )
            excerpt = head_tail_excerpt(
                extract_output(turn.user_text),
                240,
                end_ratio=0.6,
            )
            line = digest
            if excerpt:
                line = f"{digest}\n  output: {excerpt}"
            bullets.append(line)
        synopsis = (
            f"[... {len(synopsis_turns)} earlier turn(s) compressed to "
            "verdicts only ...]\n" + "\n".join(bullets)
        )
        messages.append(
            {
                "role": "user",
                "content": head_tail_excerpt(synopsis, _SYNOPSIS_CHAR_CAP),
            },
        )

    messages.extend(digest_msgs)
    messages.extend(recent_msgs)

    return _merge_adjacent_same_role(messages)


def _merge_adjacent_same_role(messages: list[dict]) -> list[dict]:
    """Collapse neighbouring same-role messages into one.

    Turns without a verdict side emit a user-only message; two in a row would
    hand the chat endpoint an ill-formed view. Merging keeps every role change
    legal without dropping content.
    """
    merged: list[dict] = []
    for msg in messages:
        if merged and merged[-1].get("role") == msg.get("role"):
            merged[-1] = {
                "role": msg["role"],
                "content": (merged[-1].get("content", "") or "")
                + "\n\n"
                + (msg.get("content", "") or ""),
            }
        else:
            merged.append(dict(msg))
    return merged
