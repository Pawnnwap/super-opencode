"""
supervisor/checkpoint.py

Lightweight file-copy checkpointing for the self-evolution loop.

Each checkpoint is a timestamped folder under  <workspace>/.checkpoints/
containing a verbatim copy of every tracked source file.

Operations:
  - save(label)  → copy current source into a new checkpoint dir
  - restore(cp)  → overwrite workspace files from a checkpoint
  - list()       → sorted list of saved Checkpoint objects
  - diff(a, b)   → list of files that changed between two checkpoints
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

_CHECKPOINT_DIR = ".checkpoints"
_IGNORE_DIRS    = {".git", "__pycache__", ".venv", "venv", ".checkpoints"}
_IGNORE_DIR_PREFIXES = (".",)
_SOURCE_EXTS    = {".py", ".md", ".toml", ".cfg", ".ini", ".txt", ".yaml", ".yml"}


@dataclass
class Checkpoint:
    label: str
    path: Path
    timestamp: float

    def age_s(self) -> float:
        return time.time() - self.timestamp

    def __str__(self) -> str:
        import datetime
        dt = datetime.datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S")
        return f"[{dt}] {self.label}"


class CheckpointManager:
    def __init__(self, workspace: Path):
        self.workspace  = workspace
        self._cp_root   = workspace / _CHECKPOINT_DIR
        self._cp_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def save(self, label: str) -> Checkpoint:
        """Snapshot current workspace source files into a new checkpoint."""
        ts    = time.time()
        slug  = label.lower().replace(" ", "_")[:40]
        name  = f"{int(ts)}_{slug}"
        dest  = self._cp_root / name
        dest.mkdir(parents=True, exist_ok=True)

        for src in self._source_files():
            rel  = src.relative_to(self.workspace)
            dst  = dest / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        return Checkpoint(label=label, path=dest, timestamp=ts)

    def restore(self, cp: Checkpoint) -> list[str]:
        """
        Overwrite workspace files with those from *cp*.
        Returns list of restored file paths (relative).
        """
        restored: list[str] = []
        for src in sorted(cp.path.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(cp.path)
            dst = self.workspace / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored.append(str(rel))
        return restored

    def list(self) -> list[Checkpoint]:
        cps: list[Checkpoint] = []
        for d in sorted(self._cp_root.iterdir()):
            if not d.is_dir():
                continue
            parts = d.name.split("_", 1)
            try:
                ts    = float(parts[0])
                label = parts[1].replace("_", " ") if len(parts) > 1 else d.name
            except ValueError:
                ts    = 0.0
                label = d.name
            cps.append(Checkpoint(label=label, path=d, timestamp=ts))
        return sorted(cps, key=lambda c: c.timestamp)

    def diff(self, a: Checkpoint, b: Checkpoint) -> list[str]:
        """File paths (relative) that differ between checkpoints a and b."""
        import hashlib

        def hashes(cp: Checkpoint) -> dict[str, str]:
            result = {}
            for f in cp.path.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(cp.path))
                    result[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
            return result

        ha, hb = hashes(a), hashes(b)
        changed = [p for p in set(ha) | set(hb) if ha.get(p) != hb.get(p)]
        return sorted(changed)

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    def _source_files(self):
        for path in sorted(self.workspace.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _IGNORE_DIRS or any(part.startswith(p) for p in _IGNORE_DIR_PREFIXES) for part in path.relative_to(self.workspace).parts):
                continue
            if path.suffix in _SOURCE_EXTS:
                yield path
