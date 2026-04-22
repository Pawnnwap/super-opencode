"""MCP entrypoint for hashline safe file editing tools."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

try:
    from mcp_server.hashline_support.autofix import (
        _FixSummary,
        _SKIPPED_FIXERS_REASON,
        _autofix_autopep8,
        _autofix_isort,
        _autofix_pyupgrade,
        _autofix_ruff,
        _ensure_fix_tool,
        _run_autofix,
        _run_fix,
    )
    from mcp_server.hashline_support.core import (
        _MismatchError,
        _apply_edits,
        _compact_diff,
        _hashline_edit,
        _hashline_read,
        _hashline_write,
        _write_atomic,
    )
    from mcp_server.hashline_support.refs import (
        _auto_patch_edits,
        _check_edit_conflicts,
        _compute_line_hash,
        _format_tagged_line,
        _parse_ref,
        _validate_all_refs,
    )
except ImportError:
    from hashline_support.autofix import (  # type: ignore[no-redef]
        _FixSummary,
        _SKIPPED_FIXERS_REASON,
        _autofix_autopep8,
        _autofix_isort,
        _autofix_pyupgrade,
        _autofix_ruff,
        _ensure_fix_tool,
        _run_autofix,
        _run_fix,
    )
    from hashline_support.core import (  # type: ignore[no-redef]
        _MismatchError,
        _apply_edits,
        _compact_diff,
        _hashline_edit,
        _hashline_read,
        _hashline_write,
        _write_atomic,
    )
    from hashline_support.refs import (  # type: ignore[no-redef]
        _auto_patch_edits,
        _check_edit_conflicts,
        _compute_line_hash,
        _format_tagged_line,
        _parse_ref,
        _validate_all_refs,
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

server = Server("hashline")


HASHLINE_READ_TOOL = Tool(
    name="read",
    description=(
        "Read a file with LINE#ID annotations required for safe editing.\n\n"
        "PREFER THIS OVER built-in `read` when the file will be edited - "
        "without LINE#IDs, `hashline edit` rejects every operation.\n\n"
        "Format:  42#VKB| def process(data):\n"
        "         43#XJZ|     return transform(data)\n\n"
        "Pass LINE#IDs directly to `hashline edit`. "
        "IDs go stale on every write - re-read after each edit.\n\n"
        "Use start_line/end_line to read only the relevant section."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path (absolute or relative to cwd)."},
            "start_line": {"type": "integer", "description": "First line to return (1-based). Default: 1.", "minimum": 1},
            "end_line": {"type": "integer", "description": "Last line to return (1-based). Default: EOF.", "minimum": 1},
        },
        "required": ["path"],
    },
)

HASHLINE_EDIT_TOOL = Tool(
    name="edit",
    description=(
        "Apply validated edits to a file using LINE#IDs from `hashline read`.\n\n"
        "PREFER THIS OVER built-in `write`/`edit` - validates atomically before "
        "touching disk; a failed write never corrupts the file.\n\n"
        "auto_retry=true (default): stale refs are auto-patched and re-applied "
        "server-side in the same call. No extra round-trip needed.\n\n"
        "auto_retry=false: returns retry_edits with corrected refs on mismatch.\n\n"
        "autofix=true: after writing, runs safe style/security fixers (isort, "
        "autopep8, pyupgrade, ruff) on the edited file. Unused-import and dead-code "
        "fixers are intentionally skipped because hashline edit only touches specific "
        "sections - removing 'unused' symbols from a fragment may break callers "
        "elsewhere in the codebase.\n\n"
        "On success: diff is returned (key omitted if no changes).\n\n"
        "ops: replace | replace_range (pos+end_pos) | delete | append | prepend\n"
        "Multiple edits -> one atomic write. Overlapping edits rejected upfront."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File to edit."},
            "edits": {
                "type": "array",
                "description": "Edit operations to apply.",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["op", "pos"],
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": ["replace", "replace_range", "delete", "append", "prepend"],
                            "description": (
                                "Operation type:\n"
                                "  replace       - Replace a single line at pos with lines[].\n"
                                "  replace_range - Replace lines from pos to end_pos (BOTH INCLUSIVE) with lines[]. "
                                "Use this to swap out an entire block; do NOT use append/prepend for block replacement "
                                "or the old block will remain as a duplicate.\n"
                                "  delete        - Delete the single line at pos.\n"
                                "  append        - Insert lines[] AFTER pos (does not remove pos).\n"
                                "  prepend       - Insert lines[] BEFORE pos (does not remove pos)."
                            ),
                        },
                        "pos": {"type": "string", "description": "LINE#ID of target line, e.g. '42#VKB'."},
                        "end_pos": {
                            "type": "string",
                            "description": (
                                "LINE#ID of the LAST line to replace - INCLUSIVE. "
                                "Example: to replace lines 10 through 15 entirely, set "
                                "pos='10#...' and end_pos='15#...'. Line 15 is replaced, not kept. "
                                "Required for replace_range; omit for single-line replace."
                            ),
                        },
                        "lines": {"type": "array", "items": {"type": "string"}, "description": "Lines to insert/replace (omit for delete)."},
                    },
                },
            },
            "dry_run": {
                "type": "boolean",
                "description": "Validate + diff only; do not write. Default: false.",
                "default": False,
            },
            "auto_retry": {
                "type": "boolean",
                "description": (
                    "When true (default), stale LINE#IDs are auto-corrected and "
                    "the edit re-applied server-side - no round-trip needed. "
                    "Set false to receive retry_edits for manual retry."
                ),
                "default": True,
            },
            "autofix": {
                "type": "boolean",
                "description": (
                    "When true, run safe style/security fixers (isort, autopep8, "
                    "pyupgrade, ruff) on the file after editing. Skipped for dry_run. "
                    "Unused-import (autoflake) and dead-code (deadcode) fixers are "
                    "always excluded - they need whole-codebase context. Default: false."
                ),
                "default": False,
            },
        },
        "required": ["path", "edits"],
    },
)

HASHLINE_WRITE_TOOL = Tool(
    name="write",
    description=(
        "Create a new file from a list of lines.\n\n"
        "USE THIS instead of built-in `write` for creating new files - "
        "it refuses to silently overwrite existing files unless overwrite=true "
        "is explicitly set, preventing accidental data loss.\n\n"
        "Parent directories are created automatically.\n\n"
        "autofix=true: after writing, runs safe style/security fixers (isort, "
        "autopep8, pyupgrade, ruff) on the new file. Unused-import and dead-code "
        "fixers are intentionally skipped because the new file may be a module "
        "whose exports are only consumed by the rest of the codebase.\n\n"
        "For editing an existing file, use `hashline edit` instead."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path of the file to create (absolute or relative to cwd).",
            },
            "lines": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File content as a list of lines (without newline characters).",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Allow overwriting an existing file. Default: false.",
                "default": False,
            },
            "autofix": {
                "type": "boolean",
                "description": (
                    "When true, run safe style/security fixers (isort, autopep8, "
                    "pyupgrade, ruff) on the file after writing. Unused-import "
                    "(autoflake) and dead-code (deadcode) fixers are always excluded "
                    "- they need whole-codebase context. Default: false."
                ),
                "default": False,
            },
        },
        "required": ["path", "lines"],
    },
)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [HASHLINE_READ_TOOL, HASHLINE_EDIT_TOOL, HASHLINE_WRITE_TOOL]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    if name == "read":
        path = arguments.get("path")
        if not path:
            return [TextContent(type="text", text="Error: 'path' is required")]

        try:
            result = _hashline_read(
                path,
                start_line=arguments.get("start_line"),
                end_line=arguments.get("end_line"),
            )
        except (FileNotFoundError, IsADirectoryError, ValueError) as exc:
            return [TextContent(type="text", text=f"Error: {exc}")]

        start, end, total = result["start_line"], result["end_line"], result["total_lines"]
        range_note = f"Lines {start}-{end} of {total}" if (start != 1 or end != total) else f"{total} lines"
        return [TextContent(type="text", text=f"File: {result['path']} ({range_note})\n\n{result['content']}")]

    if name == "edit":
        path = arguments.get("path")
        edits = arguments.get("edits")
        dry_run = bool(arguments.get("dry_run", False))
        auto_retry = bool(arguments.get("auto_retry", True))
        autofix = bool(arguments.get("autofix", False))

        if not path:
            return [TextContent(type="text", text="Error: 'path' is required")]
        if not edits:
            return [TextContent(type="text", text="Error: 'edits' must be a non-empty list")]

        try:
            result = _hashline_edit(
                path,
                edits,
                dry_run=dry_run,
                auto_retry=auto_retry,
                autofix=autofix,
            )
        except _MismatchError as exc:
            return [TextContent(type="text", text=str(exc))]
        except (FileNotFoundError, ValueError) as exc:
            return [TextContent(type="text", text=f"Error: {exc}")]

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    if name == "write":
        path = arguments.get("path")
        lines = arguments.get("lines")
        overwrite = bool(arguments.get("overwrite", False))
        autofix = bool(arguments.get("autofix", False))

        if not path:
            return [TextContent(type="text", text="Error: 'path' is required")]
        if lines is None:
            return [TextContent(type="text", text="Error: 'lines' is required")]
        if not isinstance(lines, list):
            return [TextContent(type="text", text="Error: 'lines' must be an array of strings")]

        try:
            result = _hashline_write(path, lines, overwrite=overwrite, autofix=autofix)
        except FileExistsError as exc:
            return [TextContent(type="text", text=f"Error: {exc}")]
        except (OSError, ValueError) as exc:
            return [TextContent(type="text", text=f"Error: {exc}")]

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    raise ValueError(f"Unknown tool: '{name}'")


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
