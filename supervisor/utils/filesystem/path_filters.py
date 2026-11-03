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

# Extra directories excluded when building CANDIDATE file lists for READING
# (the supervisor's LLM file-selection walk, the UI's protected-files scan).
# Deliberately more aggressive than DEFAULT_IGNORE_DIRS — which snapshots and
# archives keep using so they still see the whole workspace — because a
# candidate list feeds model context or a human droplist, where dependency
# trees, package caches and build outputs are pure noise. The dot-prefix rule
# already hides every dotted cache dir (.pytest_cache, .tox, .cache, ...);
# these are the non-dotted equivalents that would otherwise slip through.
CANDIDATE_IGNORE_DIRS: frozenset[str] = DEFAULT_IGNORE_DIRS | {
    "__pypackages__",
    "site-packages",
    "build",
    "dist",
    "target",
    "htmlcov",
    "coverage",
}

# File extensions never worth offering as reading candidates: byte-compiled
# cache artifacts and well-known binary formats. The content sniff in
# :func:`is_probably_binary` is the safety net for everything not listed.
CANDIDATE_IGNORE_SUFFIXES: frozenset[str] = frozenset(
    {
        # byte-compiled / cached python
        ".pyc",
        ".pyo",
        ".pyd",
        # native binaries & objects
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".o",
        ".obj",
        ".a",
        ".lib",
        ".class",
        ".jar",
        ".wasm",
        # archives
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        # images / media / fonts
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".pdf",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        # databases / documents / serialized blobs
        ".db",
        ".sqlite",
        ".sqlite3",
        ".parquet",
        ".pkl",
        ".pickle",
        ".xlsx",
        ".docx",
        ".pptx",
    },
)

# How many leading bytes the binary sniff inspects. 8 KB is git's ballpark:
# enough to catch a NUL byte in any real binary header, cheap even across a
# few thousand candidate files.
_BINARY_SNIFF_BYTES = 8192


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


def is_probably_binary(path: Path | str) -> bool:
    """Return ``True`` when *path* is not worth reading as text.

    Sniffs the first bytes for a NUL byte — the standard text/binary
    heuristic (the same one git uses): every real binary format (executables,
    images, archives, databases) carries NULs in its header, while text in
    any encoding — UTF-8, UTF-16 with its BOM, latin-1 — is either NUL-free
    or caught by it. A file that cannot be opened at all (permissions,
    vanished mid-walk, Windows lock) also counts as binary: an unreadable
    file has no place in a reading candidate list.
    """
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True
    if not chunk:
        return False  # empty file: trivially text
    return b"\x00" in chunk


def is_candidate_file(path: Path | str) -> bool:
    """Return ``True`` when *path* is worth offering as a reading candidate.

    The one predicate every candidate-list builder should use (supervisor
    LLM file selection, UI workspace scans): skips ignored/cache directories
    (the candidate set, not the traversal default), known binary/cache
    extensions, and any file whose content sniffs as binary or unreadable.
    """
    p = Path(path)
    if not p.is_file():
        return False
    if p.suffix.lower() in CANDIDATE_IGNORE_SUFFIXES:
        return False
    # Package-metadata dirs carry dynamic names ("pkg.egg-info") that no
    # static dir set can match — rule on the part suffix instead.
    if any(part.endswith(".egg-info") for part in p.parts):
        return False
    if should_skip_path(p, extra_dirs=CANDIDATE_IGNORE_DIRS):
        return False
    return not is_probably_binary(p)
