from __future__ import annotations

import sys
from pathlib import Path

from supervisor.runners.locator_common import find_executable

# pi ships via npm (@earendil-works/pi-coding-agent), which drops .cmd shims
# on Windows. Mirror opencode's/codex's name ordering.
_NAMES = (
    ["pi.cmd", "pi.exe", "pi.bat", "pi"]
    if sys.platform == "win32"
    else ["pi", "pi.exe", "pi.cmd", "pi.bat"]
)

_WINDOWS_EXTRA_DIRS = [
    Path.home() / ".pi" / "bin",
    Path.home() / "AppData" / "Local" / "pi",
    Path.home() / "AppData" / "Local" / "Programs" / "pi",
    Path.home() / "AppData" / "Roaming" / "npm",
    Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / ".bin",
    Path.home() / ".local" / "bin",
    Path.home() / "bin",
    Path("C:/Program Files/pi"),
    Path("C:/Program Files (x86)/pi"),
    Path("C:/tools/pi"),
]

_UNIX_EXTRA_DIRS = [
    Path.home() / ".pi" / "bin",
    Path.home() / ".npm-global" / "bin",
    Path.home() / ".local" / "bin",
    Path("/usr/local/bin"),
    Path("/usr/bin"),
]

_NOT_FOUND_MESSAGE = (
    "pi not found on PATH or in known install directories.\n\n"
    "To fix:\n"
    "  • Any OS (npm):  npm install -g @earendil-works/pi-coding-agent\n"
    "  • Or install per the pi repo instructions and add it to PATH.\n"
    "  • Then restart the web UI so it picks up the updated PATH."
)


def find_pi(explicit: str = "") -> str:
    return find_executable(
        "pi",
        names=_NAMES,
        windows_extra_dirs=_WINDOWS_EXTRA_DIRS,
        unix_extra_dirs=_UNIX_EXTRA_DIRS,
        not_found_message=_NOT_FOUND_MESSAGE,
        explicit=explicit,
    )
