"""One dedicated log directory for every service process.

The launcher (start_suprun.ps1) mirrors this path when it redirects
stdout/stderr; the Python writers - crash forensics, guardian, capability
proxy - resolve here so no process scatters .log files in the repo root.
``SOP_LOG_DIR`` overrides the directory (tests point it at a tmp path).
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_DEFAULT = ROOT / "logs"


def log_dir() -> Path:
    path = Path(os.environ.get("SOP_LOG_DIR") or _DEFAULT)
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path(name: str) -> Path:
    return log_dir() / name
