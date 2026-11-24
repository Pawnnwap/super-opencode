"""MCP schemas and async handlers enforce bounded, non-blocking contracts."""

from __future__ import annotations

import asyncio

import pytest

from mcp_server import codehelp, web_tools


def test_web_tool_schemas_declare_input_bounds():
    tools = {tool.name: tool for tool in asyncio.run(web_tools.list_tools())}

    search = tools["web_search"].inputSchema["properties"]
    fetch = tools["web_fetch"].inputSchema["properties"]
    assert search["query"]["minLength"] == 1
    assert search["max_results"]["minimum"] == 1
    assert search["max_results"]["maximum"] > 1
    assert fetch["url"]["minLength"] == 1
    assert fetch["max_chars"]["minimum"] == 1
    assert fetch["max_chars"]["maximum"] > 1


def test_codehelp_schemas_declare_required_string_bounds():
    tools = {tool.name: tool for tool in asyncio.run(codehelp.list_tools())}

    assert (
        tools["search_docstrings"].inputSchema["properties"]["target"]["minLength"] == 1
    )
    assert (
        tools["search_package_version"].inputSchema["properties"]["package_name"][
            "minLength"
        ]
        == 1
    )
    assert (
        tools["fetch_official_docs"].inputSchema["properties"]["package_name"][
            "minLength"
        ]
        == 1
    )


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("web_search", {"query": "x", "max_results": 0}),
        ("web_search", {"query": "x", "max_results": 10_000}),
        ("web_fetch", {"url": "https://example.com", "max_chars": 0}),
        ("web_fetch", {"url": "https://example.com", "max_chars": 10_000_000}),
    ],
)
def test_web_tools_reject_out_of_range_arguments(name, arguments):
    with pytest.raises(ValueError):
        asyncio.run(web_tools.call_tool(name, arguments))


def test_web_search_does_not_block_the_event_loop(monkeypatch):
    def slow_search(*_args):
        import time

        time.sleep(0.2)
        return []

    monkeypatch.setattr(web_tools, "_search_both", slow_search)

    async def scenario():
        task = asyncio.create_task(web_tools.call_tool("web_search", {"query": "x"}))
        await asyncio.sleep(0.05)
        assert not task.done()
        await task

    asyncio.run(scenario())


def test_codehelp_search_does_not_block_the_event_loop(monkeypatch):
    def slow_search(*_args):
        import time

        time.sleep(0.2)
        return {"status": "ok", "matches": []}

    monkeypatch.setattr(codehelp, "_codehelp_search_docstrings", slow_search)

    async def scenario():
        task = asyncio.create_task(
            codehelp.call_tool("search_docstrings", {"target": "x"})
        )
        await asyncio.sleep(0.05)
        assert not task.done()
        await task

    asyncio.run(scenario())
