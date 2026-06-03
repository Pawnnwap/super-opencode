from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from supervisor.utils.text_utils import coerce_str


def find_executable(
    tool: str,
    *,
    names: list[str],
    windows_extra_dirs: list[Path],
    unix_extra_dirs: list[Path],
    not_found_message: str,
    explicit: str = "",
) -> str:
    """Locate *tool* on PATH or in known install dirs (cross-platform).

    Uses ``where`` on Windows and ``which`` elsewhere, then falls back to the
    supplied platform-specific install dirs plus the global npm prefix. Raises
    ``FileNotFoundError`` with *not_found_message* when nothing is found.
    """
    explicit = coerce_str(explicit, f"{tool}_executable (find arg)")

    if explicit:
        path = Path(explicit)
        if path.is_file():
            return explicit

    lookup_cmd = "where" if sys.platform == "win32" else "which"
    try:
        result = subprocess.run(
            [lookup_cmd, tool],
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
        search_dirs.extend(windows_extra_dirs)
    else:
        search_dirs.extend(unix_extra_dirs)

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
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)

    raise FileNotFoundError(not_found_message)
