from __future__ import annotations

import sys
from pathlib import Path

from supervisor.runners.locator_common import find_executable

_NAMES = (
    ["opencode.cmd", "opencode.exe", "opencode.bat", "opencode"]
    if sys.platform == "win32"
    else ["opencode", "opencode.exe", "opencode.cmd", "opencode.bat"]
)

_WINDOWS_EXTRA_DIRS = [
    Path.home() / "AppData" / "Local" / "opencode",
    Path.home() / "AppData" / "Local" / "Programs" / "opencode",
    Path.home() / "AppData" / "Roaming" / "npm",
    Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / ".bin",
    Path.home() / ".local" / "bin",
    Path.home() / "bin",
    Path("C:/Program Files/opencode"),
    Path("C:/Program Files (x86)/opencode"),
    Path("C:/tools/opencode"),
]

_UNIX_EXTRA_DIRS = [
    Path.home() / ".opencode" / "bin",
    Path.home() / ".npm-global" / "bin",
    Path.home() / ".local" / "bin",
    Path("/usr/local/bin"),
    Path("/usr/bin"),
]

_NOT_FOUND_MESSAGE = (
    "opencode not found on PATH or in known install directories.\n\n"
    "To fix:\n"
    "  • Windows:      npm install -g opencode-ai\n"
    "  • macOS/Linux:  curl -fsSL https://opencode.ai/install | bash\n"
    "  • Then restart the Streamlit app so it picks up the updated PATH."
)


def find_opencode(explicit: str = "") -> str:
    return find_executable(
        "opencode",
        names=_NAMES,
        windows_extra_dirs=_WINDOWS_EXTRA_DIRS,
        unix_extra_dirs=_UNIX_EXTRA_DIRS,
        not_found_message=_NOT_FOUND_MESSAGE,
        explicit=explicit,
    )
