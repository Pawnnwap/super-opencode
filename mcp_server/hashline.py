"""hashline.py  –  MCP server exposing `read`, `edit`, `write` (server: hashline)
================================================================================

opencode sees: hashline_read / hashline_edit / hashline_write

    read(path, start_line?, end_line?)
    edit(path, edits, dry_run?, auto_retry?, autofix?)
    write(path, lines, overwrite?, autofix?)

── read ───────────────────────────────────────────────────────────────────────
Annotates every line with a LINE#ID position anchor:

    42#VKB| def process(data):
    43#XJZ|     return transform(data)

── edit ───────────────────────────────────────────────────────────────────────
Validates all LINE#IDs in one upfront pass, then writes atomically.
Nothing is written if any ID is stale.

auto_retry=true (default): stale refs are auto-patched and re-applied server-
side in the same call — no round-trip needed.

auto_retry=false: returns retry_edits with corrected refs for manual retry.

Edit ops: replace, replace_range, delete, append, prepend.

autofix=true: after writing, runs safe style/security auto-fixers on the file.
Skips undefined/unused fixers (autoflake, deadcode, ruff unused-import rules)
because hashline edit only touches specific sections, not the whole codebase.

── write ──────────────────────────────────────────────────────────────────────
Create a new file from a list of lines. Refuses to overwrite an existing file
unless overwrite=true is explicitly passed. Parent directories are created
automatically. No LINE#IDs needed — this is for new files only.

autofix=true: after writing, runs safe style/security auto-fixers on the file.
Skips undefined/unused fixers for the same reason as edit.

── autofix behaviour ──────────────────────────────────────────────────────────
Fixers that RUN (safe for partial/new files):
  • isort      — sorts and deduplicates imports
  • autopep8   — PEP8 whitespace / line-length / blank lines
  • pyupgrade  — modernises old Python syntax (f-strings, typing, etc.)
  • ruff       — style + security fixes only (UP/SIM/TRY/S/PTH/RUF/…)
                 explicitly excludes F401 (unused imports) and F841
                 (unused variables), which require whole-codebase context

Fixers that are SKIPPED (require whole-codebase context):
  • autoflake  — removes unused imports & variables   → may break callers
  • deadcode   — removes globally-unused functions    → needs full call graph

Usage
-----
    python hashline.py

Dependencies
------------
    pip install mcp
    pip install isort autopep8 pyupgrade ruff   # optional, for autofix
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
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
# Hash logic  (sha256 hardcoded — mixing algos across read/edit breaks refs)
# ---------------------------------------------------------------------------

_CHARSET = "ZPMQVRWSNKTXJBYH"
_ALGO = "sha256"
_HASH_CHARS = 3   # 4096 possible IDs — negligible collision risk


def _compute_line_hash(line_number: int, content: str) -> str:
    raw = f"{line_number}:{content}"
    digest = hashlib.new(_ALGO, raw.encode()).digest()
    return "".join(_CHARSET[digest[i] & 0x0F] for i in range(_HASH_CHARS))


def _format_tagged_line(line_number: int, content: str) -> str:
    return f"{line_number}#{_compute_line_hash(line_number, content)}| {content}"


def _parse_ref(ref: str) -> tuple[int, str]:
    """Parse '42#VKB' -> (42, 'VKB').  Raises ValueError on bad format."""
    ref = ref.strip()
    parts = ref.split("#", 1)
    if len(parts) != 2 or not parts[0].isdigit() or len(parts[1]) != _HASH_CHARS:
        raise ValueError(f"Invalid LINE#ID '{ref}': expected '<line_no>#<{_HASH_CHARS}-char-id>'")
    return int(parts[0]), parts[1]


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def _hashline_read(
    path: str | Path,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, Any]:
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {resolved}")
    if not resolved.is_file():
        raise IsADirectoryError(f"Path is a directory: {resolved}")

    all_lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(all_lines)

    s = max(1, start_line) if start_line is not None else 1
    e = min(total, end_line) if end_line is not None else total

    if s > total:
        raise ValueError(f"start_line={s} exceeds file length ({total}): {resolved}")

    return {
        "path": str(resolved),
        "total_lines": total,
        "start_line": s,
        "end_line": e,
        "content": "\n".join(_format_tagged_line(ln, all_lines[ln - 1]) for ln in range(s, e + 1)),
    }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_all_refs(
    edits: list[dict[str, Any]],
    lines: list[str],
) -> list[dict[str, str]]:
    """Collect ALL stale refs before touching anything.

    Returns list of stale-ref dicts (empty = all valid):
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
            expected = _compute_line_hash(line_no, lines[idx])
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
    """Return edits with stale refs replaced by their current IDs."""
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
    """Detect overlapping edits. Returns conflict descriptions (empty = ok)."""
    conflicts: list[str] = []
    ranges: list[tuple[int, int, str]] = []

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
                    f"'{edit['op']}' on lines {start}-{end} overlaps "
                    f"'{prev_op}' on lines {prev_start}-{prev_end}",
                )
        ranges.append((start, end, edit["op"]))

    return conflicts


