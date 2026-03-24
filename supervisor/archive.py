"""
supervisor/archive.py

Version archive system for storing complete workspace versions with timestamps.
Archives include code snapshots, test results, logs, and metadata.

Operations:
  - save(label, code_snapshot, results, logs) → create new archive
  - restore(archive) → restore workspace from archive
  - list() → sorted list of archived versions
  - delete(archive) → remove an archive
  - get_info(archive) → get archive metadata
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from supervisor.ignore_patterns import IgnoreMatcher

_ARCHIVE_DIR = "archive"
_IGNORE_DIRS = {".git", "__pycache__", ".venv", "venv", ".checkpoints", "archive"}
_SOURCE_EXTS = {".py", ".md", ".toml", ".cfg", ".ini", ".txt", ".yaml", ".yml"}


@dataclass
class ArchiveMetadata:
    label: str
    timestamp: float
    archive_id: str
    code_snapshot: dict[str, str]
    results: dict[str, Any] | None = None
    logs: str = ""
    opencode_version: str | None = None
    test_baseline: dict[str, Any] | None = None
    final_state: str = "unknown"
    files_archived: int = 0

    def age_s(self) -> float:
        return time.time() - self.timestamp

    def age_str(self) -> str:
        delta = self.age_s()
        if delta < 60:
            return f"{int(delta)}s ago"
        if delta < 3600:
            return f"{int(delta / 60)}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        return f"{int(delta / 86400)}d ago"

    def datetime_str(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ArchiveMetadata":
        return cls(**data)


@dataclass
class Archive:
    metadata: ArchiveMetadata
    path: Path

    def __str__(self) -> str:
        return f"[{self.metadata.datetime_str()}] {self.metadata.label}"

    def __repr__(self) -> str:
        return f"Archive({self.metadata.archive_id}, {self.metadata.label})"


class ArchiveManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._archive_root = workspace / _ARCHIVE_DIR
        self._archive_root.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        label: str,
        code_snapshot: dict[str, str] | None = None,
        results: dict[str, Any] | None = None,
        logs: str = "",
        opencode_version: str | None = None,
        test_baseline: dict[str, Any] | None = None,
        final_state: str = "unknown",
    ) -> Archive:
        ts = time.time()
        archive_id = f"{int(ts)}_{label.lower().replace(' ', '_')[:40]}"
        archive_path = self._archive_root / archive_id
        archive_path.mkdir(parents=True, exist_ok=True)

        if code_snapshot is None:
            code_snapshot = self._collect_source_files()

        metadata = ArchiveMetadata(
            label=label,
            timestamp=ts,
            archive_id=archive_id,
            code_snapshot=code_snapshot,
            results=results,
            logs=logs,
            opencode_version=opencode_version,
            test_baseline=test_baseline,
            final_state=final_state,
            files_archived=len(code_snapshot),
        )

        metadata_path = archive_path / "metadata.json"
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata.to_dict(), f, indent=2)

        code_dir = archive_path / "code"
        code_dir.mkdir(exist_ok=True)
        for rel_path, content in code_snapshot.items():
            file_path = code_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")

        if logs:
            logs_path = archive_path / "logs.txt"
            logs_path.write_text(logs, encoding="utf-8")

        if results:
            results_path = archive_path / "results.json"
            with open(results_path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)

        return Archive(metadata=metadata, path=archive_path)

    def restore(self, archive: Archive) -> list[str]:
        restored: list[str] = []
        code_dir = archive.path / "code"
        if not code_dir.exists():
            return restored

        for file_path in sorted(code_dir.rglob("*")):
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(code_dir)
            dst = self.workspace / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, dst)
            restored.append(str(rel))
        return restored

    def list(self) -> list[Archive]:
        archives: list[Archive] = []
        for d in sorted(self._archive_root.iterdir(), key=lambda x: x.name):
            if not d.is_dir():
                continue
            metadata_path = d / "metadata.json"
            if not metadata_path.exists():
                continue
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                metadata = ArchiveMetadata.from_dict(data)
                archives.append(Archive(metadata=metadata, path=d))
            except (json.JSONDecodeError, TypeError):
                continue
        return sorted(archives, key=lambda a: a.metadata.timestamp)

    def get(self, archive_id: str) -> Archive | None:
        archive_path = self._archive_root / archive_id
        if not archive_path.exists():
            return None
        metadata_path = archive_path / "metadata.json"
        if not metadata_path.exists():
            return None
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            metadata = ArchiveMetadata.from_dict(data)
            return Archive(metadata=metadata, path=archive_path)
        except (json.JSONDecodeError, TypeError):
            return None

    def delete(self, archive: Archive) -> bool:
        try:
            shutil.rmtree(archive.path)
            return True
        except Exception:
            return False

    def get_info(self, archive: Archive) -> dict[str, Any]:
        return {
            "id": archive.metadata.archive_id,
            "label": archive.metadata.label,
            "timestamp": archive.metadata.timestamp,
            "datetime": archive.metadata.datetime_str(),
            "age": archive.metadata.age_str(),
            "files_archived": archive.metadata.files_archived,
            "final_state": archive.metadata.final_state,
            "path": str(archive.path),
        }

    def prune_old_archives(self, keep_count: int = 10) -> list[str]:
        archives = self.list()
        if len(archives) <= keep_count:
            return []
        to_delete = archives[:-keep_count]
        deleted: list[str] = []
        for archive in to_delete:
            if self.delete(archive):
                deleted.append(archive.metadata.archive_id)
        return deleted

    def _collect_source_files(self, ignore_matcher: "IgnoreMatcher | None" = None) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in sorted(self.workspace.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _IGNORE_DIRS for part in path.relative_to(self.workspace).parts):
                continue
            if ignore_matcher and ignore_matcher.matches(path):
                continue
            if path.suffix in _SOURCE_EXTS:
                try:
                    rel = str(path.relative_to(self.workspace))
                    result[rel] = path.read_text(encoding="utf-8")
                except Exception:
                    pass
        return result

    def archive_count(self) -> int:
        return len(self.list())

    def get_latest(self) -> Archive | None:
        archives = self.list()
        return archives[-1] if archives else None

    def get_by_label(self, label: str) -> list[Archive]:
        return [a for a in self.list() if label.lower() in a.metadata.label.lower()]


class ProtectedPaths:
    PROTECTED_DIRS = {".opencode", ".checkpoints", "archive"}
    PROTECTED_FILES = {".opencode"}

    @classmethod
    def is_protected(cls, path: str | Path) -> bool:
        path_str = str(path).replace("\\", "/")
        parts = [p for p in path_str.split("/") if p]
        parts_set = set(parts)
        for protected in cls.PROTECTED_DIRS:
            if protected in parts_set:
                return True
        return False

    @classmethod
    def get_protected_dirs(cls) -> set[str]:
        return cls.PROTECTED_DIRS.copy()
