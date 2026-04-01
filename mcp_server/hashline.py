"""
hashline.py  –  MCP server exposing `hashed_read` and `hashed_edit`
================================================================================

Exposes two custom tools (server name = "hashed", so opencode sees):

    hashed_read(path, start_line?, end_line?, hash_algo?)
    hashed_edit(path, edits, dry_run?)

── hashed_read ──────────────────────────────────────────────────────────────
Reads a file and returns its content with LINE#ID annotations:

    42#VK| def process(data):
    43#XJ|     return transform(data)

Use instead of the built-in `read` whenever you intend to edit afterwards.

── hashed_edit ──────────────────────────────────────────────────────────────
Validates LINE#ID references and writes the edited file to disk atomically.
Rejects the entire operation (nothing written) if any ID is stale, and
returns the corrected IDs so opencode can retry immediately.

Edit ops: replace, replace_range, delete, append, prepend.

This replaces the built-in `write`/`edit` for hash-anchored workflows —
validation happens at write time, before any bytes hit disk.

Usage
-----
Run as a standalone stdio MCP server:

    python hashline.py


Dependencies
------------
    pip install mcp
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import os
from pathlib import Path
from typing import Any

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
        f"Original error: {exc}"
    )

# ---------------------------------------------------------------------------
# Hash logic
# ---------------------------------------------------------------------------

_CHARSET = "ZPMQVRWSNKTXJBYH"

_ALGO_MAP: dict[str, str] = {
    "sha256": "sha256",
    "sha1":   "sha1",
    "md5":    "md5",
}


def _compute_line_hash(line_number: int, content: str, algo: str = "sha256") -> str:
    raw = f"{line_number}:{content}"
    h = hashlib.new(_ALGO_MAP.get(algo, "sha256"), raw.encode())
    digest = h.digest()
    return f"{_CHARSET[digest[0] & 0x0F]}{_CHARSET[digest[1] & 0x0F]}"


def _format_tagged_line(line_number: int, content: str, algo: str = "sha256") -> str:
    tag = _compute_line_hash(line_number, content, algo)
    return f"{line_number}#{tag}| {content}"


def _parse_ref(ref: str) -> tuple[int, str]:
    """Parse '42#VK' → (42, 'VK').  Raises ValueError on bad format."""
    ref = ref.strip()
    if "#" not in ref:
        raise ValueError(f"Invalid LINE#ID '{ref}': expected '<line_no>#<2-char-hash>'")
    parts = ref.split("#", 1)
    if not parts[0].isdigit() or len(parts[1]) != 2:
        raise ValueError(f"Invalid LINE#ID '{ref}': expected '<line_no>#<2-char-hash>'")
    return int(parts[0]), parts[1]


# ---------------------------------------------------------------------------
# hashed_read logic
# ---------------------------------------------------------------------------

def _hashed_read(
    path: str | Path,
    start_line: int | None = None,
    end_line: int | None = None,
    algo: str = "sha256",
) -> dict[str, Any]:
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {resolved}")
    if not resolved.is_file():
        raise IsADirectoryError(f"Path is a directory, not a file: {resolved}")

    all_lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(all_lines)

    s = max(1, start_line) if start_line is not None else 1
    e = min(total, end_line) if end_line is not None else total

    if s > total:
        raise ValueError(f"start_line={s} exceeds file length ({total} lines): {resolved}")

    annotated = [
        _format_tagged_line(ln, all_lines[ln - 1], algo)
        for ln in range(s, e + 1)
    ]
    return {
        "path":        str(resolved),
        "total_lines": total,
        "start_line":  s,
        "end_line":    e,
        "algo":        algo,
        "content":     "\n".join(annotated),
    }


# ---------------------------------------------------------------------------
# hashed_edit logic
# ---------------------------------------------------------------------------

class _MismatchError(Exception):
    """Stale LINE#ID references detected — carries structured feedback."""
    def __init__(self, stale: list[tuple[str, str]], snippet: str):
        self.stale = stale        # [(provided_ref, current_ref), ...]
        self.snippet = snippet    # annotated lines around affected area
        super().__init__(self._build())

    def _build(self) -> str:
        lines = [
            f"{len(self.stale)} stale LINE#ID reference(s) — edit rejected, nothing written.",
            "",
            "Stale → Current:",
        ]
        for old, new in self.stale:
            lines.append(f"  {old}  →  {new}")
        lines += ["", "Updated snippet (use these IDs in your retry):", self.snippet]
        return "\n".join(lines)


