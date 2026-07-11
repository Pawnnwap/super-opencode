"""Safe, idempotent registration of Super-Opencode MCP tools for Codex."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

CODEHELP_SERVER_NAME = "super_opencode_codehelp"


def find_codex_executable(executable: str = "") -> str | None:
    """Find Codex CLI without assuming a particular npm install location."""
    if executable.strip():
        return executable

    appdata = os.environ.get("APPDATA")
    candidates = [
        Path(appdata) / "npm" / "codex.cmd" if appdata else None,
        shutil.which("codex.cmd"),
        shutil.which("codex"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def _run_codex(command: list[str]) -> subprocess.CompletedProcess[str]:
    executable = command[0]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=20,
        shell=(sys.platform == "win32" and executable.lower().endswith((".cmd", ".bat"))),
    )


def ensure_codehelp_codex_mcp(
    project_root: Path,
    *,
    python_executable: str | None = None,
    codex_executable: str = "",
    on_info: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> bool:
    """Register Codehelp once, leaving existing Codex MCP configuration untouched."""
    executable = find_codex_executable(codex_executable)
    if not executable:
        if on_warning:
            on_warning("Codex CLI not found; Codehelp MCP was not registered for Codex.")
        return False

    server_script = (project_root / "mcp_server" / "codehelp.py").resolve()
    if not server_script.is_file():
        if on_warning:
            on_warning(f"Codehelp MCP entrypoint missing: {server_script}")
        return False

    try:
        existing = _run_codex([executable, "mcp", "get", CODEHELP_SERVER_NAME])
    except (OSError, subprocess.TimeoutExpired) as exc:
        if on_warning:
            on_warning(f"Could not inspect Codex MCP configuration: {exc}")
        return False
    if existing.returncode == 0:
        return True

    interpreter = python_executable or sys.executable or "python"
    try:
        added = _run_codex(
            [
                executable,
                "mcp",
                "add",
                CODEHELP_SERVER_NAME,
                "--",
                interpreter,
                str(server_script),
            ],
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if on_warning:
            on_warning(f"Could not register Codehelp MCP for Codex: {exc}")
        return False

    if added.returncode != 0:
        if on_warning:
            detail = (added.stderr or added.stdout).strip() or "unknown Codex CLI error"
            on_warning(f"Could not register Codehelp MCP for Codex: {detail}")
        return False
    if on_info:
        on_info("Codehelp MCP registered for Codex.")
    return True
