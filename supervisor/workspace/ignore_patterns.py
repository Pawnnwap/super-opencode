"""
supervisor/ignore_patterns.py — handles .opencodeignore file parsing and pattern matching.

Supports patterns for excluding files during context retrieval and protecting files
from modification:
  - Exact filename matches: "debug.py"
  - Prefix matches: "prefix*" matches "prefix_anything"
  - Suffix matches: "*_test.py" matches "anything_test.py"
  - Glob patterns: "**/*.pyc"
  - Directory patterns: "build/" (excludes entire directory)

Also integrates .gitignore patterns when available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

IGNORE_FILE = ".opencodeignore"
GITIGNORE_FILE = ".gitignore"

_DEFAULT_PATTERNS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".checkpoints",
    "archive",
    ".archive",
}


@dataclass
class IgnorePattern:
    pattern: str
    is_prefix: bool = False
    is_suffix: bool = False
    is_directory: bool = False
    is_glob: bool = False
    regex: re.Pattern | None = None


class IgnoreMatcher:
    def __init__(self, workspace: Path | None = None):
        self.workspace = workspace
        self.patterns: list[IgnorePattern] = []
        self._regex_cache: dict[str, re.Pattern] = {}

    def load_from_file(self, ignore_file: Path) -> bool:
        """Load patterns from a .opencodeignore file. Returns True if file exists."""
        if not ignore_file.exists():
            return False
        try:
            content = ignore_file.read_text(encoding="utf-8")
            self.parse_patterns(content.splitlines())
            return True
        except Exception:
            return False

    def load_from_workspace(self, workspace: Path) -> bool:
        """Load patterns from workspace's .opencodeignore file."""
        self.workspace = workspace
        ignore_file = workspace / IGNORE_FILE
        return self.load_from_file(ignore_file)

    def parse_patterns(self, lines: Iterable[str]) -> None:
        """Parse lines from .opencodeignore into IgnorePattern objects."""
        self.patterns = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            pattern = IgnorePattern(pattern=line)

            if line.endswith("/"):
                pattern.is_directory = True
                pattern.pattern = line.rstrip("/")
            elif "*" in line:
                if line.startswith("*"):
                    pattern.is_suffix = True
                elif line.endswith("*"):
                    pattern.is_prefix = True
                elif "**" in line or "?" in line:
                    pattern.is_glob = True
                    pattern.regex = self._glob_to_regex(line)
            elif "/" in line:
                pattern.is_directory = True

            self.patterns.append(pattern)

    def _glob_to_regex(self, glob_pattern: str) -> re.Pattern:
        """Convert glob pattern to regex."""
        if glob_pattern in self._regex_cache:
            return self._regex_cache[glob_pattern]

        pattern = glob_pattern
        pattern = pattern.replace(".", r"\.")
        pattern = pattern.replace("**/", "(?:.*/)?")
        pattern = pattern.replace("**", ".*")
        pattern = pattern.replace("*", "[^/]*")
        pattern = pattern.replace("?", ".")
        pattern = f"^{pattern}$"

        regex = re.compile(pattern)
        self._regex_cache[glob_pattern] = regex
        return regex

    def matches(self, path: str | Path) -> bool:
        """Check if a path matches any ignore pattern."""
        path_str = str(path).replace("\\", "/")

        path_obj = Path(path_str)
        filename = path_obj.name

        for pattern in self.patterns:
            if pattern.is_directory:
                if (
                    path_str.startswith(f"{pattern.pattern}/")
                    or f"/{pattern.pattern}/" in path_str
                ):
                    return True
                if path_obj.name == pattern.pattern:
                    return True
            elif pattern.is_prefix:
                if filename.startswith(pattern.pattern.rstrip("*")):
                    return True
            elif pattern.is_suffix:
                suffix = pattern.pattern.lstrip("*")
                if filename.endswith(suffix):
                    return True
            elif pattern.is_glob and pattern.regex:
                if pattern.regex.match(path_str):
                    return True
            else:
                if filename == pattern.pattern or path_str == pattern.pattern:
                    return True

        return False

    def filter_paths(self, paths: Iterable[str | Path]) -> tuple[list[str], list[str]]:
        """Filter paths into allowed and ignored."""
        allowed = []
        ignored = []
        for path in paths:
            if self.matches(path):
                ignored.append(str(path))
            else:
                allowed.append(str(path))
        return allowed, ignored

    def get_allowed_files(self, paths: Iterable[str | Path]) -> list[str]:
        """Return list of allowed (non-ignored) paths."""
        allowed, _ = self.filter_paths(paths)
        return allowed

    def is_ignored(self, path: str | Path) -> bool:
        """Check if a path should be ignored."""
        return self.matches(path)

    def get_patterns(self) -> list[str]:
        """Return list of raw pattern strings."""
        return [p.pattern for p in self.patterns]

    def add_pattern(self, pattern: str) -> None:
        """Add a single pattern."""
        self.parse_patterns([pattern])

    def clear(self) -> None:
        """Clear all patterns."""
        self.patterns = []
        self._regex_cache.clear()


