"""supervisor/workspace/opencodeignore_handler.py

Handles loading and matching .opencodeignore patterns for workspace operations.
Provides functions for loading patterns from .opencodeignore files and checking
whether paths should be ignored during archiving and other operations.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path


def load_opencodeignore_patterns(workspace: Path) -> list[str]:
    """Read .opencodeignore from the workspace root and return patterns.

    Args:
        workspace: Path to the workspace root directory.

    Returns:
        List of pattern strings from .opencodeignore, or empty list if the
        file doesn't exist or cannot be read.

    """
    ignore_file = workspace / ".opencodeignore"
    if not ignore_file.exists():
        return []
    try:
        content = ignore_file.read_text(encoding="utf-8")
        patterns: list[str] = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
        return patterns
    except (OSError, UnicodeDecodeError):
        return []


def should_ignore(path: Path, patterns: list[str]) -> bool:
    """Check if a path matches any pattern in the list.

    Supports:
      - Exact filename: "debug.py"
      - Directory patterns: "build/" matches "build" and anything under it
      - Glob patterns: "*.pyc", "**/*.log", "test_*"
      - Path patterns: "src/temp.py"

    Args:
        path: The path to check (relative or absolute).
        patterns: List of pattern strings to match against.

    Returns:
        True if the path matches any pattern, False otherwise.

    """
    if not patterns:
        return False

    path_str = str(path).replace("\\", "/")
    filename = Path(path_str).name

    for pattern in patterns:
        if not pattern:
            continue

        if pattern.endswith("/"):
            # Directory pattern: matches if any part of the path equals the dir name
            dir_name = pattern.rstrip("/")
            parts = path_str.split("/")
            if dir_name in parts:
                return True
            continue

        if "/" in pattern:
            # Path pattern: use fnmatch on the full path
            if fnmatch.fnmatch(path_str, pattern):
                return True
            continue

        # Filename or glob pattern
        if fnmatch.fnmatch(filename, pattern):
            return True

    return False
