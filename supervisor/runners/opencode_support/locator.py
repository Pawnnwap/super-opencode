from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from supervisor.utils.text_utils import coerce_str

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


def find_opencode(explicit: str = "") -> str:
    explicit = coerce_str(explicit, "opencode_executable (find_opencode arg)")

    if explicit:
        path = Path(explicit)
        if path.is_file():
            return explicit

    lookup_cmd = "where" if sys.platform == "win32" else "which"
    try:
        result = subprocess.run(
            [lookup_cmd, "opencode"],
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
                Path.home() / ".opencode" / "bin",
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
        "opencode not found on PATH or in known install directories.\n\n"
        "To fix:\n"
        "  • Windows:      npm install -g opencode-ai\n"
        "  • macOS/Linux:  curl -fsSL https://raw.githubusercontent.com/opencode-ai/opencode/main/install | bash\n"
        "  • Then restart the Streamlit app so it picks up the updated PATH.",
    )
