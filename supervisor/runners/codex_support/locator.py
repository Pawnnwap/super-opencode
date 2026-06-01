from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from supervisor.utils.text_utils import coerce_str

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


def find_codex(explicit: str = "") -> str:
    """Locate the codex executable on PATH or in known install dirs.

    Cross-platform: uses ``where`` on Windows and ``which`` elsewhere, then
    falls back to a list of well-known install directories. Raises
    ``FileNotFoundError`` with install instructions when nothing is found.
    """
    explicit = coerce_str(explicit, "codex_executable (find_codex arg)")

    if explicit:
        path = Path(explicit)
        if path.is_file():
            return explicit

    lookup_cmd = "where" if sys.platform == "win32" else "which"
    try:
        result = subprocess.run(
            [lookup_cmd, "codex"],
            capture_output=True,
            text=True,
            check=True,
        )
        candidates = [
            line.strip() for line in result.stdout.splitlines() if line.strip()
        ]
        if sys.platform == "win32":
            exec_exts = (".exe", ".cmd", ".bat", ".ps1")
            candidates = [
                path for path in candidates if path.lower().endswith(exec_exts)
            ] or candidates
        for path in candidates:
            if Path(path).is_file():
                return path
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    search_dirs: list[Path] = []
    if sys.platform == "win32":
        search_dirs.extend(_WINDOWS_EXTRA_DIRS)
    else:
        search_dirs.extend(
            [
                Path.home() / ".codex" / "bin",
                Path.home() / ".npm-global" / "bin",
                Path.home() / ".local" / "bin",
                Path("/usr/local/bin"),
                Path("/usr/bin"),
            ]
        )

    try:
        npm_prefix = subprocess.run(
            ["npm", "prefix", "-g"],
            capture_output=True,
            text=True,
            check=True,
            shell=(sys.platform == "win32"),
            timeout=5,
        ).stdout.strip()
        if npm_prefix:
            prefix_path = Path(npm_prefix)
            search_dirs.append(prefix_path)
            if sys.platform != "win32":
                search_dirs.append(prefix_path / "bin")
    except Exception:
        pass

    for directory in search_dirs:
        for name in _NAMES:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)

    raise FileNotFoundError(
        "codex not found on PATH or in known install directories.\n\n"
        "To fix:\n"
        "  • Any OS (npm):  npm install -g @openai/codex\n"
        "  • macOS (brew):  brew install codex\n"
        "  • Or download a release binary from the codex repo and add it to PATH.\n"
        "  • Then restart the Streamlit app so it picks up the updated PATH.",
    )
