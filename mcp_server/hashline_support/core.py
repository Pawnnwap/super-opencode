from __future__ import annotations

import difflib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .autofix import _SKIPPED_FIXERS_REASON, _run_autofix
from .refs import (
    _auto_patch_edits,
    _check_edit_conflicts,
    _compute_line_hash,
    _format_tagged_line,
    _parse_ref,
    _validate_all_refs,
)


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

    start = max(1, start_line) if start_line is not None else 1
    end = min(total, end_line) if end_line is not None else total

    if start > total:
        raise ValueError(f"start_line={start} exceeds file length ({total}): {resolved}")

    return {
        "path": str(resolved),
        "total_lines": total,
        "start_line": start,
        "end_line": end,
        "content": "\n".join(
            _format_tagged_line(line_no, all_lines[line_no - 1])
            for line_no in range(start, end + 1)
        ),
    }


def _apply_edits(working: list[str], edits: list[dict[str, Any]]) -> None:
    def _sort_key(edit: dict[str, Any]) -> int:
        pos = edit.get("pos")
        return -_parse_ref(pos)[0] if pos else 0

    for edit in sorted(edits, key=_sort_key):
        op = edit["op"]

        if op in ("replace", "replace_range"):
            start_no, _ = _parse_ref(edit["pos"])
            start_idx = start_no - 1
            if edit.get("end_pos"):
                end_no, _ = _parse_ref(edit["end_pos"])
                end_idx = end_no
            else:
                end_idx = start_idx + 1
            working[start_idx:end_idx] = edit.get("lines") or []
        elif op == "delete":
            line_no, _ = _parse_ref(edit["pos"])
            del working[line_no - 1]
        elif op == "append":
            line_no, _ = _parse_ref(edit["pos"])
            insert_at = line_no
            for idx, new_line in enumerate(edit.get("lines") or []):
                working.insert(insert_at + idx, new_line)
        elif op == "prepend":
            line_no, _ = _parse_ref(edit["pos"])
            insert_at = line_no - 1
            for idx, new_line in enumerate(edit.get("lines") or []):
                working.insert(insert_at + idx, new_line)
        else:
            raise ValueError(
                f"Unknown op '{op}'. Must be: replace | replace_range | delete | append | prepend",
            )


def _write_atomic(resolved: Path, content: str, *, exclusive: bool = False) -> None:
    fd, tmp_path = tempfile.mkstemp(dir=resolved.parent, prefix=".hashline_tmp_")
    os.close(fd)
    try:
        Path(tmp_path).write_text(content, encoding="utf-8")
        if exclusive:
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
    return "\n".join(
        difflib.unified_diff(
            original,
            new,
            fromfile="before",
            tofile="after",
            lineterm="",
            n=context,
        ),
    )


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
            f"File already exists: {resolved}  - pass overwrite=true to replace it, "
            "or use `hashline edit` to modify it in place.",
        )

    resolved.parent.mkdir(parents=True, exist_ok=True)
    status = "overwritten" if resolved.exists() else "created"

    normalized = [line.rstrip("\r\n") for line in lines]
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
        result["autofix"] = {
            "applied": _run_autofix(str(resolved)),
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
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"File not found: {resolved}")

    raw_text = resolved.read_text(encoding="utf-8", errors="replace")
    had_trailing_newline = raw_text.endswith("\n")
    original = raw_text.splitlines()

    conflicts = _check_edit_conflicts(edits)
    if conflicts:
        raise ValueError(
            "Edit conflict(s) detected - nothing written:\n"
            + "\n".join(f"  * {conflict}" for conflict in conflicts),
        )

    stale = _validate_all_refs(edits, original)
    auto_retried = False
    if stale:
        if not auto_retry:
            affected: set[int] = set()
            for stale_ref in stale:
                try:
                    line_no = _parse_ref(stale_ref["provided"])[0]
                    affected.update(range(max(1, line_no - 2), min(len(original), line_no + 3) + 1))
                except ValueError:
                    pass

            snippet_lines: list[str] = []
            for line_no in sorted(affected):
                idx = line_no - 1
                if 0 <= idx < len(original):
                    tag = _compute_line_hash(line_no, original[idx])
                    marker = ">>>" if any(
                        stale_ref["provided"].startswith(f"{line_no}#")
                        for stale_ref in stale
                    ) else "   "
                    snippet_lines.append(f"{marker} {line_no}#{tag}| {original[idx]}")

            raise _MismatchError(stale, _auto_patch_edits(edits, stale), "\n".join(snippet_lines))

        edits = _auto_patch_edits(edits, stale)
        auto_retried = True

    working = list(original)
    _apply_edits(working, edits)

    new_content = "\n".join(working)
    if had_trailing_newline:
        new_content += "\n"

    diff = _compact_diff(original, working)
    if not dry_run:
        _write_atomic(resolved, new_content)

    result: dict[str, Any] = {
        "status": "dry_run" if dry_run else ("auto_retried" if auto_retried else "written"),
    }
    if auto_retried:
        result["patches"] = len(stale)
    if diff:
        result["diff"] = diff
    if autofix and not dry_run:
        result["autofix"] = {
            "applied": _run_autofix(str(resolved)),
            "skipped": _SKIPPED_FIXERS_REASON,
        }
    return result


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
        super().__init__(
            json.dumps(
                {
                    "error": "HashlineMismatch",
                    "description": f"{len(stale_refs)} stale LINE#ID(s) - nothing written.",
                    "stale_refs": stale_refs,
                    "retry_edits": retry_edits,
                    "snippet": snippet,
                },
                indent=2,
            ),
        )
