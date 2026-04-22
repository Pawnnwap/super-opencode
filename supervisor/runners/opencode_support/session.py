from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

SESSION_CAPTURE_LOCK = threading.Lock()


def list_all_session_ids(
    *,
    workspace: Path,
    opencode_executable: str,
    find_opencode_fn: Callable[[str], str],
) -> set[str]:
    """Enumerate every session ID opencode currently reports."""
    try:
        exe = find_opencode_fn(opencode_executable)
        use_shell = sys.platform == "win32" and exe.lower().endswith(
            (".cmd", ".bat", ".ps1"),
        )
        result = subprocess.run(
            [exe, "session", "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(workspace),
            timeout=10,
            shell=use_shell,
        )
        ids: set[str] = set()
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("ses_"):
                ids.add(stripped.split()[0])
        return ids
    except Exception as exc:
        logger.warning("Failed to list sessions: %s", exc)
        return set()


def capture_new_session_id(
    before: set[str],
    *,
    workspace: Path,
    opencode_executable: str,
    find_opencode_fn: Callable[[str], str],
    attempts: int = 4,
    delay_seconds: float = 0.25,
) -> str | None:
    """Return session ID that appeared since `before` snapshot."""
    for attempt in range(1, attempts + 1):
        after = list_all_session_ids(
            workspace=workspace,
            opencode_executable=opencode_executable,
            find_opencode_fn=find_opencode_fn,
        )
        new_ids = after - before
        if len(new_ids) == 1:
            session_id = next(iter(new_ids))
            logger.info("Captured session ID: %s", session_id)
            return session_id
        if len(new_ids) > 1:
            logger.warning(
                "Ambiguous session capture after attempt %d/%d: %d new sessions appeared (%s); refusing to pick one.",
                attempt,
                attempts,
                len(new_ids),
                sorted(new_ids),
            )
            return None
        if attempt < attempts:
            time.sleep(delay_seconds)
    logger.warning(
        "No new session appeared after BREVITY_COMMAND (%d attempts).",
        attempts,
    )
    return None

