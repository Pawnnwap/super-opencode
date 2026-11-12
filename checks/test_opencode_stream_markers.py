"""opencode 2.x stream: the `error` event must reach the judge as text.

run_70c94806 closed BLOCKED with every gate UNMET because the supervisor's
per-turn output was a bare ``[exit 1]``. opencode 2.x reports a turn that never
reached the model as ONE ``error`` JSON event and exits 1; the parser classified
it as "other" and dropped it, and a JSON-parseable line never reaches the raw
fallback — so ``build_output`` returned "" and ``RunResult.output`` collapsed to
the exit marker alone. The judge read "no evidence" as "target unmet".

The event lines below are captured live from opencode v2.2.7.
"""

import json

from supervisor.runners.opencode_support.result import RunResult
from supervisor.runners.opencode_support.stream import build_output, classify_line

# Captured: `opencode run --format json --model anthropic/gpt-does-not-exist-99`
NO_ROUTE = {
    "type": "error",
    "timestamp": 1794476072564,
    "sessionID": "ses_e30e42513ffeqNyLrAUSDpUZIB",
    "error": {
        "type": "provider.no-route",
        "message": "Model unavailable: anthropic/gpt-does-not-exist-99",
    },
}


def _output_for(*events, returncode=1):
    """Fold *events* the way stream_driver does, then surface RunResult.output."""
    text_parts, markers, raw_parts = [], [], []
    saw_json = False
    for ev in events:
        event = classify_line(json.dumps(ev))
        kind = getattr(event, "kind", None)
        if kind in ("text", "error", "tool", "tokens", "session", "other", "thought"):
            saw_json = True
        if kind in ("text", "error"):
            # The driver folds error text into text_parts, never prose_parts.
            text_parts.append(event.text)
        elif kind == "tool":
            markers.append(event.marker)
        elif kind == "raw":
            raw_parts.append(event.raw)
    stdout = build_output(
        text_parts=text_parts,
        markers=markers,
        saw_json=saw_json,
        raw_parts=raw_parts,
    )
    return RunResult(stdout=stdout, stderr="", returncode=returncode).output


def test_provider_no_route_is_an_error_event():
    event = classify_line(json.dumps(NO_ROUTE))
    assert event.kind == "error", event
    assert "Model unavailable" in event.text
    assert "gpt-does-not-exist-99" in event.text


def test_error_only_turn_surfaces_the_message_not_a_bare_exit():
    """The regression: the judge must read WHY the turn died."""
    out = _output_for(NO_ROUTE)
    # The `message` field wins over `type` — it names the model to fix.
    assert "Model unavailable" in out
    assert "gpt-does-not-exist-99" in out
    assert out != "[exit 1]"


def test_error_message_shapes():
    for event, expected in [
        ({"type": "error", "message": "auth failed"}, "auth failed"),
        ({"type": "error", "error": "plain string"}, "plain string"),
        (
            {"type": "error", "error": {"message": "nested message"}},
            "nested message",
        ),
        # No message at all: the `type` is the only signal left, and it beats
        # silence.
        ({"type": "error", "error": {"type": "provider.auth"}}, "provider.auth"),
        (
            {"type": "error", "properties": {"message": "from properties"}},
            "from properties",
        ),
    ]:
        assert classify_line(json.dumps(event)).text == expected, event


def test_error_event_without_any_text_does_not_crash():
    event = classify_line(json.dumps({"type": "error"}))
    assert event.kind == "error"
    assert event.text == ""


def test_turn_scoring_prose_keeps_model_text_only():
    """An error line is turn output, never "the agent answered".

    ``prose`` is what callers use as the "model produced text" criterion; the
    error text must land in stdout (``text_parts``) and NOT in prose, so a dead
    provider is never mistaken for a completed turn.
    """
    from supervisor.runners.stream_driver import StreamOutcome  # noqa: F401

    text_ev = classify_line(json.dumps({"type": "text", "part": {"text": "done"}}))
    err_ev = classify_line(json.dumps(NO_ROUTE))
    assert text_ev.kind == "text" and text_ev.text == "done"
    assert err_ev.kind == "error"


def test_bookkeeping_events_still_ignored():
    for ev in (
        {"type": "step_start", "part": {"type": "step-start"}},
        {"type": "session_id", "sessionID": "ses_1"},
        {"type": "message_part", "part": {"type": "reasoning", "text": "..."}},
    ):
        assert classify_line(json.dumps(ev)).kind == "other", ev


def test_step_finish_tokens_untouched_by_the_error_branch():
    event = classify_line(
        json.dumps(
            {
                "type": "step_finish",
                "part": {"type": "step-finish", "tokens": {"input": 100, "output": 5}},
            },
        ),
    )
    assert event.kind == "tokens"
    assert event.tokens_total == 105


def test_healthy_turn_output_unchanged():
    out = _output_for(
        {"type": "step_start", "part": {"type": "step-start"}},
        {"type": "text", "part": {"type": "text", "text": "I wrote the file."}},
        {"type": "tool_use", "part": {"type": "tool", "tool": "write"}},
        {"type": "step_finish", "part": {"tokens": {"total": 120}}},
        returncode=0,
    )
    assert out == "I wrote the file."
