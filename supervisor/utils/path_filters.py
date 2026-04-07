"""Shared path filtering utilities for directory traversal.

Provides a canonical set of ignore directories and prefixes used across
the codebase, plus a reusable ``should_skip_path()`` predicate so that
every module does not need its own duplicate constants and logic.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

# Canonical set of directories that should always be skipped during
# workspace traversal (codebase analysis, archiving, vulnerability
# scanning, etc.).
DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".checkpoint",
        ".checkpoints",
        ".archive",
    },
)

# Directory-name prefixes that should be skipped.
DEFAULT_IGNORE_PREFIXES: tuple[str, ...] = (".",)


def should_skip_path(
    path: Path | str,
    extra_dirs: Iterable[str] | None = None,
    extra_prefixes: Iterable[str] | None = None,
) -> bool:
    """Return ``True`` if *path* should be skipped during traversal.

    Parameters
    ----------
    path:
        A filesystem path (absolute or relative).
    extra_dirs:
        Additional directory names to treat as ignored.
    extra_prefixes:
        Additional directory-name prefixes to skip.

    Returns
    -------
    bool
        ``True`` when any component of the path matches an ignore rule.

    """
    p = Path(path)
    parts = p.parts

    ignore_dirs = DEFAULT_IGNORE_DIRS
    if extra_dirs:
        ignore_dirs = ignore_dirs | frozenset(extra_dirs)

    prefixes = DEFAULT_IGNORE_PREFIXES
    if extra_prefixes:
        prefixes = (*prefixes, *extra_prefixes)

    for part in parts:
        if part in ignore_dirs:
            return True
        if any(part.startswith(prefix) for prefix in prefixes):
            return True
    return False

