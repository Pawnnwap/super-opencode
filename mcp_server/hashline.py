"""hashline.py  –  MCP server exposing `hashline_read` and `hashline_edit`
================================================================================

Exposes two custom tools (server name = "hashline", so opencode sees):

    hashline_read(path, start_line?, end_line?, hash_algo?)
    hashline_edit(path, edits, dry_run?)

── hashline_read ──────────────────────────────────────────────────────────────
Adds LINE#ID annotations to each line of the file.

    42#VKB| def process(data):
    43#XJZ|     return transform(data)

Use instead of the built-in `read` whenever you intend to edit afterwards.

── hashline_edit ──────────────────────────────────────────────────────────────
Validates LINE#ID references and writes the edited file to disk atomically.
Rejects the entire operation (nothing written) if any ID is stale.

Returns structured JSON feedback on mismatch so opencode can auto-patch refs
and retry immediately without a manual re-read.

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

import difflib
import hashlib
import json
import os
import sys
import tempfile
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
        f"Original error: {exc}",
    )

# ---------------------------------------------------------------------------
# Hash logic
# ---------------------------------------------------------------------------

_CHARSET = "ZPMQVRWSNKTXJBYH"

_ALGO_MAP: dict[str, str] = {
    "sha256": "sha256",
    "sha1": "sha1",
    "md5": "md5",
}

# 3-char IDs → 4096 possible values (vs 256 with 2-char), negligible collision risk
_HASH_CHARS = 3


def _compute_line_hash(line_number: int, content: str, algo: str = "sha256") -> str:
    raw = f"{line_number}:{content}"
    h = hashlib.new(_ALGO_MAP.get(algo, "sha256"), raw.encode())
    digest = h.digest()
    return "".join(_CHARSET[digest[i] & 0x0F] for i in range(_HASH_CHARS))


def _format_tagged_line(line_number: int, content: str, algo: str = "sha256") -> str:
    tag = _compute_line_hash(line_number, content, algo)
    return f"{line_number}#{tag}| {content}"


def _parse_ref(ref: str) -> tuple[int, str]:
    """Parse '42#VKB' → (42, 'VKB').  Raises ValueError on bad format."""
    ref = ref.strip()
    if "#" not in ref:
        raise ValueError(f"Invalid LINE#ID '{ref}': expected '<line_no>#<{_HASH_CHARS}-char-hash>'")
    parts = ref.split("#", 1)
    if not parts[0].isdigit() or len(parts[1]) != _HASH_CHARS:
        raise ValueError(f"Invalid LINE#ID '{ref}': expected '<line_no>#<{_HASH_CHARS}-char-hash>'")
    return int(parts[0]), parts[1]


# ---------------------------------------------------------------------------
# hashline_read logic
# ---------------------------------------------------------------------------

def _hashline_read(
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

    raw_text = resolved.read_text(encoding="utf-8", errors="replace")
    all_lines = raw_text.splitlines()
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
        "path": str(resolved),
        "total_lines": total,
        "start_line": s,
        "end_line": e,
        "algo": algo,
        "content": "\n".join(annotated),
    }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_all_refs(
    edits: list[dict[str, Any]],
    lines: list[str],
    algo: str = "sha256",
) -> list[dict[str, str]]:
    """Complete upfront validation pass — collects ALL stale refs before any edit
    is applied so opencode gets a full picture in a single response.

    Returns a list of structured stale-ref dicts (empty = all valid):
        [{"edit_op": "replace", "provided": "42#VKB", "current": "42#XJZ",
          "line_content": "def process(data):"}]
    """
    stale: list[dict[str, str]] = []
    for edit in edits:
        for attr in ("pos", "end_pos"):
            ref = edit.get(attr)
            if ref is None:
                continue
            line_no, given_hash = _parse_ref(ref)
            idx = line_no - 1
            if idx < 0 or idx >= len(lines):
                stale.append({
                    "edit_op": edit["op"],
                    "provided": ref,
                    "current": f"<line {line_no} out of range>",
                    "line_content": "",
                })
                continue
            expected = _compute_line_hash(line_no, lines[idx], algo)
            if given_hash != expected:
                stale.append({
                    "edit_op": edit["op"],
                    "provided": ref,
                    "current": f"{line_no}#{expected}",
                    "line_content": lines[idx],
                })
    return stale


def _auto_patch_edits(
    edits: list[dict[str, Any]],
    stale: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Return a copy of `edits` with stale refs replaced by their current IDs.
    This is included in the error payload so opencode can retry without
    a manual re-read.
    """
    correction_map = {s["provided"]: s["current"] for s in stale}

    patched = []
    for edit in edits:
        e = dict(edit)
        if e.get("pos") in correction_map:
            e["pos"] = correction_map[e["pos"]]
        if e.get("end_pos") in correction_map:
            e["end_pos"] = correction_map[e["end_pos"]]
        patched.append(e)
    return patched


