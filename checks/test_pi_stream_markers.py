"""Pi stream parsing: prose, tool markers, tokens, session id — and the
role filter that keeps the SYSTEM prompt out of the supervisor's context.

pi's `message_end` fires for every message (system, user, assistant,
tool-result echoes); only the assistant role may become turn output. Tool
markers must carry tool CONTENT (the loop detector keys on them), mirroring
checks/test_codex_stream_markers.py.
"""

import json

from supervisor.analyzers.loop_detector import LoopDetector
from supervisor.runners.pi_support.stream import classify_line


def _line(obj) -> str:
    return json.dumps(obj)


def test_session_event_yields_session_id():
    event = classify_line(
        _line({"type": "session", "version": 3, "id": "01a19a52-8576-77c9", "cwd": "X"})
    )
    assert event.kind == "session"
    assert event.session_id == "01a19a52-8576-77c9"


def test_assistant_message_end_yields_text():
    event = classify_line(
        _line({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "internal"},
                    {"type": "text", "text": "Created "},
                    {"type": "text", "text": "probe.txt"},
                ],
            },
        })
    )
    assert event.kind == "text"
    assert event.text == "Created probe.txt"


def test_system_and_user_message_ends_are_dropped():
    system = classify_line(
        _line({
            "type": "message_end",
            "message": {"role": "system", "content": "", "sections": {"preamble": "You are"}},
        })
    )
    user = classify_line(
        _line({
            "type": "message_end",
            "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        })
    )
    assert system.kind == "other"
    assert user.kind == "other"


def test_toolcall_end_yields_content_marker():
    event = classify_line(
        _line({
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "toolcall_end",
                "contentIndex": 1,
                "toolCall": {
                    "type": "toolCall",
                    "name": "bash",
                    "arguments": {"command": "ls -l probe.txt"},
                },
            },
        })
    )
    assert event.kind == "tool"
    assert event.marker == "[bash] ls -l probe.txt"


def test_streaming_deltas_are_dropped():
    for update_type in ("thinking_start", "thinking_delta", "text_delta",
                        "toolcall_start", "toolcall_delta"):
        event = classify_line(
            _line({
                "type": "message_update",
                "assistantMessageEvent": {"type": update_type, "delta": "x"},
            })
        )
        assert event.kind == "other", update_type


def test_turn_end_yields_usage_tokens():
    event = classify_line(
        _line({
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "content": [],
                "usage": {"input": 3947, "output": 130, "cacheRead": 8192,
                          "cacheWrite": 0, "reasoning": 46, "totalTokens": 12269},
            },
        })
    )
    assert event.kind == "tokens"
    assert event.tokens_total == 12269


def test_non_json_line_falls_back_to_raw():
    event = classify_line("Error: something went wrong")
    assert event.kind == "raw"
    assert event.raw == "Error: something went wrong"


def _marker_for(tool_call):
    result = classify_line(
        _line({
            "type": "message_update",
            "assistantMessageEvent": {"type": "toolcall_end", "toolCall": tool_call},
        })
    )
    assert result.kind == "tool", result
    return result.marker


def test_different_content_yields_different_markers():
    a = _marker_for({"type": "toolCall", "name": "read",
                     "arguments": {"path": "src/a.py"}})
    b = _marker_for({"type": "toolCall", "name": "read",
                     "arguments": {"path": "src/b.py"}})
    c = _marker_for({"type": "toolCall", "name": "bash",
                     "arguments": {"command": "pytest -q checks/test_one.py"}})
    d = _marker_for({"type": "toolCall", "name": "bash",
                     "arguments": {"command": "pytest -q checks/test_two.py"}})
    assert a != b and c != d
    assert "src/a.py" in a and "test_one.py" in c


def test_consecutive_different_content_is_NOT_a_loop():
    detector = LoopDetector()
    for i in range(12):
        marker = _marker_for({
            "type": "toolCall", "name": "bash",
            "arguments": {"command": f"grep -rn pattern_{i} src/"},
        })
        signal = detector.record_tool(marker)
        assert signal is None, f"false loop: {signal}"


def test_same_content_repeats_are_STILL_detected():
    detector = LoopDetector()
    marker = _marker_for({"type": "toolCall", "name": "write",
                          "arguments": {"path": "out.txt", "content": "same"}})
    signals = [detector.record_tool(marker) for _ in range(6)]
    assert any(s is not None for s in signals)


def test_marker_without_payload_falls_back_to_name():
    assert _marker_for({"type": "toolCall", "name": "bash"}) == "[bash]"
