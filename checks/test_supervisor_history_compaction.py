"""Tests for the tiered supervisor-history view (llm_support.history_compaction)."""

from supervisor.core.llm_support.history_compaction import (
    build_context_messages,
    extract_output,
    output_digest,
    pair_turns,
    verdict_digest,
)

_VERDICT = """\
The implementation is incomplete.

CRITERIA:
- [MET] Feature A — test_a passes (pytest exit 0)
- [UNMET] Feature B — no test_b found in tests/
NEXT_ACTION: Add tests/test_b.py covering the empty-input branch.
DONE: no
"""

_JUDGE_PROMPT = """\
The coding agent just produced the following output. Evaluate it against the protocol.

--- Step Context ---
Current step: 3/10
Current phase: implementation

--- opencode output ---
I implemented feature A and ran the tests.

Plan: add module a.py.
Result: test_a passes, test_b still missing.
--- end ---

Focus your feedback on the current phase and remaining work."""


def _turns(n: int, *, output: str = "agent output", verdict: str = _VERDICT):
    history = []
    for i in range(n):
        history.append(
            {
                "role": "user",
                "content": f"--- opencode output ---\n[{i}] {output}\n--- end ---",
            },
        )
        history.append({"role": "assistant", "content": verdict})
    return history


def test_pair_turns_pairs_user_and_assistant():
    turns = pair_turns(_turns(3))
    assert len(turns) == 3
    assert [t.index for t in turns] == [1, 2, 3]
    assert "[0] agent output" in turns[0].user_text
    assert "NEXT_ACTION:" in turns[0].assistant_text


def test_pair_turns_tolerates_odd_lengths():
    # Assistant-only leading turn (intermediate, unrecorded user side).
    turns = pair_turns(
        [{"role": "assistant", "content": "solo verdict"}],
    )
    assert turns == [] or (len(turns) == 1 and turns[0].user_text == "")
    assert pair_turns([]) == []


def test_pair_turns_handles_trailing_lone_user():
    turns = pair_turns(
        [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "orphan"},
        ],
    )
    assert len(turns) == 2
    assert turns[1].user_text == "orphan"
    assert turns[1].assistant_text == ""


def test_verdict_digest_keeps_criteria_and_next_action():
    digest = verdict_digest(_VERDICT, turn_index=5)
    assert "[turn 5 | DONE: no]" in digest
    assert "NEXT_ACTION: Add tests/test_b.py" in digest
    assert "[MET] Feature A" in digest
    assert "[UNMET] Feature B" in digest
    # The verbose preamble ("The implementation is incomplete.") is dropped.
    assert "The implementation is incomplete" not in digest


def test_verdict_digest_marks_done_yes():
    digest = verdict_digest(_VERDICT.replace("DONE: no", "DONE: yes"))
    assert "DONE: yes" in digest


def test_verdict_digest_falls_back_for_unstructured_replies():
    plain = "Just some prose with no structured block at all."
    digest = verdict_digest(plain)
    assert digest == plain  # nothing to extract -> excerpt of the raw reply
    labeled = verdict_digest(plain, turn_index=2)
    assert "[turn 2 — verdict, unstructured]" in labeled
    assert "Just some prose" in labeled


def test_extract_output_slices_between_markers():
    out = extract_output(_JUDGE_PROMPT)
    assert out.startswith("I implemented feature A")
    assert out.endswith("test_b still missing.")
    # Step-context boilerplate and verdict format are excluded.
    assert "Current step" not in out


def test_extract_output_returns_whole_text_without_markers():
    assert extract_output("no markers here") == "no markers here"


def test_output_digest_head_tail_excerpt():
    long_output = "HEAD " * 4000 + "TAIL_CONCLUSION " * 10
    digest = output_digest(f"--- opencode output ---\n{long_output}\n--- end ---")
    assert "TAIL_CONCLUSION" in digest
    assert "omitted" in digest


def test_build_context_messages_never_mutates_history():
    history = _turns(6)
    before = [dict(m) for m in history]
    build_context_messages(history, max_tokens=150_000, verbatim_turns=2)
    assert history == before


def test_build_context_messages_keeps_recent_turns_verbatim():
    history = _turns(5)
    messages = build_context_messages(
        history,
        max_tokens=150_000,  # roomy budget: older turns emit as digests
        verbatim_turns=2,
    )
    # 2 verbatim pairs + 3 digest pairs = 10 messages, strictly alternating.
    assert len(messages) == 10
    assert [m["role"] for m in messages] == ["user", "assistant"] * 5
    # The two most recent turns are verbatim: full output, no digest prefix.
    recent_users = [m for m in messages if m["role"] == "user"][-2:]
    assert all("earlier turn" not in m["content"] for m in recent_users)
    assert "[3] agent output" in recent_users[0]["content"]
    assert "[4] agent output" in recent_users[1]["content"]
    # Older turns are digests.
    assert "earlier turn" in messages[0]["content"]
    assert "NEXT_ACTION:" in messages[1]["content"]