def _check_edit_conflicts(edits: list[dict[str, Any]]) -> list[str]:
    """Detect overlapping edits (e.g. replace and delete on the same line).
    Returns a list of human-readable conflict descriptions (empty = no conflicts).
    """
    conflicts: list[str] = []
    ranges: list[tuple[int, int, str]] = []   # (start, end, op)

    for edit in edits:
        pos_ref = edit.get("pos")
        if not pos_ref:
            continue
        start = _parse_ref(pos_ref)[0]
        end_ref = edit.get("end_pos")
        end = _parse_ref(end_ref)[0] if end_ref else start

        for prev_start, prev_end, prev_op in ranges:
            if not (end < prev_start or start > prev_end):
                conflicts.append(
                    f"'{edit['op']}' on lines {start}–{end} overlaps "
                    f"'{prev_op}' on lines {prev_start}–{prev_end}",
                )
        ranges.append((start, end, edit["op"]))

    return conflicts


# ---------------------------------------------------------------------------
# hashline_edit logic
# ---------------------------------------------------------------------------

class _MismatchError(Exception):
    """Stale LINE#ID references detected.

    Carries:
      .stale_refs   – structured list for machine consumption
      .retry_edits  – original edits with refs auto-corrected (ready to retry)
      .snippet      – annotated text around affected lines (human-readable)
    """

    def __init__(
        self,
        stale_refs: list[dict[str, str]],
        retry_edits: list[dict[str, Any]],
        snippet: str,
    ):
        self.stale_refs = stale_refs
        self.retry_edits = retry_edits
        self.snippet = snippet
        super().__init__(self._build())

    def _build(self) -> str:
        return json.dumps({
            "error": "HashlineMismatch",
            "description": (
                f"{len(self.stale_refs)} stale LINE#ID reference(s) — "
                "edit rejected, nothing written."
            ),
            "stale_refs": self.stale_refs,
            "retry_edits": self.retry_edits,
            "snippet": self.snippet,
        }, indent=2)


def _compact_diff(original_lines: list[str], new_lines: list[str], context: int = 3) -> str:
    """Return a unified diff string for post-edit verification."""
    return "\n".join(difflib.unified_diff(
        original_lines,
        new_lines,
        fromfile="before",
        tofile="after",
        lineterm="",
        n=context,
    ))


