"""supervisor/workspace_guard.py — enforces workspace path boundaries."""

from __future__ import annotations

import re
from pathlib import Path

_PATH_RE = re.compile(r"""(?:^|\s)(/?(?:[\w.\-]+/)+[\w.\-]*)""")


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
            Path(path).resolve().relative_to(self.workspace)
            return True
        except ValueError:
            return False
