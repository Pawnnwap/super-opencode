"""supervisor/utils/experience_tracker.py — Track build experience across iterations.

Maintains an experience.md file in the workspace with two sections:
- What worked
- What Failed

This experience is injected into the supervisor's judgment prompts to
provide context about previous attempts.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_HEADER_WORKED = "## What Worked"
_HEADER_FAILED = "## What Failed"
_EXPERIENCE_FILE = "experience.md"


def init_experience_file(workspace: Path) -> Path:
    path = workspace / _EXPERIENCE_FILE
    if not path.exists():
        initial_content = f"{_HEADER_WORKED}\n\n{_HEADER_FAILED}\n"
        _atomic_write(path, initial_content)
        logger.info("Initialized experience file: %s", path)
    return path


def update_experience(
    workspace: Path,
    worked: list[str] | None = None,
    failed: list[str] | None = None,
) -> None:
    path = workspace / _EXPERIENCE_FILE
    content = read_experience(workspace)
    if not content:
        content = f"{_HEADER_WORKED}\n\n{_HEADER_FAILED}\n"

    # Split on the Failed header only (maxsplit=1 prevents re-splitting on body text)
    parts = content.split(_HEADER_FAILED, maxsplit=1)
    if len(parts) == 1:
        content = f"{_HEADER_WORKED}\n\n{_HEADER_FAILED}\n"
        parts = content.split(_HEADER_FAILED, maxsplit=1)

    # before_failed already contains the _HEADER_WORKED line; after_failed is
    # the body text that follows _HEADER_FAILED (no header prefix).
    before_failed = parts[0]
    after_failed = parts[1] if len(parts) > 1 else "\n"

    if worked:
        worked_items = "\n".join(f"- {item}" for item in worked)
        before_failed = before_failed.rstrip() + "\n" + worked_items + "\n"

    if failed:
        failed_items = "\n".join(f"- {item}" for item in failed)
        after_failed = after_failed.rstrip() + "\n" + failed_items + "\n"

    # Reconstruct: before_failed already has the Worked header; append the
    # Failed header + body without duplicating any header.
    worked_body = before_failed.rstrip()
    failed_body = after_failed.strip()
    if failed_body:
        new_content = f"{worked_body}\n\n{_HEADER_FAILED}\n\n{failed_body}\n"
    else:
        new_content = f"{worked_body}\n\n{_HEADER_FAILED}\n"
    _atomic_write(path, new_content)


def read_experience(workspace: Path) -> str:
    path = workspace / _EXPERIENCE_FILE
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Failed to read experience file: %s", e)
        return ""


def read_experience_capped(workspace: Path, max_chars: int = 10000) -> str:
    content = read_experience(workspace)
    if not content or len(content) <= max_chars:
        return content

    lines = content.splitlines()
    if len(lines) <= 4:
        return content

    worked_idx = None
    failed_idx = None
    for i, line in enumerate(lines):
        if line.strip() == _HEADER_WORKED:
            worked_idx = i
        elif line.strip() == _HEADER_FAILED:
            failed_idx = i

    if worked_idx is None or failed_idx is None:
        return content[:max_chars]

    worked_header = lines[worked_idx]
    failed_header = lines[failed_idx]
    header_len = len(worked_header) + len(failed_header) + 2
    available = max_chars - header_len

    worked_budget = int(available * 0.4)
    failed_budget = int(available * 0.6)

    worked_body_start = worked_idx + 1
    worked_body_end = failed_idx
    worked_body = lines[worked_body_start:worked_body_end]
    kept_worked = []
    worked_len = 0
    for line in reversed(worked_body):
        line_len = len(line) + 1
        if worked_len + line_len > worked_budget:
            break
        kept_worked.append(line)
        worked_len += line_len
    kept_worked = list(reversed(kept_worked))

    failed_body_start = failed_idx + 1
    failed_body = lines[failed_body_start:]
    kept_failed = []
    failed_len = 0
    for line in reversed(failed_body):
        line_len = len(line) + 1
        if failed_len + line_len > failed_budget:
            break
        kept_failed.append(line)
        failed_len += line_len
    kept_failed = list(reversed(kept_failed))

    return "\n".join([worked_header] + kept_worked + [failed_header] + kept_failed)


def _atomic_write(path: Path, content: str) -> None:
    dir_path = path.parent
    dir_path.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), prefix=".experience_tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