def _hashline_edit(
    path: str | Path,
    edits: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate all LINE#ID references, then apply edits atomically.

    Each edit dict:
        op       : "replace" | "delete" | "append" | "prepend"
        pos      : "42#VKB"            (required for all ops)
        end_pos  : "45#XJZ"            (replace-range only)
        lines    : ["new line", ...]   (replace / append / prepend)

    Returns a result dict containing:
        written      : bool
        line_count   : int
        diff         : str   (unified diff for immediate verification)

    Raises _MismatchError if any ID is stale (nothing written).
    Raises ValueError on edit conflicts or unknown ops.
    Writes via a temp file + rename so a failed mid-write never corrupts disk.
    """
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {resolved}")

    raw_text = resolved.read_text(encoding="utf-8", errors="replace")
    had_trailing_newline = raw_text.endswith("\n")
    original = raw_text.splitlines()

    # ── 1. Conflict detection ─────────────────────────────────────────────────
    conflicts = _check_edit_conflicts(edits)
    if conflicts:
        raise ValueError(
            "Edit conflict(s) detected — nothing written:\n"
            + "\n".join(f"  • {c}" for c in conflicts),
        )

    # ── 2. Complete upfront validation pass ───────────────────────────────────
    stale = _validate_all_refs(edits, original)

    if stale:
        # Build annotated snippet around all affected lines
        affected_line_nos: set[int] = set()
        for s in stale:
            try:
                ln = _parse_ref(s["provided"])[0]
                affected_line_nos.update(range(max(1, ln - 2), min(len(original), ln + 3) + 1))
            except ValueError:
                pass

        snippet_lines: list[str] = []
        for ln in sorted(affected_line_nos):
            idx = ln - 1
            if 0 <= idx < len(original):
                tag = _compute_line_hash(ln, original[idx])
                is_bad = any(s["provided"].startswith(f"{ln}#") for s in stale)
                marker = ">>>" if is_bad else "   "
                snippet_lines.append(f"{marker} {ln}#{tag}| {original[idx]}")

        retry_edits = _auto_patch_edits(edits, stale)
        raise _MismatchError(stale, retry_edits, "\n".join(snippet_lines))

    # ── 3. Sort bottom-up so earlier indices stay valid after each splice ─────
    def _sort_key(e: dict) -> int:
        pos = e.get("pos")
        return -_parse_ref(pos)[0] if pos else 0

    working = list(original)   # copy we will mutate

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
            working[start_idx:end_idx] = edit.get("lines") or []

        elif op == "delete":
            line_no, _ = _parse_ref(edit["pos"])
            del working[line_no - 1]

        elif op == "append":
            if edit.get("pos"):
                line_no, _ = _parse_ref(edit["pos"])
                insert_at = line_no       # after this line (0-based: line_no)
            else:
                insert_at = len(working)
            for i, new_line in enumerate(edit.get("lines") or []):
                working.insert(insert_at + i, new_line)

        elif op == "prepend":
            if edit.get("pos"):
                line_no, _ = _parse_ref(edit["pos"])
                insert_at = line_no - 1   # before this line
            else:
                insert_at = 0
            for i, new_line in enumerate(edit.get("lines") or []):
                working.insert(insert_at + i, new_line)

        else:
            raise ValueError(f"Unknown op '{op}'. Must be replace | delete | append | prepend.")

    # ── 4. Preserve trailing newline ──────────────────────────────────────────
    new_content = "\n".join(working)
    if had_trailing_newline:
        new_content += "\n"

    # ── 5. Build diff before writing ──────────────────────────────────────────
    diff = _compact_diff(original, working)

    # ── 6. Atomic write via temp file + os.replace ────────────────────────────
    if not dry_run:
        dir_ = resolved.parent
        fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".hashline_edit_tmp_")
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

    return {
        "written": not dry_run,
        "line_count": len(working),
        "diff": diff,
    }


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("hashline")


# ── Tool schemas ─────────────────────────────────────────────────────────────

HASHLINE_READ_TOOL = Tool(
    name="read",
    description=(
        "Read a file and return its content with LINE#ID hash annotations.\n\n"
        "Each line is prefixed:   LINE_NO#HASH| <content>\n\n"
        "    42#VKB| def process(data):\n"
        "    43#XJZ|     return transform(data)\n\n"
        "Always use this tool instead of built-in `read` when you plan to edit "
        "the file afterwards.  Pass the LINE#IDs directly to `hashline_edit`.\n\n"
        "IDs go stale the moment the file changes — re-read after every write.\n\n"
        "Note: IDs are 3 characters (4096 possible values) for low collision risk."
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

HASHLINE_EDIT_TOOL = Tool(
    name="edit",
    description=(
        "Apply hash-validated edits to a file.\n\n"
        "Every edit references a LINE#ID obtained from `hashline_read`.  All IDs "
        "are validated in a single upfront pass before a single byte is written.  "
        "If any ID is stale the entire operation is rejected and you receive:\n\n"
        "  • stale_refs   – structured list of provided vs current IDs\n"
        "  • retry_edits  – your original edits with refs auto-corrected (retry immediately)\n"
        "  • snippet      – annotated lines around affected area\n\n"
        "On success you receive a unified diff for immediate verification.\n\n"
        "Always use this tool instead of the built-in `write`/`edit` when "
        "working with hash-annotated files.\n\n"
        "Operations\n"
        "----------\n"
        "replace   – replace line at `pos` (or range `pos`..`end_pos`) with `lines`\n"
        "delete    – remove the line at `pos`\n"
        "append    – insert `lines` AFTER `pos`\n"
        "prepend   – insert `lines` BEFORE `pos`\n\n"
        "Multiple edits are applied in a single atomic write (bottom-up order "
        "is handled automatically).  Overlapping edits are detected and rejected."
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
                            "description": "LINE#ID of the target line, e.g. '42#VKB'.",
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
    return [HASHLINE_READ_TOOL, HASHLINE_EDIT_TOOL]


# ── Tool execution ────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:

    # ── hashline_read ───────────────────────────────────────────────────────────
    if name == "read":
        path = arguments.get("path")
        start = arguments.get("start_line")
        end = arguments.get("end_line")
        algo = arguments.get("hash_algo", "sha256")

        if not path:
            return [TextContent(type="text", text="Error: 'path' is required")]
        if algo not in _ALGO_MAP:
            return [TextContent(type="text", text=f"Error: unsupported hash_algo '{algo}'")]

        try:
            result = _hashline_read(path, start_line=start, end_line=end, algo=algo)
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

    # ── hashline_edit ───────────────────────────────────────────────────────────
    if name == "edit":
        path = arguments.get("path")
        edits = arguments.get("edits")
        dry_run = bool(arguments.get("dry_run", False))

        if not path:
            return [TextContent(type="text", text="Error: 'path' is required")]
        if not edits:
            return [TextContent(type="text", text="Error: 'edits' must be a non-empty list")]

        try:
            result = _hashline_edit(path, edits, dry_run=dry_run)
        except _MismatchError as exc:
            # Return structured JSON — opencode can auto-patch refs and retry
            return [TextContent(type="text", text=str(exc))]
        except (FileNotFoundError, ValueError) as exc:
            return [TextContent(type="text", text=f"Error: {exc}")]

        action = "Validated (dry_run)" if dry_run else "Written"
        summary_obj = {
            "status": action,
            "path": str(Path(path).resolve()),
            "edits": len(edits),
            "line_count": result["line_count"],
            "diff": result["diff"] or "(no changes)",
        }
        return [TextContent(type="text", text=json.dumps(summary_obj, indent=2))]

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
