from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from supervisor.utils.text_utils import coerce_str


def _trusted_executable_roots() -> tuple[Path, ...]:
    home = Path.home()
    candidates = [
        home / ".local" / "bin",
        home / ".cargo" / "bin",
        home / ".bun" / "bin",
        home / ".volta" / "bin",
        home / ".conda" / "envs",
        home / "miniconda3" / "envs",
        home / "anaconda3" / "envs",
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/opt/homebrew/bin"),
        Path("C:/Program Files"),
        Path("C:/Program Files (x86)"),
    ]
    for env_name in (
        "APPDATA",
        "LOCALAPPDATA",
        "CONDA_PREFIX",
        "SOP_TRUSTED_EXECUTABLE_ROOTS",
    ):
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        for value in raw.split(os.pathsep):
            if not value:
                continue
            path = Path(value)
            if env_name == "APPDATA":
                path = path / "npm"
            elif env_name == "LOCALAPPDATA":
                path = path / "Programs"
            candidates.append(path)
    return tuple(path for path in candidates if path.is_dir())


def _trusted_explicit_executable(value: str) -> str | None:
    path = Path(value).expanduser()
    if not path.is_file():
        return None
    resolved = path.resolve()
    if any(
        resolved.is_relative_to(root.resolve()) for root in _trusted_executable_roots()
    ):
        return str(resolved)
    return None


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
        trusted = _trusted_explicit_executable(explicit)
        if trusted:
            return trusted

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
            trusted = _trusted_explicit_executable(path)
            if trusted:
                return trusted
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
    except Exception:  # noqa: BLE001, S110 - npm prefix is an optional search dir
        pass

    for directory in search_dirs:
        for name in names:
            candidate = directory / name
            trusted = _trusted_explicit_executable(str(candidate))
            if trusted:
                return trusted

    raise FileNotFoundError(not_found_message)
