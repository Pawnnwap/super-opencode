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