# ---------------------------------------------------------------------------
# Edit (core)
# ---------------------------------------------------------------------------

def _apply_edits(working: list[str], edits: list[dict[str, Any]]) -> None:
    """Apply edits in-place, bottom-up so earlier indices stay valid."""
    def _sort_key(e: dict) -> int:
        pos = e.get("pos")
        return -_parse_ref(pos)[0] if pos else 0

    for edit in sorted(edits, key=_sort_key):
        op = edit["op"]

        if op in ("replace", "replace_range"):
            start_no, _ = _parse_ref(edit["pos"])
            start_idx = start_no - 1
            if edit.get("end_pos"):
                end_no, _ = _parse_ref(edit["end_pos"])
                # end_pos is INCLUSIVE. Because line numbers are 1-based and start_idx = start_no - 1,
                # the slice working[start_idx:end_no] naturally covers lines start_no..end_no inclusive.
                # Example: replace lines 3-5 -> working[2:5] -> indices 2,3,4 (lines 3,4,5) ✓
                # Do NOT change end_no to end_no+1 — that would make it exclusive and skip the last line.
                end_idx = end_no
            else:
                end_idx = start_idx + 1
            working[start_idx:end_idx] = edit.get("lines") or []

        elif op == "delete":
            line_no, _ = _parse_ref(edit["pos"])
            del working[line_no - 1]

        elif op == "append":
            line_no, _ = _parse_ref(edit["pos"])
            insert_at = line_no   # 0-based index after this line
            for i, new_line in enumerate(edit.get("lines") or []):
                working.insert(insert_at + i, new_line)

        elif op == "prepend":
            line_no, _ = _parse_ref(edit["pos"])
            insert_at = line_no - 1
            for i, new_line in enumerate(edit.get("lines") or []):
                working.insert(insert_at + i, new_line)

        else:
            raise ValueError(
                f"Unknown op '{op}'. Must be: replace | replace_range | delete | append | prepend",
            )


