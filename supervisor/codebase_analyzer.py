"""
supervisor/codebase_analyzer.py

Reads the supervisor codebase itself and produces:
  - A structured file tree string
  - Per-file content (truncated for very large files)
  - A compact "digest" suitable for injection into an LLM system prompt

Used by the self-evolution subsystem so the supervisor understands
exactly what it is judging before it asks opencode to modify code.
"""

from __future__ import annotations

import hashlib
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

# Files/dirs to skip when snapshotting
_IGNORE_DIRS  = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", ".checkpoints"}
_IGNORE_DIR_PREFIXES = (".",)
_IGNORE_EXTS  = {".pyc", ".pyo", ".egg-info", ".DS_Store"}
_MAX_FILE_CHARS = 6_000   # truncate individual files beyond this in the digest


@dataclass
class FileSnapshot:
    rel_path: str        # relative to repo root
    content: str         # (possibly truncated)
    truncated: bool
    sha256: str          # full-content hash


@dataclass
class CodebaseSnapshot:
    root: Path
    files: list[FileSnapshot] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Formatted views                                                      #
    # ------------------------------------------------------------------ #

    def tree(self) -> str:
        """ASCII file tree."""
        lines = [f"{self.root.name}/"]
        paths = sorted(f.rel_path for f in self.files)
        for p in paths:
            depth = p.count("/")
            name  = p.rsplit("/", 1)[-1] if "/" in p else p
            lines.append("  " * depth + f"└─ {name}")
        return "\n".join(lines)

    def digest_for_prompt(self, max_files: int = 30) -> str:
        """
        Compact multi-file listing suitable for an LLM system prompt.
        Keeps the most important files (Python source first, then others).
        """
        ranked = sorted(
            self.files,
            key=lambda f: (0 if f.rel_path.endswith(".py") else 1, f.rel_path),
        )[:max_files]

        parts: list[str] = [
            f"## Codebase snapshot  ({len(self.files)} files total)\n",
            "### File tree\n```\n" + self.tree() + "\n```\n",
        ]
        for snap in ranked:
            suffix = "  [TRUNCATED]" if snap.truncated else ""
            parts.append(
                f"### {snap.rel_path}{suffix}\n"
                f"```python\n{snap.content}\n```\n"
            )
        return "\n".join(parts)

    def file_hashes(self) -> dict[str, str]:
        """Map of rel_path → sha256 for change detection."""
        return {f.rel_path: f.sha256 for f in self.files}

    def changed_files(self, other: "CodebaseSnapshot") -> list[str]:
        """Files that differ between two snapshots (added, removed, modified)."""
        a = self.file_hashes()
        b = other.file_hashes()
        changed: list[str] = []
        for path in set(a) | set(b):
            if a.get(path) != b.get(path):
                changed.append(path)
        return sorted(changed)


# ------------------------------------------------------------------ #
# Public factory                                                       #
# ------------------------------------------------------------------ #

def snapshot_codebase(root: Path) -> CodebaseSnapshot:
    """Walk *root* and build a CodebaseSnapshot."""
    snap = CodebaseSnapshot(root=root)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _IGNORE_DIRS or any(part.startswith(p) for p in _IGNORE_DIR_PREFIXES) for part in path.parts):
            continue
        if path.suffix in _IGNORE_EXTS:
            continue
        _add_file(snap, path, root)
    return snap


def _add_file(snap: CodebaseSnapshot, path: Path, root: Path) -> None:
    try:
        raw = path.read_bytes()
        sha  = hashlib.sha256(raw).hexdigest()[:12]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = f"<binary file, {len(raw)} bytes>"
        truncated = len(text) > _MAX_FILE_CHARS
        snap.files.append(FileSnapshot(
            rel_path=str(path.relative_to(root)),
            content=text[:_MAX_FILE_CHARS] if truncated else text,
            truncated=truncated,
            sha256=sha,
        ))
    except OSError:
        pass  # skip unreadable files
