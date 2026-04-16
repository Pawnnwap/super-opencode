"""codehelp.py  –  MCP server exposing code-help tools (server: codehelp)
================================================================================

opencode sees: codehelp_search_docstrings / codehelp_search_package_version /
               codehelp_search_package_examples

    search_docstrings(target, codebase_path?)
    search_package_version(package_name, ecosystem?)
    search_package_examples(query, max_results?)

── search_docstrings ──────────────────────────────────────────────────────────
Walks Python files under codebase_path (default: cwd) and extracts docstrings
matching the target name.

target forms:
  • "requests"              → package/module-level docstring
  • "requests.get"          → function named "get" inside any requests file
  • "Session"               → any class named Session
  • "Session.send"          → method "send" on class "Session"

Returns matched docstrings with file path and line number.

── search_package_version ─────────────────────────────────────────────────────
Queries dedicated package registries for the latest published version:
  • PyPI    (https://pypi.org/pypi/{pkg}/json)   — Python default
  • npm     (https://registry.npmjs.org/{pkg})   — JavaScript fallback / explicit

Returns: version, published date, summary, homepage/docs URL.

── search_package_examples ────────────────────────────────────────────────────
Searches reliable coder forums for example usage and best practices:
  1. Stack Overflow API  — top voted questions tagged with the library
  2. Real Python search  — curated Python tutorial excerpts

Returns: question title, score, excerpt, and canonical URL.

Usage
-----
    python codehelp.py

Dependencies
------------
    pip install mcp
"""

from __future__ import annotations

import ast
import gzip
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP SDK import
# ---------------------------------------------------------------------------
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError as exc:
    sys.exit(
        "mcp package not found.  Install it with:  pip install mcp\n"
        f"Original error: {exc}",
    )

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = 10  # seconds
_UA = "codehelp-mcp/1.0 (super-opencode; +https://github.com/Pawnnwap/super-opencode)"


def _http_get(url: str, *, timeout: int = _DEFAULT_TIMEOUT) -> dict[str, Any] | list[Any]:
    """GET url, decode JSON. Raises urllib.error.URLError / json.JSONDecodeError."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.info().get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Tool 1 – search_docstrings
# ---------------------------------------------------------------------------

_MAX_FILES = 500   # cap to avoid runaway scans
_MAX_RESULTS = 20


def _iter_py_files(root: Path, max_files: int = _MAX_FILES):
    """Yield .py files under root, skipping hidden dirs and __pycache__."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden dirs, __pycache__, .venv, node_modules, etc.
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d not in ("__pycache__", ".venv", "venv", "node_modules", ".git")
        ]
        for fname in filenames:
            if fname.endswith(".py"):
                yield Path(dirpath) / fname
                count += 1
                if count >= max_files:
                    return


def _get_docstring(node: ast.AST) -> str | None:
    """Return the docstring of a module/class/function node, or None."""
    if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    return ast.get_docstring(node)


