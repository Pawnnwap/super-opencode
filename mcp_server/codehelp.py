"""MCP entrypoint for codehelp search tools."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

try:
    from mcp_server.codehelp_support.docstrings import (
        _codehelp_search_docstrings,
        _get_docstring,
        _iter_py_files,
        _match_target,
        _parts_match,
    )
    from mcp_server.codehelp_support.examples import (
        _codehelp_search_package_examples,
        _realpython_results,
        _so_results,
        _strip_html,
    )
    from mcp_server.codehelp_support.http import _http_get
    from mcp_server.codehelp_support.dependencies import _codehelp_analyze_dependencies
    from mcp_server.codehelp_support.docs import _codehelp_fetch_official_docs
    from mcp_server.codehelp_support.packages import (
        _codehelp_search_package_version,
        _npm_version,
        _pypi_version,
    )
except ImportError:
    from codehelp_support.docstrings import (  # type: ignore[no-redef]
        _codehelp_search_docstrings,
        _get_docstring,
        _iter_py_files,
        _match_target,
        _parts_match,
    )
    from codehelp_support.examples import (  # type: ignore[no-redef]
        _codehelp_search_package_examples,
        _realpython_results,
        _so_results,
        _strip_html,
    )
    from codehelp_support.http import _http_get  # type: ignore[no-redef]
    from codehelp_support.dependencies import _codehelp_analyze_dependencies  # type: ignore[no-redef]
    from codehelp_support.docs import _codehelp_fetch_official_docs  # type: ignore[no-redef]
    from codehelp_support.packages import (  # type: ignore[no-redef]
        _codehelp_search_package_version,
        _npm_version,
        _pypi_version,
    )

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError as exc:
    sys.exit(
        "mcp package not found.  Install it with:  pip install mcp\n"
        f"Original error: {exc}",
    )

server = Server("codehelp")


CODEHELP_SEARCH_DOCSTRINGS_TOOL = Tool(
    name="search_docstrings",
    description=(
        "Search the local codebase for docstrings of a package, module, class, "
        "function, or method.\n\n"
        "target forms:\n"
        "  - 'requests'        -> module/package-level docstring\n"
        "  - 'requests.get'    -> function 'get' inside any file under 'requests/'\n"
        "  - 'Session'         -> any class named Session\n"
        "  - 'Session.send'    -> method 'send' on class 'Session'\n\n"
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
        "  - PyPI  (https://pypi.org)  - Python packages (default)\n"
        "  - npm   (https://npmjs.com) - JavaScript/Node packages\n\n"
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
        "Search community sources for example usage and best practices of a package.\n\n"
        "Sources queried (in order):\n"
        "  1. Stack Overflow - top voted questions tagged with the library\n"
        "  2. Real Python   - curated Python tutorial articles\n\n"
        "Returns question titles, vote scores, answer counts, excerpts, and URLs.\n\n"
        "query examples:\n"
        "  - 'requests session connection pooling'\n"
        "  - 'pandas groupby aggregate best practice'\n"
        "  - 'asyncio timeout handling'\n\n"
        "This is community evidence, not official documentation. Prefer "
        "fetch_official_docs for package APIs and compatibility details.\n\n"
        "Use max_results (1-10) to control how many Stack Overflow results to fetch. "
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
                "description": "Max number of Stack Overflow results to return (1-10). Default: 5.",
                "default": 5,
                "minimum": 1,
                "maximum": 10,
            },
        },
        "required": ["query"],
    },
)

CODEHELP_ANALYZE_DEPENDENCY_TOOL = Tool(
    name="analyze_dependency",
    description=(
        "Inspect declared and locked dependencies in a local project without changing files. "
        "Reads pyproject.toml, requirements*.txt, package.json, and package-lock.json. "
        "Returns source files, declared constraints, lock versions, and a simple compatibility-risk label. "
        "Set package_name to narrow results. Set include_latest=true only when registry comparison is needed; "
        "lookups are capped at 20 packages."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "codebase_path": {
                "type": "string",
                "description": "Project root to inspect. Defaults to current working directory.",
                "default": ".",
            },
            "package_name": {
                "type": "string",
                "description": "Optional exact package name filter.",
                "default": "",
            },
            "include_latest": {
                "type": "boolean",
                "description": "Compare each selected dependency with its registry's latest version. Defaults to false.",
                "default": False,
            },
        },
    },
)

CODEHELP_FETCH_OFFICIAL_DOCS_TOOL = Tool(
    name="fetch_official_docs",
    description=(
        "Fetch a short excerpt from public HTTPS documentation URL declared by PyPI or npm registry metadata. "
        "Returns registry and HTTP provenance, cache state, and an authority warning. "
        "Use this before community examples for current package API guidance."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "package_name": {
                "type": "string",
                "description": "Package name as listed in PyPI or npm.",
            },
            "ecosystem": {
                "type": "string",
                "enum": ["auto", "pypi", "npm"],
                "description": "Registry to query. Defaults to auto.",
                "default": "auto",
            },
            "query": {
                "type": "string",
                "description": "Optional topic retained with result for agent context; page search is not performed.",
                "default": "",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum returned text, from 200 to 8000. Defaults to 4000.",
                "minimum": 200,
                "maximum": 8000,
                "default": 4000,
            },
        },
        "required": ["package_name"],
    },
)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        CODEHELP_SEARCH_DOCSTRINGS_TOOL,
        CODEHELP_SEARCH_PACKAGE_VERSION_TOOL,
        CODEHELP_ANALYZE_DEPENDENCY_TOOL,
        CODEHELP_FETCH_OFFICIAL_DOCS_TOOL,
        CODEHELP_SEARCH_PACKAGE_EXAMPLES_TOOL,
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name == "search_docstrings":
        target = arguments.get("target", "")
        codebase_path = arguments.get("codebase_path", ".")
        if not target:
            return [TextContent(type="text", text="Error: 'target' is required")]
        result = _codehelp_search_docstrings(target, codebase_path)
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

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

    if name == "analyze_dependency":
        codebase_path = arguments.get("codebase_path", ".")
        package_name = arguments.get("package_name", "")
        include_latest = bool(arguments.get("include_latest", False))
        try:
            result = _codehelp_analyze_dependencies(codebase_path, package_name, include_latest)
        except Exception as exc:
            result = {"status": "error", "error": str(exc), "codebase_path": codebase_path}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "fetch_official_docs":
        package_name = arguments.get("package_name", "")
        ecosystem = arguments.get("ecosystem", "auto")
        query = arguments.get("query", "")
        try:
            max_chars = int(arguments.get("max_chars", 4_000))
        except (TypeError, ValueError):
            return [TextContent(type="text", text="Error: 'max_chars' must be an integer")]
        if not package_name:
            return [TextContent(type="text", text="Error: 'package_name' is required")]
        try:
            result = _codehelp_fetch_official_docs(package_name, ecosystem, query, max_chars)
        except Exception as exc:
            result = {"status": "error", "error": str(exc), "package": package_name}
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    raise ValueError(f"Unknown tool: '{name}'")


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
