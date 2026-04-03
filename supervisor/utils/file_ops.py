"""File operation utilities."""

import shutil
from pathlib import Path


def copy_tree_to_workspace(src_dir: Path, workspace_dir: Path) -> list[str]:
    """Copies all files from src_dir to workspace_dir preserving directory structure.
    Returns a list of relative paths that were restored/copied.
    """
    restored: list[str] = []
    for src in sorted(src_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(src_dir)
        dst = workspace_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored.append(str(rel))
    return restored
