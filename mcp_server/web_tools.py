from __future__ import annotations

import html as html_mod
import re
import urllib.parse

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

try:
    from mcp_server.codehelp_support.http import _http_get_text
except ImportError:
    from codehelp_support.http import _http_get_text  # type: ignore[no-redef]

_SEARCH_ENDPOINT = (
    "https://www.bing.com/search?setmkt=en-US&setlang=en"
)
# Independent index fallback: Yandex/DDG/Brave/Startpage/searx are
# blocked or captcha-gated from this network; marginalia responds with
# organic results (verified 2026-10-13).
_FALLBACK_ENDPOINT = "https://search.marginalia.nu/search"
_MAX_FETCH_CHARS = 200_000
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

server = Server("web_tools")


def _strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return html_mod.unescape(re.sub(r"\s+", " ", fragment)).strip()


def _marginalia_results(html_content: str, max_results: int) -> list[dict[str, str]]:
    """Parse Marginalia's independent index: keep the longest anchor text per URL."""
    titled: dict[str, str] = {}
    for m in re.finditer(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', html_content, re.DOTALL):
        url = m.group(1)
        if any(d in url for d in ("marginalia", "web.archive.org")):
            continue
        text = _strip_tags(m.group(2))
        if url not in titled or len(text) > len(titled[url]):
            titled[url] = text
    return [{"title": text or url, "url": url, "snippet": ""} for url, text in titled.items()][:max_results]


def _bing_results(html_content: str, max_results: int) -> list[dict[str, str]]:
    results = []
    for block in re.findall(r'<li class="b_algo".*?</li>', html_content, re.DOTALL):
        match = re.search(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        if not match:
            continue
        url = match.group(1)
        if not url.startswith("http"):
            continue
        title = _strip_tags(match.group(2))
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
        snippet = _strip_tags(snippet_match.group(1)) if snippet_match else ""
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="web_search",
            description=(
                "Search the web (global Bing first, Marginalia fallback). "
                "Returns titles, URLs, and snippets."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="web_fetch",
            description="Fetch a URL and return readable plain text (HTTP status and final URL included).",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Absolute http(s) URL."},
                    "max_chars": {"type": "integer", "default": 200000},
                },
                "required": ["url"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, str]) -> list[TextContent]:
    if name == "web_search":
        query = str(arguments.get("query", ""))
        if not query:
            raise ValueError("query is required")
        max_results = int(arguments.get("max_results", 5))
        results: list[dict[str, str]] = []
        for endpoint, param, parse in (
            (_SEARCH_ENDPOINT, "q", _bing_results),
            (_FALLBACK_ENDPOINT, "query", _marginalia_results),
        ):
            try:
                sep = "&" if "?" in endpoint else "?"
                url = endpoint + sep + urllib.parse.urlencode({param: query})
                text = _http_get_text(url, timeout=20)
                results = parse(text, max_results)
            except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
                continue
            if results:
                break
        lines = "\n".join(
            f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet']}"
            for i, r in enumerate(results)
        ) or "No results."
        return [TextContent(type="text", text=f"{len(results)} results for `{query}`:\n{lines}")]

    if name == "web_fetch":
        url = str(arguments.get("url", ""))
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must be absolute http(s)")
        max_chars = int(arguments.get("max_chars", _MAX_FETCH_CHARS))
        text = _http_get_text(url, timeout=20)
        text = _strip_tags(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.DOTALL))
        return [TextContent(type="text", text=text[:max_chars])]

    raise ValueError(f"Unknown tool: {name}")


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())

