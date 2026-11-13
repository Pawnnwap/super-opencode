"""Workspace containment for the MCP codebase tools.

``codebase_path`` is an agent-supplied argument, and the codehelp tools walk
whatever directory it names. Without a bound, ``codebase_path="C:\\Users"``
reads any tree on the machine — including credential stores the path policy
never whitelisted. The bound is the run's workspace, published by the
supervisor as ``SOP_WORKSPACE`` when it registers the MCP server.

Outside a supervised run the variable is absent; the tools then fall back to
the process cwd, which keeps interactive use working without widening the
bound to "anywhere".
"""

from __future__ import annotations

import os
from pathlib import Path

WORKSPACE_ENV = "SOP_WORKSPACE"


def workspace_root() -> Path:
    """The directory codebase tools may read (never the whole filesystem)."""
    raw = (os.environ.get(WORKSPACE_ENV) or "").strip()
    if raw:
        try:
            return Path(raw).resolve()
        except OSError:
            pass
    return Path.cwd().resolve()


def resolve_codebase_path(raw: str | Path | None) -> tuple[Path | None, str]:
    """Resolve *raw* to a directory inside the workspace.

    Returns ``(path, "")`` on success and ``(None, reason)`` when the path is
    missing, not a directory, or outside the workspace. Callers turn the
    reason into a structured tool error instead of raising.
    """
    text = str(raw or ".").strip() or "."
    root = workspace_root()
    try:
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
    except (OSError, ValueError) as exc:
        return None, f"unresolvable path: {exc}"
    if not resolved.is_dir():
        return None, f"codebase_path is not a directory: {resolved}"
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, f"codebase_path is outside the workspace: {resolved}"
    return resolved, ""


__all__ = ["WORKSPACE_ENV", "resolve_codebase_path", "workspace_root"]
