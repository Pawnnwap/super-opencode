from __future__ import annotations

import os
import shutil
from pathlib import Path


def clean_workspace_artifacts(workspace: Path) -> dict:
    file_suffixes = {".bak", ".isorted", ".pyc", ".pyo"}
    dir_names = {"__pycache__", ".pytest_cache", ".ruff_cache"}

    removed_files: list[str] = []
    removed_dirs: list[str] = []
    errors: list[str] = []

    if not workspace.is_dir():
        return {"skipped": True, "reason": "workspace does not exist or is not a directory"}

    def on_walk_error(err: OSError) -> None:
        errors.append(f"walk: {err}")

    for dirpath, dirnames, filenames in os.walk(
        str(workspace),
        topdown=True,
        onerror=on_walk_error,
    ):
        to_delete = [d for d in dirnames if d in dir_names or d.endswith(".egg-info")]
        for dname in to_delete:
            dpath = Path(dirpath) / dname
            try:
                shutil.rmtree(dpath, ignore_errors=False)
                removed_dirs.append(str(dpath))
            except OSError as exc:
                errors.append(f"dir {dpath}: {exc}")
        dirnames[:] = [d for d in dirnames if d not in to_delete]

        for fname in filenames:
            if Path(fname).suffix.lower() in file_suffixes:
                fpath = Path(dirpath) / fname
                try:
                    fpath.unlink()
                    removed_files.append(str(fpath))
                except OSError as exc:
                    errors.append(f"file {fpath}: {exc}")

    return {
        "workspace": str(workspace),
        "removed_files": len(removed_files),
        "removed_dirs": len(removed_dirs),
        "errors": errors,
    }
