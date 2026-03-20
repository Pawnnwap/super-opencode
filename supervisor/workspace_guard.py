"""supervisor/workspace_guard.py — enforces workspace path boundaries and protects critical directories."""

from __future__ import annotations

import re
from pathlib import Path

_PATH_RE = re.compile(r"""(?:^|\s)(/?(?:[\w.\-]+/)+[\w.\-]*)""")

_PROTECTED_DIRS = {".opencode", ".checkpoints", "archive"}
_PROTECTED_FILES = {".opencoderc", ".opencode"}


class WorkspaceGuard:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

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
            else:
                resolved = (self.workspace / path_obj).resolve()
                resolved.relative_to(self.workspace)
                return True
        except ValueError:
            return False

    def is_protected_path(self, path: str | Path) -> bool:
        """Check if a path is a protected system directory."""
        path_obj = Path(path)
        parts = path_obj.parts
        path_str = str(path)
        
        for protected in _PROTECTED_DIRS:
            if protected in parts:
                return True
        
        for protected in _PROTECTED_FILES:
            if path_str == protected or path_str.startswith(f"{protected}/") or path_str.startswith(f"{protected}\\"):
                return True
            if path_obj.name == protected:
                return True
        
        return False

    def check_protected_violations(self, message: str) -> list[str]:
        """Check message for attempts to access or modify protected paths."""
        violations: list[str] = []
        
        protected_patterns = [
            r'\.opencode',
            r'\.checkpoints',
            r'\barchive\b',
        ]
        
        for pattern in protected_patterns:
            for match in re.finditer(pattern, message, re.IGNORECASE):
                context_start = max(0, match.start() - 20)
                context_end = min(len(message), match.end() + 20)
                context = message[context_start:context_end]
                
                action_patterns = [
                    r'delete', r'remove', r'unlink', r'rm\s',
                    r'move\s', r'mv\s', r'rename',
                    r'modify', r'edit', r'change',
                    r'overwrite', r'write',
                    r'chmod', r'attrib',
                ]
                
                for action in action_patterns:
                    if re.search(action, context, re.IGNORECASE):
                        violations.append(f"{match.group()}: {action.strip()}")
                        break
        
        return violations

    def sanitize_with_protection(self, message: str) -> tuple[str, list[str], list[str]]:
        """
        Full sanitization: workspace bounds + protected paths.
        Returns (sanitized_msg, workspace_violations, protected_violations).
        """
        sanitized, ws_violations = self.sanitize_message(message)
        protected_violations = self.check_protected_violations(message)
        
        if protected_violations:
            protection_warning = (
                "\n\n[CRITICAL PROTECTION] The following paths are protected and must NOT be "
                "modified or deleted:\n"
                "  - .opencode/ directory (supervisor configuration)\n"
                "  - .checkpoints/ directory (system checkpoints)\n"
                "  - .archive/ directory (version archives)\n"
            )
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

    def validate_no_protected_operations(self, paths: list[str]) -> tuple[bool, list[str]]:
        """
        Validate that a list of file paths does not include protected paths.
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