def load_ignore_matcher(workspace: Path) -> IgnoreMatcher:
    """Create an IgnoreMatcher and load patterns from workspace."""
    matcher = IgnoreMatcher(workspace)
    matcher.load_from_workspace(workspace)
    return matcher


def get_default_ignore_dirs() -> set[str]:
    """Return default directories that should be ignored."""
    return _DEFAULT_PATTERNS.copy()


def create_ignore_file(workspace: Path, content: str = "") -> Path:
    """Create a .opencodeignore file in the workspace."""
    ignore_path = workspace / IGNORE_FILE
    if not ignore_path.exists():
        ignore_path.write_text(content, encoding="utf-8")
    return ignore_path


def read_ignore_file(workspace: Path) -> str | None:
    """Read the .opencodeignore file content. Returns None if not found."""
    ignore_path = workspace / IGNORE_FILE
    if ignore_path.exists():
        return ignore_path.read_text(encoding="utf-8")
    return None


def write_ignore_file(workspace: Path, content: str) -> Path:
    """Write content to .opencodeignore file."""
    ignore_path = workspace / IGNORE_FILE
    ignore_path.write_text(content, encoding="utf-8")
    return ignore_path


def load_gitignore_patterns(workspace: Path) -> list[str]:
    """Load patterns from .gitignore file.

    Args:
        workspace: Path to the workspace root.

    Returns:
        List of pattern strings from .gitignore, or empty list if not found.
    """
    gitignore_path = workspace / GITIGNORE_FILE
    if not gitignore_path.exists():
        return []
    try:
        content = gitignore_path.read_text(encoding="utf-8")
        patterns: list[str] = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)
        return patterns
    except (OSError, UnicodeDecodeError):
        return []


def load_combined_ignore_matcher(workspace: Path) -> IgnoreMatcher:
    """Create an IgnoreMatcher with patterns from both .gitignore and .opencodeignore.

    Both sets of patterns are combined into a single matcher.
    Patterns from .opencodeignore are added after .gitignore patterns.

    Args:
        workspace: Path to the workspace root.

    Returns:
        IgnoreMatcher with combined patterns.
    """
    matcher = IgnoreMatcher(workspace)

    # Collect all lines from both files
    all_lines: list[str] = []

    # Load .gitignore patterns first
    gitignore_path = workspace / GITIGNORE_FILE
    if gitignore_path.exists():
        try:
            content = gitignore_path.read_text(encoding="utf-8")
            all_lines.extend(content.splitlines())
        except (OSError, UnicodeDecodeError):
            pass

    # Load .opencodeignore patterns (these are added on top)
    opencodeignore_path = workspace / IGNORE_FILE
    if opencodeignore_path.exists():
        try:
            content = opencodeignore_path.read_text(encoding="utf-8")
            all_lines.extend(content.splitlines())
        except (OSError, UnicodeDecodeError):
            pass

    matcher.parse_patterns(all_lines)
    return matcher
