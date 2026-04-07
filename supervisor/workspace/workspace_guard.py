"""supervisor/workspace_guard.py — enforces workspace path boundaries and protects critical directories."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from supervisor.utils.file_permissions import remove_file_readonly, set_file_readonly

if TYPE_CHECKING:
    from supervisor.workspace.ignore_patterns import IgnoreMatcher

_PATH_RE = re.compile(r"""(?:^|\s)(/?(?:[\w.\-]+/)+[\w.\-]*)""")

_PROTECTED_DIRS = {".opencode", ".checkpoints", ".archive", "archive"}
_PROTECTED_DIR_PREFIXES = (".",)
_PROTECTED_FILES = {".opencoderc", ".opencode", ".opencodeignore"}


class WorkspaceGuard:
    def __init__(self, workspace: Path, protected_files: Iterable[str] = ()):
        self.workspace = workspace.resolve()
        self._protected_files: set[str] = set()
        self._ignore_matcher: IgnoreMatcher | None = None
        for pf in protected_files:
            self._protected_files.add(Path(pf).as_posix())

    def sanitize_message(self, message: str) -> tuple[str, list[str]]:
        """Prepend workspace reminder; return (patched_msg, violations)."""
        violations: list[str] = []
        for m in _PATH_RE.finditer(message):
            candidate = m.group(1)
            try:
                Path(candidate).resolve().relative_to(self.workspace)
            except ValueError:
                violations.append(candidate)

        preamble = (
            f"[WORKSPACE RESTRICTION] You may only operate inside: {self.workspace}\n\n"
        )
        return preamble + message, violations

    def is_inside(self, path: str) -> bool:
        try:
            path_obj = Path(path)
            if path_obj.is_absolute():
                path_obj.relative_to(self.workspace)
                return True
            resolved = (self.workspace / path_obj).resolve()
            resolved.relative_to(self.workspace)
            return True
        except ValueError:
            return False

    def is_protected_path(self, path: str | Path) -> bool:
        """Check if a path is a protected system directory or user-protected file."""
        path_obj = Path(path)
        parts = path_obj.parts
        path_str = str(path)

        if self.is_user_protected_file(path):
            return True

        for protected in _PROTECTED_DIRS:
            if protected in parts:
                return True

        for part in parts:
            if any(part.startswith(p) for p in _PROTECTED_DIR_PREFIXES):
                return True

        for protected in _PROTECTED_FILES:
            if (
                path_str == protected
                or path_str.startswith(f"{protected}/")
                or path_str.startswith(f"{protected}\\")
            ):
                return True
            if path_obj.name == protected:
                return True

        return False

    def check_protected_violations(self, message: str) -> list[str]:
        """Check message for attempts to access or modify protected paths."""
        violations: list[str] = []

        protected_patterns = [
            r"\.opencode",
            r"\.checkpoints",
            r"\.archive\b",
            r"\barchive\b",
        ]
        for protected_file in self._protected_files:
            protected_patterns.append(rf"\b{re.escape(protected_file)}\b")

        action_patterns = [
            r"delete",
            r"remove",
            r"unlink",
            r"rm\s",
            r"move\s",
            r"mv\s",
            r"rename",
            r"modify",
            r"edit",
            r"change",
            r"overwrite",
            r"chmod",
            r"attrib",
        ]

        for pattern in protected_patterns:
            for match in re.finditer(pattern, message, re.IGNORECASE):
                context_start = max(0, match.start() - 20)
                context_end = min(len(message), match.end() + 20)
                context = message[context_start:context_end]

                for action in action_patterns:
                    if re.search(action, context, re.IGNORECASE):
                        # Use the matched string as the label
                        violations.append(f"{match.group()}: {action.strip()}")
                        break

        return violations

    def sanitize_with_protection(
        self, message: str,
    ) -> tuple[str, list[str], list[str]]:
        """Full sanitization: workspace bounds + protected paths.
        Returns (sanitized_msg, workspace_violations, protected_violations).
        """
        sanitized, ws_violations = self.sanitize_message(message)
        protected_violations = self.check_protected_violations(message)

        if protected_violations or self._protected_files:
            protection_warning = (
                "\n\n[CRITICAL PROTECTION] The following paths are protected and must NOT be "
                "modified or deleted:\n"
                "  - .opencode/ directory (supervisor configuration)\n"
                "  - .checkpoints/ directory (system checkpoints)\n"
                "  - .archive/ directory (version archives)\n"
            )
            if self._protected_files:
                protection_warning += "\nUser-defined protected files:\n"
                for pf in sorted(self._protected_files):
                    protection_warning += f"  - {pf}\n"
            sanitized += protection_warning

        return sanitized, ws_violations, protected_violations

    def get_protected_dirs(self) -> set[str]:
        """Return set of protected directory names."""
        return _PROTECTED_DIRS.copy()

    def filter_protected_paths(self, paths: list[str]) -> tuple[list[str], list[str]]:
        """Filter a list of paths into allowed and protected paths."""
        allowed = []
        protected = []
        for path in paths:
            if self.is_protected_path(path):
                protected.append(path)
            else:
                allowed.append(path)
        return allowed, protected

    def validate_no_protected_operations(
        self, paths: list[str],
    ) -> tuple[bool, list[str]]:
        """Validate that a list of file paths does not include protected paths.
        Returns (is_valid, list_of_violations).
        """
        violations = []
        for path in paths:
            if self.is_protected_path(path):
                violations.append(f"Protected path: {path}")
        return len(violations) == 0, violations

    @property
    def protected_paths(self) -> set[str]:
        """Return the set of protected path names."""
        return _PROTECTED_DIRS.copy()

    def add_protected_file(self, path: str | Path) -> None:
        """Add a user-defined protected file path."""
        path_str = str(path)
        self._protected_files.add(path_str)
        normalized = Path(path_str).as_posix()
        if normalized != path_str:
            self._protected_files.add(normalized)

    def remove_protected_file(self, path: str | Path) -> bool:
        """Remove a user-defined protected file. Returns True if found and removed."""
        path_str = str(path)
        if path_str in self._protected_files:
            self._protected_files.discard(path_str)
            return True
        normalized = Path(path_str).as_posix()
        if normalized in self._protected_files:
            self._protected_files.discard(normalized)
            return True
        return False

    def get_user_protected_files(self) -> frozenset[str]:
        """Return the set of user-defined protected files."""
        return frozenset(self._protected_files)

    def set_protected_files(self, paths: Iterable[str]) -> None:
        """Replace all user-defined protected files."""
        self._protected_files = set(paths)

    def is_user_protected_file(self, path: str | Path) -> bool:
        """Check if a path is a user-defined protected file."""
        path_str = str(path)
        if path_str in self._protected_files:
            return True
        normalized = Path(path_str).as_posix()
        if normalized in self._protected_files:
            return True
        if Path(path_str).name in self._protected_files:
            return True
        if Path(normalized).name in self._protected_files:
            return True
        try:
            rel = Path(path_str).resolve().relative_to(self.workspace)
            if str(rel) in self._protected_files or rel.name in self._protected_files:
                return True
            normalized_rel = rel.as_posix()
            if normalized_rel in self._protected_files:
                return True
        except ValueError:
            pass
        return False

    def get_all_protected_files_description(self) -> str:
        """Return a formatted string describing all protected files for prompts."""
        if not self._protected_files:
            return ""
        lines = [
            "\n\n[USER PROTECTED FILES] The following files are protected and must NOT be modified or deleted:",
        ]
        for pf in sorted(self._protected_files):
            lines.append(f"  - {pf}")
        return "\n".join(lines)

    def set_ignore_matcher(self, matcher: IgnoreMatcher) -> None:
        """Set the ignore matcher for checking ignored file patterns."""
        self._ignore_matcher = matcher

    def is_ignored_path(self, path: str | Path) -> bool:
        """Check if a path matches ignore patterns from .opencodeignore."""
        if self._ignore_matcher is None:
            return False
        return self._ignore_matcher.matches(path)

    def check_ignore_violations(self, paths: list[str]) -> list[str]:
        """Check for paths that match ignore patterns and should not be modified."""
        if self._ignore_matcher is None:
            return []
        violations = []
        for path in paths:
            if self._ignore_matcher.matches(path):
                violations.append(f"Ignored path: {path}")
        return violations

    def set_readonly_protection(self, paths: list[str]) -> list[str]:
        """Set read-only attributes recursively on the given paths.
        Handles platform differences (Windows vs Unix).
        Does not follow symbolic links outside the workspace.
        Returns list of paths that were successfully protected.
        """
        protected = []
        for path_str in paths:
            path = Path(path_str)
            if not path.exists():
                continue
            try:
                if path.is_symlink():
                    link_target = path.resolve()
                    try:
                        link_target.relative_to(self.workspace)
                    except ValueError:
                        continue
                    self._set_file_readonly(path_str)
                    protected.append(path_str)
                elif path.is_dir():
                    for item in path.rglob("*"):
                        if item.is_symlink():
                            try:
                                item.resolve().relative_to(self.workspace)
                            except ValueError:
                                continue
                        try:
                            if item.is_file():
                                if item.name != ".archive_counter":
                                    self._set_file_readonly(str(item))
                        except (OSError, PermissionError):
                            pass
                    protected.append(path_str)
                else:
                    if path.name != ".archive_counter":
                        self._set_file_readonly(path_str)
                    protected.append(path_str)
            except (OSError, PermissionError, ValueError):
                continue
        return protected

    def remove_readonly_protection(self, paths: list[str]) -> list[str]:
        """Remove read-only attributes recursively on the given paths.
        Handles platform differences (Windows vs Unix).
        Does not follow symbolic links outside the workspace.
        Returns list of paths that were successfully unprotected.
        """
        unprotected = []
        for path_str in paths:
            path = Path(path_str)
            if not path.exists():
                continue
            try:
                if path.is_symlink():
                    link_target = path.resolve()
                    try:
                        link_target.relative_to(self.workspace)
                    except ValueError:
                        continue
                    self._remove_file_readonly(path_str)
                    unprotected.append(path_str)
                elif path.is_dir():
                    for item in path.rglob("*"):
                        if item.is_symlink():
                            try:
                                item.resolve().relative_to(self.workspace)
                            except ValueError:
                                continue
                        try:
                            if item.is_file():
                                self._remove_file_readonly(str(item))
                        except (OSError, PermissionError):
                            pass
                    unprotected.append(path_str)
                else:
                    self._remove_file_readonly(path_str)
                    unprotected.append(path_str)
            except (OSError, PermissionError, ValueError):
                continue
        return unprotected

    @staticmethod
    def _set_file_readonly(path_str: str) -> None:
        set_file_readonly(path_str)

    @staticmethod
    def _remove_file_readonly(path_str: str) -> None:
        remove_file_readonly(path_str)