def _hashed_edit(
    path: str | Path,
    edits: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> str:
    """
    Validate all LINE#ID references, then apply edits atomically.

    Each edit dict:
        op       : "replace" | "delete" | "append" | "prepend"
        pos      : "42#VK"            (required for all ops)
        end_pos  : "45#XJ"            (replace-range only)
        lines    : ["new line", ...]  (replace / append / prepend)

    Writes via a temp file + rename so a failed mid-write never corrupts disk.
    Raises _MismatchError if any ID is stale (nothing written).
    """
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {resolved}")

    original = resolved.read_text(encoding="utf-8", errors="replace").splitlines()

    # ── 1. Validate ALL references before touching anything ──────────────────
    stale: list[tuple[str, str]] = []
    snippet_idxs: set[int] = set()

    for edit in edits:
        for attr in ("pos", "end_pos"):
            ref = edit.get(attr)
            if ref is None:
                continue
            line_no, given_hash = _parse_ref(ref)
            idx = line_no - 1
            if idx < 0 or idx >= len(original):
                stale.append((ref, f"<line {line_no} out of range>"))
                snippet_idxs.update(range(max(0, idx - 2), min(len(original), idx + 3)))
                continue
            expected = _compute_line_hash(line_no, original[idx])
            if given_hash != expected:
                stale.append((ref, f"{line_no}#{expected}"))
                snippet_idxs.update(range(max(0, idx - 2), min(len(original), idx + 3)))

    if stale:
        snippet_lines = []
        for idx in sorted(snippet_idxs):
            ln = idx + 1
            tag = _compute_line_hash(ln, original[idx])
            marker = ">>>" if any(r.startswith(f"{ln}#") for r, _ in stale) else "   "
            snippet_lines.append(f"{marker} {ln}#{tag}| {original[idx]}")
        raise _MismatchError(stale, "\n".join(snippet_lines))

    # ── 2. Sort bottom-up so earlier indices stay valid after each splice ─────
    def _sort_key(e: dict) -> int:
        pos = e.get("pos")
        return -_parse_ref(pos)[0] if pos else 0

    for edit in sorted(edits, key=_sort_key):
        op = edit["op"]

        if op == "replace":
            start_no, _ = _parse_ref(edit["pos"])
            start_idx = start_no - 1
            if edit.get("end_pos"):
                end_no, _ = _parse_ref(edit["end_pos"])
                end_idx = end_no          # exclusive slice end
            else:
                end_idx = start_idx + 1
            original[start_idx:end_idx] = edit.get("lines") or []

        elif op == "delete":
            line_no, _ = _parse_ref(edit["pos"])
            del original[line_no - 1]

        elif op == "append":
            if edit.get("pos"):
                line_no, _ = _parse_ref(edit["pos"])
                insert_at = line_no       # after this line (0-based: line_no)
            else:
                insert_at = len(original)
            for i, new_line in enumerate(edit.get("lines") or []):
                original.insert(insert_at + i, new_line)

        elif op == "prepend":
            if edit.get("pos"):
                line_no, _ = _parse_ref(edit["pos"])
                insert_at = line_no - 1   # before this line
            else:
                insert_at = 0
            for i, new_line in enumerate(edit.get("lines") or []):
                original.insert(insert_at + i, new_line)

        else:
            raise ValueError(f"Unknown op '{op}'. Must be replace | delete | append | prepend.")

    new_content = "\n".join(original)

    # ── 3. Atomic write via temp file + os.replace ────────────────────────────
    if not dry_run:
        dir_ = resolved.parent
        fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".hashed_edit_tmp_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_content)
            os.replace(tmp_path, resolved)   # atomic on POSIX; best-effort on Windows
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    return new_content


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("hashed")


# ── Tool schemas ─────────────────────────────────────────────────────────────

HASHED_READ_TOOL = Tool(
    name="read",
    description=(
        "Read a file and return its content with LINE#ID hash annotations.\n\n"
        "Each line is prefixed:   LINE_NO#HASH| <content>\n\n"
        "    42#VK| def process(data):\n"
        "    43#XJ|     return transform(data)\n\n"
        "Always use this tool instead of built-in `read` when you plan to edit "
        "the file afterwards.  Pass the LINE#IDs directly to `hashed_edit`.\n\n"
        "IDs go stale the moment the file changes — re-read after every write."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file (absolute or relative to cwd).",
            },
            "start_line": {
                "type": "integer",
                "description": "First line to return (1-based, inclusive). Default: 1.",
                "minimum": 1,
            },
            "end_line": {
                "type": "integer",
                "description": "Last line to return (1-based, inclusive). Default: EOF.",
                "minimum": 1,
            },
            "hash_algo": {
                "type": "string",
                "enum": ["sha256", "sha1", "md5"],
                "description": "Hash algorithm for LINE#IDs. Default: sha256.",
                "default": "sha256",
            },
        },
        "required": ["path"],
    },
)

