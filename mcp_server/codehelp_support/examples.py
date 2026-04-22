from __future__ import annotations

import re
import urllib.parse
from typing import Any

from .http import _http_get

_SO_SEARCH_URL = (
    "https://api.stackexchange.com/2.3/search/advanced"
    "?order=desc&sort=votes&q={q}&tagged={tag}&site=stackoverflow"
    "&filter=withbody&pagesize={n}&key="
)
_SO_SEARCH_NOTAG_URL = (
    "https://api.stackexchange.com/2.3/search/advanced"
    "?order=desc&sort=votes&q={q}&site=stackoverflow"
    "&filter=withbody&pagesize={n}&key="
)
_REALPYTHON_SEARCH_URL = "https://realpython.com/search/api/?q={q}&kind=article&page=1"
_EXCERPT_LEN = 400


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _so_results(query: str, package_tag: str, n: int) -> list[dict[str, Any]]:
    tag = urllib.parse.quote(package_tag.lower(), safe="")
    quoted_query = urllib.parse.quote(query, safe="")

    url = _SO_SEARCH_URL.format(q=quoted_query, tag=tag, n=n)
    try:
        data = _http_get(url)
        items = data.get("items", [])
    except Exception:
        items = []

    if not items:
        url = _SO_SEARCH_NOTAG_URL.format(q=quoted_query, n=n)
        try:
            data = _http_get(url)
            items = data.get("items", [])
        except Exception:
            items = []

    results = []
    for item in items:
        body = _strip_html(item.get("body", item.get("excerpt", "")))
        results.append({
            "source": "stackoverflow",
            "title": item.get("title", ""),
            "score": item.get("score", 0),
            "is_answered": item.get("is_answered", False),
            "answer_count": item.get("answer_count", 0),
            "url": item.get("link", ""),
            "tags": item.get("tags", []),
            "excerpt": body[:_EXCERPT_LEN] + ("..." if len(body) > _EXCERPT_LEN else ""),
        })
    return results


def _realpython_results(query: str) -> list[dict[str, Any]]:
    url = _REALPYTHON_SEARCH_URL.format(q=urllib.parse.quote(query, safe=""))
    try:
        data = _http_get(url)
        articles = data.get("results", [])
    except Exception:
        return []

    results = []
    for article in articles[:3]:
        results.append({
            "source": "realpython",
            "title": article.get("title", ""),
            "url": "https://realpython.com" + article.get("url", ""),
            "excerpt": _strip_html(article.get("blurb", ""))[:_EXCERPT_LEN],
            "published": article.get("pub_date", ""),
        })
    return results


def _codehelp_search_package_examples(query: str, max_results: int = 5) -> dict[str, Any]:
    query = query.strip()
    if not query:
        return {"error": "query must not be empty"}

    max_results = max(1, min(max_results, 10))
    tag_candidate = query.split()[0].lower()

    so_results = _so_results(query, tag_candidate, max_results)
    realpython_results = _realpython_results(query)
    combined = so_results + realpython_results
    if not combined:
        return {
            "query": query,
            "found": 0,
            "message": "No results found. Try a broader or differently phrased query.",
        }

    return {
        "query": query,
        "found": len(combined),
        "results": combined,
        "sources": ["stackoverflow", "realpython"],
        "note": (
            "Stack Overflow results sorted by vote score. "
            "Real Python results are curated articles. "
            "For broader search, try varying the query."
        ),
    }