def test_build_context_messages_compresses_older_turns():
    history = _turns(6)
    messages = build_context_messages(
        history,
        max_tokens=150_000,
        verbatim_turns=2,
    )
    # 2 verbatim + 2 digest pairs + 2 synopsis... older turns 1-2 get digests.
    digest_users = [m for m in messages if "earlier turn" in m["content"]]
    assert len(digest_users) >= 1
    assert "NEXT_ACTION:" in messages[1]["content"]


def test_build_context_messages_respects_budget():
    big = "x " * 20_000  # ~10k tokens per output
    history = _turns(12, output=big)
    messages = build_context_messages(
        history,
        max_tokens=100_000,
        verbatim_turns=4,
        budget_fraction=0.2,  # 20k token budget for history
    )
    from supervisor.monitoring.token_estimator import estimate_tokens

    total = sum(estimate_tokens(m["content"]) for m in messages)
    # Budget applies to tiers A+B; the synopsis is a small override, so allow
    # a generous margin over the raw budget but it must be far below the
    # un-compressed 12-turn total (~120k tokens).
    assert total < 40_000
    assert any("compressed to verdicts only" in m["content"] for m in messages)


def test_build_context_messages_emits_synopsis_for_omitted_turns():
    history = _turns(20, output="y " * 5000)
    messages = build_context_messages(
        history,
        max_tokens=50_000,
        verbatim_turns=2,
        budget_fraction=0.1,
    )
    synopsis = [m for m in messages if "compressed to verdicts only" in m["content"]]
    assert len(synopsis) == 1
    assert synopsis[0]["role"] == "user"
    # The synopsis sits first (oldest material, most compressed).
    assert messages[0] is synopsis[0]


def test_build_context_messages_empty_history():
    assert build_context_messages([], max_tokens=100_000) == []


def test_build_context_messages_verbatim_zero_compresses_all():
    history = _turns(3)
    messages = build_context_messages(
        history,
        max_tokens=150_000,
        verbatim_turns=0,
    )
    assert all("earlier turn" in m["content"] or "compressed" in m["content"]
               for m in messages if m["role"] == "user")
    assert len(messages) <= 6


def test_build_context_messages_giant_output_does_not_eat_budget():
    giant = "z" * 400_000  # ~100k tokens in a single recent turn
    history = _turns(3, output=giant)
    messages = build_context_messages(
        history,
        max_tokens=100_000,
        verbatim_turns=2,
        budget_fraction=0.35,
    )
    from supervisor.monitoring.token_estimator import estimate_tokens

    total = sum(estimate_tokens(m["content"]) for m in messages)
    assert total < 60_000  # capped rather than passed through verbatim
    # Recent turn is still recognizable, just excerpted.
    assert any("z" in m["content"] for m in messages)


def test_build_context_messages_alternation_is_valid():
    history = _turns(9, output="a " * 3000)
    messages = build_context_messages(
        history,
        max_tokens=60_000,
        verbatim_turns=3,
        budget_fraction=0.3,
    )
    roles = [m["role"] for m in messages]
    # user/assistant must strictly alternate so the chat view stays well-formed.
    for prev, cur in zip(roles, roles[1:]):
        assert prev != cur


class _FakeSupervisor:
    """Minimal stand-in exposing only what rollup_history touches."""

    def __init__(self, history):
        self._history = history


def test_rollup_history_keeps_verdict_signal():
    from supervisor.core.llm_support.history import rollup_history

    history = _turns(10)
    sup = _FakeSupervisor(list(history))
    condensed = rollup_history(sup, keep_recent=6)

    assert condensed == len(history) - 1 - 6
    # head anchor + synopsis + recent tail.
    assert len(sup._history) == 8
    synopsis = sup._history[1]["content"]
    # The decision record survives; the bulky prompt preamble does not.
    assert "NEXT_ACTION: Add tests/test_b.py" in synopsis
    assert "DONE: no" in synopsis
    assert "[UNMET] Feature B" in synopsis
    assert "Evaluate it against the protocol" not in synopsis


def test_rollup_history_noop_on_short_history():
    from supervisor.core.llm_support.history import rollup_history

    sup = _FakeSupervisor(_turns(3))
    assert rollup_history(sup) == 0
    assert len(sup._history) == 6


def test_build_context_messages_merges_unpaired_same_role():
    # Verdict-less turns emit user-only messages; consecutive ones must merge.
    history = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "solo verdict"},  # pairs with u1
        {"role": "user", "content": "u2"},                  # unpaired
        {"role": "user", "content": "u3"},                  # unpaired
        {"role": "assistant", "content": _VERDICT},
    ]
    messages = build_context_messages(history, max_tokens=150_000, verbatim_turns=1)
    roles = [m["role"] for m in messages]
    for prev, cur in zip(roles, roles[1:]):
        assert prev != cur
