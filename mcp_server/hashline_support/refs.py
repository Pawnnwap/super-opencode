from __future__ import annotations

import hashlib
from typing import Any

_CHARSET = "ZPMQVRWSNKTXJBYH"
_ALGO = "sha256"
_HASH_CHARS = 3


def _compute_line_hash(line_number: int, content: str) -> str:
    raw = f"{line_number}:{content}"
    digest = hashlib.new(_ALGO, raw.encode()).digest()
    return "".join(_CHARSET[digest[i] & 0x0F] for i in range(_HASH_CHARS))


def _format_tagged_line(line_number: int, content: str) -> str:
    return f"{line_number}#{_compute_line_hash(line_number, content)}| {content}"


def _parse_ref(ref: str) -> tuple[int, str]:
    ref = ref.strip()
    parts = ref.split("#", 1)
    if len(parts) != 2 or not parts[0].isdigit() or len(parts[1]) != _HASH_CHARS:
        raise ValueError(f"Invalid LINE#ID '{ref}': expected '<line_no>#<{_HASH_CHARS}-char-id>'")
    return int(parts[0]), parts[1]


def _validate_all_refs(
    edits: list[dict[str, Any]],
    lines: list[str],
) -> list[dict[str, str]]:
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
    correction_map = {stale_item["provided"]: stale_item["current"] for stale_item in stale}
    patched = []
    for edit in edits:
        updated = dict(edit)
        if updated.get("pos") in correction_map:
            updated["pos"] = correction_map[updated["pos"]]
        if updated.get("end_pos") in correction_map:
            updated["end_pos"] = correction_map[updated["end_pos"]]
        patched.append(updated)
    return patched


def _check_edit_conflicts(edits: list[dict[str, Any]]) -> list[str]:
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