def _write_atomic(resolved: Path, content: str, *, exclusive: bool = False) -> None:
    """Write content via temp file + os.replace (atomic on POSIX).
    
    If exclusive=True, fails if the destination file already exists.
    """
    fd, tmp_path = tempfile.mkstemp(dir=resolved.parent, prefix=".hashline_tmp_")
    os.close(fd)  # close raw fd; write via Path.write_text instead
    try:
        Path(tmp_path).write_text(content, encoding="utf-8")
        if exclusive:
            # O_CREAT|O_EXCL is atomic — raises FileExistsError if target exists
            dest_fd = os.open(str(resolved), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(dest_fd)
            os.unlink(str(resolved))
        os.replace(tmp_path, resolved)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _compact_diff(original: list[str], new: list[str], context: int = 3) -> str:
    return "\n".join(difflib.unified_diff(
        original, new, fromfile="before", tofile="after", lineterm="", n=context,
    ))


# ---------------------------------------------------------------------------
# Auto-fix integration
# ---------------------------------------------------------------------------
# Fixers that are SAFE to run on a partial/new file (no whole-codebase context
# needed).  Unused-import / unused-variable / dead-code fixers are deliberately
# excluded: hashline operates on specific sections; removing an "unused" symbol
# from a fragment may break the rest of the codebase.
# ---------------------------------------------------------------------------

_AUTOFIX_TOOL_TIMEOUT = 30  # seconds per fixer


def _run_fix(cmd: list[str]) -> tuple[str, str, int]:
    """Run a subprocess and return (stdout, stderr, returncode)."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_AUTOFIX_TOOL_TIMEOUT,
        )
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", f"timed out after {_AUTOFIX_TOOL_TIMEOUT}s", 1
    except Exception as exc:
        return "", str(exc), 1


def _ensure_fix_tool(package: str, binary: str) -> bool:
    """Return True if binary is on PATH, otherwise try pip-installing it."""
    if shutil.which(binary):
        return True
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", package, "-q"],
            capture_output=True,
            timeout=60,
        )
        return result.returncode == 0
    except Exception:
        return False


@dataclass
class _FixSummary:
    """Lightweight record of what each fixer did."""
    tool: str
    ok: bool
    note: str = ""


def _autofix_isort(filepath: str) -> _FixSummary:
    """Sort and deduplicate imports in a single file."""
    if not _ensure_fix_tool("isort", "isort"):
        return _FixSummary("isort", ok=False, note="not installed")
    _, stderr, rc = _run_fix(["isort", "--quiet", filepath])
    return _FixSummary("isort", ok=(rc == 0), note=stderr.strip() if rc != 0 else "")


def _autofix_autopep8(filepath: str) -> _FixSummary:
    """Fix PEP8 whitespace / line-length / blank-line issues."""
    if not _ensure_fix_tool("autopep8", "autopep8"):
        return _FixSummary("autopep8", ok=False, note="not installed")
    safe_fixes = "E1,E2,E3,E4,W1,W2,W3,W6"
    _, stderr, rc = _run_fix([
        "autopep8", "--in-place",
        f"--select={safe_fixes}",
        "--max-line-length=88",
        filepath,
    ])
    return _FixSummary("autopep8", ok=(rc == 0), note=stderr.strip() if rc != 0 else "")


def _autofix_pyupgrade(filepath: str) -> _FixSummary:
    """Upgrade legacy Python syntax to modern equivalents."""
    if not _ensure_fix_tool("pyupgrade", "pyupgrade"):
        return _FixSummary("pyupgrade", ok=False, note="not installed")
    _, stderr, rc = _run_fix(["pyupgrade", "--py38-plus", filepath])
    # pyupgrade exits 1 when it modifies a file — that is success
    ok = rc in (0, 1)
    return _FixSummary("pyupgrade", ok=ok, note=stderr.strip() if not ok else "")


def _autofix_ruff(filepath: str) -> _FixSummary:
    """Apply Ruff's safe style + security auto-fixes.

    Explicitly excluded rule families (require whole-codebase context):
      F401  — unused imports
      F841  — unused local variables
      F811  — redefinition of unused name
      ERA   — commented-out code  (too aggressive on partial edits)

    All other fixable rules (UP, SIM, S, TRY, PTH, RUF, …) are applied.
    """
    if not _ensure_fix_tool("ruff", "ruff"):
        return _FixSummary("ruff", ok=False, note="not installed")
    _, stderr, rc = _run_fix([
        "ruff", "check",
        "--fix-only",
        "--select=ALL",
        # Skip rules that need whole-codebase context or are too aggressive on partials
        "--ignore=F401,F811,F841,ERA,E,W,B,C4,ISC,N",
        filepath,
    ])
    ok = rc in (0, 1)
    return _FixSummary("ruff", ok=ok, note=stderr.strip() if not ok else "")


# Ordered list — applied in this sequence after every write/edit.
_PARTIAL_FIXERS: list[tuple[str, Any]] = [
    ("isort",     _autofix_isort),
    ("autopep8",  _autofix_autopep8),
    ("pyupgrade", _autofix_pyupgrade),
    ("ruff",      _autofix_ruff),
]

# Documented explanation surfaced in the autofix result payload.
_SKIPPED_FIXERS_REASON = (
    "autoflake (unused imports/vars) and deadcode (unused functions/classes) are "
    "skipped: hashline operates on specific sections, not the whole codebase. "
    "Removing 'unused' symbols from a fragment may break callers elsewhere."
)


def _run_autofix(filepath: str) -> list[dict[str, Any]]:
    """Run all partial-safe fixers on filepath. Returns a per-tool summary list."""
    results = []
    for label, runner in _PARTIAL_FIXERS:
        try:
            summary = runner(filepath)
            results.append({
                "tool": summary.tool,
                "status": "ok" if summary.ok else "error",
                **({"note": summary.note} if summary.note else {}),
            })
        except Exception as exc:
            logger.warning("Autofix tool %r raised unexpectedly: %s", label, exc)
            results.append({"tool": label, "status": "error", "note": str(exc)})
    return results


# ---------------------------------------------------------------------------
# Write (new files)
# ---------------------------------------------------------------------------

def _hashline_write(
    path: str | Path,
    lines: list[str],
    *,
    overwrite: bool = False,
    autofix: bool = False,
) -> dict[str, Any]:
    resolved = Path(path).resolve()

    if resolved.exists() and not overwrite:
        raise FileExistsError(
            f"File already exists: {resolved}  —  pass overwrite=true to replace it, "
            "or use `hashline edit` to modify it in place.",
        )

    resolved.parent.mkdir(parents=True, exist_ok=True)
    status = "overwritten" if resolved.exists() else "created"

    normalized = [ln.rstrip("\r\n") for ln in lines]
    content = "\n".join(normalized)
    if normalized:
        content += "\n"

    _write_atomic(resolved, content, exclusive=not overwrite)

    result: dict[str, Any] = {
        "status": status,
        "path": str(resolved),
        "lines": len(lines),
    }

    if autofix:
        fix_results = _run_autofix(str(resolved))
        result["autofix"] = {
            "applied": fix_results,
            "skipped": _SKIPPED_FIXERS_REASON,
        }

    return result


def _hashline_edit(
    path: str | Path,
    edits: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    auto_retry: bool = True,
    autofix: bool = False,
) -> dict[str, Any]:
    """Validate LINE#IDs, apply edits, write atomically.

    auto_retry=True  -> stale refs are auto-patched and re-applied server-side.
    auto_retry=False -> raises _MismatchError with retry_edits payload.

    Parameters
    ----------
    autofix : bool
        When True, runs safe style/security fixers (isort, autopep8,
        pyupgrade, ruff) after the edit is written.  Skipped for dry_run.
        Unused-import and dead-code fixers are always skipped — they require
        whole-codebase context that hashline edit does not have.

    Returns
    -------
    status   : "written" | "dry_run" | "auto_retried"
    patches  : number of refs auto-corrected (only when auto_retried)
    diff     : unified diff string (key omitted if no changes)
    autofix  : dict with "applied" list and "skipped" reason (when autofix=True and not dry_run)
    """
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {resolved}")

    raw_text = resolved.read_text(encoding="utf-8", errors="replace")
    had_trailing_newline = raw_text.endswith("\n")
    original = raw_text.splitlines()

    # 1. Conflict detection
    conflicts = _check_edit_conflicts(edits)
    if conflicts:
        raise ValueError(
            "Edit conflict(s) detected - nothing written:\n"
            + "\n".join(f"  * {c}" for c in conflicts),
        )

    # 2. Validate refs
    stale = _validate_all_refs(edits, original)
    auto_retried = False

    if stale:
        if not auto_retry:
            # Build snippet around affected lines for manual debugging
            affected: set[int] = set()
            for s in stale:
                try:
                    ln = _parse_ref(s["provided"])[0]
                    affected.update(range(max(1, ln - 2), min(len(original), ln + 3) + 1))
                except ValueError:
                    pass

            snippet_lines: list[str] = []
            for ln in sorted(affected):
                idx = ln - 1
                if 0 <= idx < len(original):
                    tag = _compute_line_hash(ln, original[idx])
                    marker = ">>>" if any(s["provided"].startswith(f"{ln}#") for s in stale) else "   "
                    snippet_lines.append(f"{marker} {ln}#{tag}| {original[idx]}")

            raise _MismatchError(stale, _auto_patch_edits(edits, stale), "\n".join(snippet_lines))

        # auto_retry=True: patch and continue
        edits = _auto_patch_edits(edits, stale)
        auto_retried = True

    # 3. Apply edits
    working = list(original)
    _apply_edits(working, edits)

    # 4. Preserve trailing newline
    new_content = "\n".join(working)
    if had_trailing_newline:
        new_content += "\n"

    # 5. Diff
    diff = _compact_diff(original, working)

    # 6. Write
    if not dry_run:
        _write_atomic(resolved, new_content)

    result: dict[str, Any] = {
        "status": "dry_run" if dry_run else ("auto_retried" if auto_retried else "written"),
    }
    if auto_retried:
        result["patches"] = len(stale)
    if diff:
        result["diff"] = diff

    # 7. Autofix (skipped for dry_run — nothing was written to disk)
    if autofix and not dry_run:
        fix_results = _run_autofix(str(resolved))
        result["autofix"] = {
            "applied": fix_results,
            "skipped": _SKIPPED_FIXERS_REASON,
        }

    return result


# ---------------------------------------------------------------------------
# _MismatchError  (only raised when auto_retry=False)
# ---------------------------------------------------------------------------

class _MismatchError(Exception):
    def __init__(
        self,
        stale_refs: list[dict[str, str]],
        retry_edits: list[dict[str, Any]],
        snippet: str,
    ):
        self.stale_refs = stale_refs
        self.retry_edits = retry_edits
        self.snippet = snippet
        super().__init__(json.dumps({
            "error": "HashlineMismatch",
            "description": f"{len(stale_refs)} stale LINE#ID(s) - nothing written.",
            "stale_refs": stale_refs,
            "retry_edits": retry_edits,
            "snippet": snippet,
        }, indent=2))


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server = Server("hashline")


HASHLINE_READ_TOOL = Tool(
    name="read",
    description=(
        "Read a file with LINE#ID annotations required for safe editing.\n\n"
        "PREFER THIS OVER built-in `read` when the file will be edited — "
        "without LINE#IDs, `hashline edit` rejects every operation.\n\n"
        "Format:  42#VKB| def process(data):\n"
        "         43#XJZ|     return transform(data)\n\n"
        "Pass LINE#IDs directly to `hashline edit`. "
        "IDs go stale on every write — re-read after each edit.\n\n"
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
        "PREFER THIS OVER built-in `write`/`edit` — validates atomically before "
        "touching disk; a failed write never corrupts the file.\n\n"
        "auto_retry=true (default): stale refs are auto-patched and re-applied "
        "server-side in the same call. No extra round-trip needed.\n\n"
        "auto_retry=false: returns retry_edits with corrected refs on mismatch.\n\n"
        "autofix=true: after writing, runs safe style/security fixers (isort, "
        "autopep8, pyupgrade, ruff) on the edited file. Unused-import and dead-code "
        "fixers are intentionally skipped because hashline edit only touches specific "
        "sections — removing 'unused' symbols from a fragment may break callers "
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
                                "  replace       — Replace a single line at pos with lines[].\n"
                                "  replace_range — Replace lines from pos to end_pos (BOTH INCLUSIVE) with lines[]. "
                                "Use this to swap out an entire block; do NOT use append/prepend for block replacement "
                                "or the old block will remain as a duplicate.\n"
                                "  delete        — Delete the single line at pos.\n"
                                "  append        — Insert lines[] AFTER pos (does not remove pos).\n"
                                "  prepend       — Insert lines[] BEFORE pos (does not remove pos)."
                            ),
                        },
                        "pos": {"type": "string", "description": "LINE#ID of target line, e.g. '42#VKB'."},
                        "end_pos": {
                            "type": "string",
                            "description": (
                                "LINE#ID of the LAST line to replace — INCLUSIVE. "
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
                    "the edit re-applied server-side — no round-trip needed. "
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
                    "always excluded — they need whole-codebase context. Default: false."
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
        "USE THIS instead of built-in `write` for creating new files — "
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
                    "— they need whole-codebase context. Default: false."
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

    # ── read ────────────────────────────────────────────────────────────────────
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

        s, e, total = result["start_line"], result["end_line"], result["total_lines"]
        range_note = f"Lines {s}-{e} of {total}" if (s != 1 or e != total) else f"{total} lines"
        return [TextContent(type="text", text=f"File: {result['path']} ({range_note})\n\n{result['content']}")]

    # ── edit ────────────────────────────────────────────────────────────────────
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
                path, edits,
                dry_run=dry_run,
                auto_retry=auto_retry,
                autofix=autofix,
            )
        except _MismatchError as exc:
            return [TextContent(type="text", text=str(exc))]
        except (FileNotFoundError, ValueError) as exc:
            return [TextContent(type="text", text=f"Error: {exc}")]

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    # ── write ───────────────────────────────────────────────────────────────────
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(_main())