"""Detect supervisor code that changed under a long-lived job worker.

The worker daemon imports the loop modules once and keeps them for its whole
life, so a job launched after a code deployment runs the *previous* logic
unless the daemon is restarted. Measured failure (2026-11-05): a lane kept
re-judging a closed target for 100+ turns on code that predated the
attainment-based BLOCKED exit by one day - the worker had been up since the
previous afternoon.

The fingerprint is content-based over ``supervisor/`` and ``services/``; a
git checkout that only touches mtimes does not trigger a restart, while a
real edit does.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_WATCH_DIRS = ("supervisor", "services")


def code_fingerprint(root: Path | None = None) -> str:
    """Stable digest of every Python source file the worker executes."""
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for rel_dir in _WATCH_DIRS:
        base = root / rel_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(data)
            digest.update(b"\n")
    return digest.hexdigest()


def freshness_action(startup: str, current: str, active_jobs: int) -> str:
    """What the worker should do when loaded code no longer matches disk.

    ``ok``      - nothing changed; keep running and claiming jobs.
    ``hold``    - code changed while jobs are live: stop claiming new jobs,
                  let the live ones finish on the code they started with.
    ``restart`` - code changed and the worker is idle: re-exec to load it.
    """
    if current == startup:
        return "ok"
    return "restart" if active_jobs == 0 else "hold"
