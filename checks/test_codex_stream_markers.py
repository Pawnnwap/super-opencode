"""Loop-detector keying: markers must carry tool CONTENT, not just the name.

academic_study_1012_1..4 all aborted as "stuck in a tool loop: repeated the
same tool call ([mcp] web_search|web_fetch)" — the old codex marker dropped
the query/URL, so consecutive web calls on DIFFERENT content looked identical
to the loop detector. Distinct content must yield distinct markers; only
same-content repeats may fire.
"""

import json

from supervisor.analyzers.loop_detector import LoopDetector
from supervisor.runners.codex_support.stream import classify_line


def _marker_for(item):
    result = classify_line(json.dumps({"type": "item.completed", "item": item}))
    assert result.kind == "tool", result
    return result.marker


def _search_item(tool, payload):
    return {"type": "mcp_tool_call", "server": "web_tools", "tool": tool, "input": payload}


def test_different_queries_and_urls_yield_different_markers():
    a = _marker_for(_search_item("web_search", {"query": "taker maker order size asymmetry"}))
    b = _marker_for(_search_item("web_search", {"query": "deep order flow imbalance"}))
    c = _marker_for(_search_item("web_fetch", {"url": "https://arxiv.org/abs/1234.5678"}))
    d = _marker_for(_search_item("web_fetch", {"url": "https://www.science.org/doi/10.1/x"}))
    assert a != b, a
    assert c != d, c
    assert "taker maker order size" in a
    assert "arxiv" in c
    assert a.startswith("[mcp]")


def test_nested_arguments_payload_supported():
    item = {"type": "mcp_tool_call", "server": "web_tools", "tool": "web_search",
            "input": {"name": "mcp__web_tools__web_search",
                      "arguments": {"query": "intraday momentum cross section"}}}
    marker = _marker_for(item)
    assert "intraday momentum cross section" in marker


def test_consecutive_different_content_is_NOT_a_loop():
    detector = LoopDetector()
    queries = [
        f"literature search query number {i} about market microstructure"
        for i in range(12)
    ]
    for q in queries:
        signal = detector.record_tool(_marker_for(_search_item("web_search", {"query": q})))
        assert signal is None, f"false loop: {signal}"


def test_same_content_repeats_are_STILL_detected():
    detector = LoopDetector()
    marker = _marker_for(_search_item("web_search", {"query": "same query repeated"}))
    assert marker != "[mcp] web_search"
    signals = [detector.record_tool(marker) for _ in range(6)]
    assert any(s is not None for s in signals)


def test_marker_without_payload_falls_back_to_name():
    marker = _marker_for({"type": "mcp_tool_call", "tool": "web_fetch"})
    assert marker == "[mcp] web_fetch"


def test_silent_high_variety_exploration_is_NOT_a_loop():
    """The measured misfire: 25 DISTINCT read-only survey calls with no
    intermediate prose must not trip the silent-streak rule."""
    detector = LoopDetector()
    for i in range(30):
        signal = detector.record_tool(f"[shell] get-childitem survey command number {i}")
        assert signal is None, f"false loop at tool {i}: {signal}"


def test_alternating_test_and_edit_is_NOT_a_loop():
    """Rerunning the suite across distinct edits is the protected case: 50%
    novelty must survive even at twice the silent-streak threshold."""
    detector = LoopDetector()
    for i in range(13):
        for marker in ("[shell] run the test suite", f"[edit] src/module_{i}.py"):
            signal = detector.record_tool(marker)
            assert signal is None, f"false loop at edit {i}: {signal}"


def test_silent_low_variety_thrash_is_STILL_detected():
    """A silent streak that keeps reusing a small command set is the real
    thrash the rule exists for — no clean cycle, but heavy repetition."""
    detector = LoopDetector()
    keys = [f"[shell] thrash command {i}" for i in range(10)]
    signals = [detector.record_tool(k) for k in keys * 2 + keys[:5]]
    fired = [s for s in signals if s is not None]
    assert fired, "low-variety silent thrash not detected"
    assert "without producing any output" in fired[0].reason


def test_engine_error_items_do_not_feed_the_loop_detector():
    result = classify_line(json.dumps({
        "type": "item.completed",
        "item": {"type": "error", "message": "Model metadata for `swift-1.5` not found."},
    }))
    assert result.kind == "tool"
    assert "Model metadata" in result.marker
    assert result.count_for_loop is False
    ok = classify_line(json.dumps({
        "type": "item.completed",
        "item": {"type": "command_execution", "command": "ls"},
    }))
    assert ok.count_for_loop is True
