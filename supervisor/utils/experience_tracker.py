"""supervisor/utils/experience_tracker.py — Track build experience across iterations.

Maintains an experience.md file in the workspace with two sections:
- What worked
- What failed

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
    """Create the experience.md file if it doesn't exist.

    Args:
        workspace: The workspace directory path.

    Returns:
        Path to the experience.md file.
    """
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
    """Append worked/failed items to the experience file.

    Creates the file if it doesn't exist.

    Args:
        workspace: The workspace directory path.
        worked: List of things that worked.
        failed: List of things that failed.
    """
    path = workspace / _EXPERIENCE_FILE
    if path.exists():
        content = path.read_text(encoding="utf-8")
    else:
        content = f"{_HEADER_WORKED}\n\n{_HEADER_FAILED}\n"

    lines = content.splitlines()

    # Find section boundaries
    worked_idx = None
    failed_idx = None
    for i, line in enumerate(lines):
        if line.strip() == _HEADER_WORKED:
            worked_idx = i
        elif line.strip() == _HEADER_FAILED:
            failed_idx = i

    if worked_idx is None or failed_idx is None:
        # Malformed file, recreate
        content = f"{_HEADER_WORKED}\n\n{_HEADER_FAILED}\n"
        worked_idx = 0
        failed_idx = 2

    lines = content.splitlines()
    new_lines = list(lines)

    # Insert worked bullets before the failed header
    if worked:
        insert_pos = failed_idx
        for item in reversed(worked):
            new_lines.insert(insert_pos, f"- {item}")

    # Insert failed bullets at the end
    if failed:
        for item in failed:
            new_lines.append(f"- {item}")

    new_content = "\n".join(new_lines) + "\n"
    _atomic_write(path, new_content)


def read_experience(workspace: Path) -> str:
    """Read the full experience file content.

    Args:
        workspace: The workspace directory path.

    Returns:
        Content of the experience file, or empty string if not found.
    """
    path = workspace / _EXPERIENCE_FILE
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Failed to read experience file: %s", e)
        return ""


def read_experience_capped(workspace: Path, max_chars: int = 10000) -> str:
    """Read experience file, truncating from the middle if it exceeds max_chars.

    Preserves headers and recent entries.

    Args:
        workspace: The workspace directory path.
        max_chars: Maximum number of characters to return.

    Returns:
        Truncated experience content.
    """
    content = read_experience(workspace)
    if not content or len(content) <= max_chars:
        return content

    # Keep headers and recent content
    lines = content.splitlines()
    if len(lines) <= 4:
        return content

    # Find header positions
    worked_idx = None
    failed_idx = None
    for i, line in enumerate(lines):
        if line.strip() == _HEADER_WORKED:
            worked_idx = i
        elif line.strip() == _HEADER_FAILED:
            failed_idx = i

    if worked_idx is not None and failed_idx is not None:
        # Keep both headers and distribute remaining budget
        header_lines = [lines[worked_idx], lines[failed_idx]]
        body_lines = lines[failed_idx + 1:]

        budget = max_chars - len("\n".join(header_lines)) - 100
        if budget > 0 and body_lines:
            # Take last N lines that fit
            kept = []
            current_len = 0
            for line in reversed(body_lines):
                line_len = len(line) + 1
                if current_len + line_len > budget:
                    break
                kept.append(line)
                current_len += line_len
            body_lines = list(reversed(kept))

        return "\n".join(header_lines + body_lines)

    return content[:max_chars]


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically using a temp file.

    Args:
        path: Target file path.
        content: Content to write.
    """
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
