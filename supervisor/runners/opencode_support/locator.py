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
    "  • Then restart the web UI so it picks up the updated PATH."
)


def _resolve_windows_native_exe(resolved: str) -> str:
    """Prefer the bundled native opencode.exe over an npm .cmd shim.

    Spawning through the ``.cmd`` shim requires ``shell=True``, and cmd.exe
    mangles long quoted multi-line prompts (truncation/metacharacter issues):
    agents then receive an empty task and just wait for instructions. The npm
    package ships a native ``opencode-windows-x64/bin/opencode.exe`` that can
    be spawned directly with ``shell=False`` and a cleanly quoted argument
    list, which sidesteps cmd.exe entirely.
    """
    path = Path(resolved)
    if path.suffix.lower() != ".cmd" and path.suffix.lower() != ".bat":
        return resolved
    npm_root = path.parent
    candidates = [
        npm_root / "node_modules" / "opencode-ai" / "node_modules"
        / "opencode-windows-x64" / "bin" / "opencode.exe",
    ]
    # Independent (non-npm) installs may sit next to the shim instead.
    if (path.parent / "opencode.exe").exists():
        candidates.insert(0, path.parent / "opencode.exe")
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return resolved


def find_opencode(explicit: str = "") -> str:
    resolved = find_executable(
        "opencode",
        names=_NAMES,
        windows_extra_dirs=_WINDOWS_EXTRA_DIRS,
        unix_extra_dirs=_UNIX_EXTRA_DIRS,
        not_found_message=_NOT_FOUND_MESSAGE,
        explicit=explicit,
    )
    if sys.platform == "win32":
        # Even an explicitly configured .cmd shim spawns better through its
        # own native exe (see _resolve_windows_native_exe).
        resolved = _resolve_windows_native_exe(resolved)
    return resolved