def _match_target(target: str, filepath: Path, root: Path) -> list[dict[str, Any]]:
    """Parse filepath and return all docstrings matching target."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    parts = [p.strip() for p in target.split(".") if p.strip()]
    results: list[dict[str, Any]] = []

    rel_path = str(filepath.relative_to(root))

    # Determine if target refers to module/package level
    # e.g. target="requests" matches file requests.py or requests/__init__.py
    def file_matches_package(parts: list[str]) -> bool:
        if not parts:
            return False
        stem = filepath.stem  # filename without .py
        parent_name = filepath.parent.name
        if len(parts) == 1:
            return stem == parts[0] or (stem == "__init__" and parent_name == parts[0])
        return False

    # Module-level docstring
    if len(parts) <= 1 and file_matches_package(parts):
        doc = ast.get_docstring(tree)
        if doc:
            results.append({
                "kind": "module",
                "name": target,
                "file": rel_path,
                "line": 1,
                "docstring": doc,
            })

    # Walk AST for class / function / method matches
    # Build a flat list of (qualified_name, node) pairs
    def walk_nodes(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qname = f"{prefix}.{child.name}" if prefix else child.name
                doc = ast.get_docstring(child)
                if doc:
                    # Check if child name or qualified name matches target parts
                    child_parts = qname.split(".")
                    if _parts_match(parts, child_parts):
                        results.append({
                            "kind": "class" if isinstance(child, ast.ClassDef) else "function",
                            "name": qname,
                            "file": rel_path,
                            "line": child.lineno,
                            "docstring": doc,
                        })
                walk_nodes(child, qname)

    walk_nodes(tree)
    return results


def _parts_match(target_parts: list[str], node_parts: list[str]) -> bool:
    """True if target_parts is a suffix of node_parts (case-sensitive)."""
    if len(target_parts) > len(node_parts):
        return False
    return node_parts[-len(target_parts):] == target_parts


def _codehelp_search_docstrings(target: str, codebase_path: str = ".") -> dict[str, Any]:
    root = Path(codebase_path).resolve()
    if not root.is_dir():
        return {"error": f"codebase_path is not a directory: {root}"}

    target = target.strip()
    if not target:
        return {"error": "target must not be empty"}

    all_results: list[dict[str, Any]] = []
    files_scanned = 0

    for py_file in _iter_py_files(root):
        matches = _match_target(target, py_file, root)
        all_results.extend(matches)
        files_scanned += 1
        if len(all_results) >= _MAX_RESULTS:
            break

    if not all_results:
        return {
            "target": target,
            "found": 0,
            "files_scanned": files_scanned,
            "message": "No docstrings found matching the target. The name may not exist in this codebase.",
        }

    return {
        "target": target,
        "found": len(all_results),
        "files_scanned": files_scanned,
        "results": all_results[:_MAX_RESULTS],
    }


# ---------------------------------------------------------------------------
# Tool 2 – search_package_version
# ---------------------------------------------------------------------------

_PYPI_URL = "https://pypi.org/pypi/{package}/json"
_NPM_URL = "https://registry.npmjs.org/{package}/latest"


def _pypi_version(package: str) -> dict[str, Any]:
    url = _PYPI_URL.format(package=urllib.parse.quote(package, safe=""))
    data = _http_get(url)
    info = data.get("info", {})
    releases = data.get("releases", {})
    latest = info.get("version", "unknown")

    # Find publish date of the latest release
    pub_date = "unknown"
    if latest in releases:
        files = releases[latest]
        if files:
            pub_date = files[0].get("upload_time", "unknown")
            if pub_date != "unknown":
                pub_date = pub_date[:10]  # ISO date only

    return {
        "ecosystem": "PyPI",
        "package": package,
        "latest_version": latest,
        "published_date": pub_date,
        "summary": info.get("summary", ""),
        "homepage": info.get("home_page") or info.get("project_url") or f"https://pypi.org/project/{package}/",
        "docs_url": info.get("docs_url") or "",
        "requires_python": info.get("requires_python") or "",
        "source": _PYPI_URL.format(package=package),
    }


def _npm_version(package: str) -> dict[str, Any]:
    url = _NPM_URL.format(package=urllib.parse.quote(package, safe="@/"))
    data = _http_get(url)
    return {
        "ecosystem": "npm",
        "package": package,
        "latest_version": data.get("version", "unknown"),
        "published_date": (data.get("dist", {}).get("tarball", "")[:10] if False else "see npm"),
        "summary": data.get("description", ""),
        "homepage": data.get("homepage", f"https://www.npmjs.com/package/{package}"),
        "source": f"https://registry.npmjs.org/{package}/latest",
    }


def _codehelp_search_package_version(package_name: str, ecosystem: str = "auto") -> dict[str, Any]:
    package_name = package_name.strip()
    if not package_name:
        return {"error": "package_name must not be empty"}

    ecosystem = ecosystem.lower()
    errors: list[str] = []

    if ecosystem in ("auto", "pypi", "python"):
        try:
            return _pypi_version(package_name)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                errors.append(f"PyPI: package '{package_name}' not found (404)")
            else:
                errors.append(f"PyPI HTTP {e.code}: {e.reason}")
        except Exception as e:
            errors.append(f"PyPI: {e}")

    if ecosystem in ("auto", "npm", "node", "javascript", "js"):
        try:
            return _npm_version(package_name)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                errors.append(f"npm: package '{package_name}' not found (404)")
            else:
                errors.append(f"npm HTTP {e.code}: {e.reason}")
        except Exception as e:
            errors.append(f"npm: {e}")

    return {
        "error": "Package not found in any registry",
        "package": package_name,
        "tried": errors,
    }


# ---------------------------------------------------------------------------
# Tool 3 – search_package_examples
# ---------------------------------------------------------------------------

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

_EXCERPT_LEN = 400   # characters of body/excerpt to include


def _strip_html(text: str) -> str:
    """Very light HTML tag stripper — no deps needed."""
    import re
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _so_results(query: str, package_tag: str, n: int) -> list[dict[str, Any]]:
    """Fetch Stack Overflow results, first with tag, fall back without."""
    tag = urllib.parse.quote(package_tag.lower(), safe="")
    q = urllib.parse.quote(query, safe="")

    # Try with tag first (more focused)
    url = _SO_SEARCH_URL.format(q=q, tag=tag, n=n)
    try:
        data = _http_get(url)
        items = data.get("items", [])
    except Exception:
        items = []

    # Fallback: no tag filter if tag yields nothing
    if not items:
        url = _SO_SEARCH_NOTAG_URL.format(q=q, n=n)
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
            "excerpt": body[:_EXCERPT_LEN] + ("…" if len(body) > _EXCERPT_LEN else ""),
        })
    return results


def _realpython_results(query: str) -> list[dict[str, Any]]:
    """Fetch Real Python article results (best-effort — may 404)."""
    url = _REALPYTHON_SEARCH_URL.format(q=urllib.parse.quote(query, safe=""))
    try:
        data = _http_get(url)
        articles = data.get("results", [])
    except Exception:
        return []

    results = []
    for art in articles[:3]:
        results.append({
            "source": "realpython",
            "title": art.get("title", ""),
            "url": "https://realpython.com" + art.get("url", ""),
            "excerpt": _strip_html(art.get("blurb", ""))[:_EXCERPT_LEN],
            "published": art.get("pub_date", ""),
        })
    return results


def _codehelp_search_package_examples(query: str, max_results: int = 5) -> dict[str, Any]:
    query = query.strip()
    if not query:
        return {"error": "query must not be empty"}

    max_results = max(1, min(max_results, 10))

    # Use first word of query as the SO tag candidate (e.g. "requests" from "requests session pooling")
    tag_candidate = query.split()[0].lower()

    so = _so_results(query, tag_candidate, max_results)
    rp = _realpython_results(query)

    combined = so + rp
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


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("codehelp")


CODEHELP_SEARCH_DOCSTRINGS_TOOL = Tool(
    name="search_docstrings",
    description=(
        "Search the local codebase for docstrings of a package, module, class, "
        "function, or method.\n\n"
        "target forms:\n"
        "  • 'requests'        → module/package-level docstring\n"
        "  • 'requests.get'    → function 'get' inside any file under 'requests/'\n"
        "  • 'Session'         → any class named Session\n"
        "  • 'Session.send'    → method 'send' on class 'Session'\n\n"
        "Returns matched docstrings with file path and line number. "
        "Use codebase_path to narrow the search to a subdirectory."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Name to search for. Supports dotted paths like 'MyClass.my_method' "
                    "or bare names like 'parse'. Case-sensitive."
                ),
            },
            "codebase_path": {
                "type": "string",
                "description": (
                    "Root directory to search (absolute or relative to cwd). "
                    "Defaults to '.' (current working directory)."
                ),
                "default": ".",
            },
        },
        "required": ["target"],
    },
)

CODEHELP_SEARCH_PACKAGE_VERSION_TOOL = Tool(
    name="search_package_version",
    description=(
        "Look up the latest published version of a package from its official registry.\n\n"
        "Checks dedicated package registries (NOT a general web search):\n"
        "  • PyPI  (https://pypi.org)  — Python packages (default)\n"
        "  • npm   (https://npmjs.com) — JavaScript/Node packages\n\n"
        "Returns: latest version, publish date, summary, homepage/docs URL.\n\n"
        "Set ecosystem='npm' for JavaScript packages. "
        "Default ecosystem='auto' tries PyPI first, then npm."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "package_name": {
                "type": "string",
                "description": "Package name exactly as it appears on the registry (e.g. 'requests', 'numpy', 'react').",
            },
            "ecosystem": {
                "type": "string",
                "enum": ["auto", "pypi", "npm"],
                "description": "Registry to query. 'auto' tries PyPI then npm. Default: 'auto'.",
                "default": "auto",
            },
        },
        "required": ["package_name"],
    },
)

CODEHELP_SEARCH_PACKAGE_EXAMPLES_TOOL = Tool(
    name="search_package_examples",
    description=(
        "Search reliable coder forums for example usage and best practices of a package.\n\n"
        "Sources queried (in order):\n"
        "  1. Stack Overflow — top voted questions tagged with the library\n"
        "  2. Real Python   — curated Python tutorial articles\n\n"
        "Returns question titles, vote scores, answer counts, excerpts, and URLs.\n\n"
        "query examples:\n"
        "  • 'requests session connection pooling'\n"
        "  • 'pandas groupby aggregate best practice'\n"
        "  • 'asyncio timeout handling'\n\n"
        "Use max_results (1–10) to control how many Stack Overflow results to fetch. "
        "Tip: leading term of query is used as the SO tag filter."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search query. Start with the package name for best tag filtering. "
                    "E.g. 'requests retry on failure', 'flask authentication blueprint'."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Max number of Stack Overflow results to return (1–10). Default: 5.",
                "default": 5,
                "minimum": 1,
                "maximum": 10,
            },
        },
        "required": ["query"],
    },
)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        CODEHELP_SEARCH_DOCSTRINGS_TOOL,
        CODEHELP_SEARCH_PACKAGE_VERSION_TOOL,
        CODEHELP_SEARCH_PACKAGE_EXAMPLES_TOOL,
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:

    # ── search_docstrings ────────────────────────────────────────────────────
    if name == "search_docstrings":
        target = arguments.get("target", "")
        codebase_path = arguments.get("codebase_path", ".")
        if not target:
            return [TextContent(type="text", text="Error: 'target' is required")]
        result = _codehelp_search_docstrings(target, codebase_path)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    # ── search_package_version ───────────────────────────────────────────────
    if name == "search_package_version":
        package_name = arguments.get("package_name", "")
        ecosystem = arguments.get("ecosystem", "auto")
        if not package_name:
            return [TextContent(type="text", text="Error: 'package_name' is required")]
        try:
            result = _codehelp_search_package_version(package_name, ecosystem)
        except Exception as exc:
            result = {"error": str(exc), "package": package_name}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    # ── search_package_examples ──────────────────────────────────────────────
    if name == "search_package_examples":
        query = arguments.get("query", "")
        max_results = int(arguments.get("max_results", 5))
        if not query:
            return [TextContent(type="text", text="Error: 'query' is required")]
        try:
            result = _codehelp_search_package_examples(query, max_results)
        except Exception as exc:
            result = {"error": str(exc), "query": query}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    raise ValueError(f"Unknown tool: '{name}'")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(_main())
