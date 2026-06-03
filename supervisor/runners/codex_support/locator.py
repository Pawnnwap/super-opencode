from __future__ import annotations

import sys
from pathlib import Path

from supervisor.runners.locator_common import find_executable

# Codex CLI ships as a native binary (Rust) and also via npm (@openai/codex),
# which drops .cmd/.exe shims on Windows. Mirror opencode's name ordering.
_NAMES = (
    ["codex.cmd", "codex.exe", "codex.bat", "codex"]
    if sys.platform == "win32"
    else ["codex", "codex.exe", "codex.cmd", "codex.bat"]
)

_WINDOWS_EXTRA_DIRS = [
    Path.home() / ".codex" / "bin",
    Path.home() / "AppData" / "Local" / "codex",
    Path.home() / "AppData" / "Local" / "Programs" / "codex",
    Path.home() / "AppData" / "Roaming" / "npm",
    Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / ".bin",
    Path.home() / ".local" / "bin",
    Path.home() / "bin",
    Path("C:/Program Files/codex"),
    Path("C:/Program Files (x86)/codex"),
    Path("C:/tools/codex"),
]

_UNIX_EXTRA_DIRS = [
    Path.home() / ".codex" / "bin",
    Path.home() / ".npm-global" / "bin",
    Path.home() / ".local" / "bin",
    Path("/usr/local/bin"),
    Path("/usr/bin"),
]

_NOT_FOUND_MESSAGE = (
    "codex not found on PATH or in known install directories.\n\n"
    "To fix:\n"
    "  • Any OS (npm):  npm install -g @openai/codex\n"
    "  • macOS (brew):  brew install codex\n"
    "  • Or download a release binary from the codex repo and add it to PATH.\n"
    "  • Then restart the Streamlit app so it picks up the updated PATH."
)


def find_codex(explicit: str = "") -> str:
    return find_executable(
        "codex",
        names=_NAMES,
        windows_extra_dirs=_WINDOWS_EXTRA_DIRS,
        unix_extra_dirs=_UNIX_EXTRA_DIRS,
        not_found_message=_NOT_FOUND_MESSAGE,
        explicit=explicit,
    )