HASHED_EDIT_TOOL = Tool(
    name="edit",
    description=(
        "Apply hash-validated edits to a file.\n\n"
        "Every edit references a LINE#ID obtained from `hashed_read`.  All IDs "
        "are validated before a single byte is written.  If any ID is stale the "
        "entire operation is rejected and you receive the corrected IDs — simply "
        "retry with the updated references.\n\n"
        "Always use this tool instead of the built-in `write`/`edit` when "
        "working with hash-annotated files.\n\n"
        "Operations\n"
        "----------\n"
        "replace   – replace line at `pos` (or range `pos`..`end_pos`) with `lines`\n"
        "delete    – remove the line at `pos`\n"
        "append    – insert `lines` AFTER `pos`\n"
        "prepend   – insert `lines` BEFORE `pos`\n\n"
        "Multiple edits are applied in a single atomic write (bottom-up order "
        "is handled automatically)."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to edit.",
            },
            "edits": {
                "type": "array",
                "description": "List of edit operations to apply.",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": ["replace", "delete", "append", "prepend"],
                            "description": "Edit operation.",
                        },
                        "pos": {
                            "type": "string",
                            "description": "LINE#ID of the target line, e.g. '42#VK'.",
                        },
                        "end_pos": {
                            "type": "string",
                            "description": "LINE#ID of the last line in a range (replace-range only).",
                        },
                        "lines": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Replacement / insertion lines (not needed for delete).",
                        },
                    },
                    "required": ["op", "pos"],
                },
                "minItems": 1,
            },
            "dry_run": {
                "type": "boolean",
                "description": "Validate only — do not write to disk. Default: false.",
                "default": False,
            },
        },
        "required": ["path", "edits"],
    },
)


# ── Tool listing ──────────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [HASHED_READ_TOOL, HASHED_EDIT_TOOL]


# ── Tool execution ────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:

    # ── hashed_read ───────────────────────────────────────────────────────────
    if name == "read":
        path  = arguments.get("path")
        start = arguments.get("start_line")
        end   = arguments.get("end_line")
        algo  = arguments.get("hash_algo", "sha256")

        if not path:
            return [TextContent(type="text", text="Error: 'path' is required")]
        if algo not in _ALGO_MAP:
            return [TextContent(type="text", text=f"Error: unsupported hash_algo '{algo}'")]

        try:
            result = _hashed_read(path, start_line=start, end_line=end, algo=algo)
        except (FileNotFoundError, IsADirectoryError, ValueError) as exc:
            return [TextContent(type="text", text=f"Error: {exc}")]

        if result["start_line"] != 1 or result["end_line"] != result["total_lines"]:
            range_note = (
                f"Lines {result['start_line']}–{result['end_line']} "
                f"of {result['total_lines']} (algo={result['algo']})"
            )
        else:
            range_note = f"{result['total_lines']} lines total (algo={result['algo']})"

        text = f"File: {result['path']}\n{range_note}\n\n{result['content']}"
        return [TextContent(type="text", text=text)]

    # ── hashed_edit ───────────────────────────────────────────────────────────
    elif name == "edit":
        path    = arguments.get("path")
        edits   = arguments.get("edits")
        dry_run = bool(arguments.get("dry_run", False))

        if not path:
            return [TextContent(type="text", text="Error: 'path' is required")]
        if not edits:
            return [TextContent(type="text", text="Error: 'edits' must be a non-empty list")]

        try:
            new_content = _hashed_edit(path, edits, dry_run=dry_run)
        except _MismatchError as exc:
            # Structured feedback — the LLM can retry with the corrected IDs
            return [TextContent(type="text", text=f"HashlineMismatch:\n{exc}")]
        except (FileNotFoundError, ValueError) as exc:
            return [TextContent(type="text", text=f"Error: {exc}")]

        line_count = new_content.count("\n") + 1
        action = "Validated (dry_run)" if dry_run else "Written"
        summary = (
            f"{action}: {Path(path).resolve()}\n"
            f"{len(edits)} edit(s) applied — file now {line_count} lines."
        )
        return [TextContent(type="text", text=summary)]

    else:
        raise ValueError(f"Unknown tool: '{name}'")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(_main())