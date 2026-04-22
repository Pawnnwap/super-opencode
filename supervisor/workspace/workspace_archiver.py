"""supervisor/workspace_archiver.py

Workspace archiving mechanism that preserves historical versions
instead of deleting old files.

Archive structure:
    <workspace>/.archive/
        run_<timestamp>_<counter>/
            code/
                ... (preserved source files)
            results/
                ... (preserved result files)
            logs/
                ... (preserved log files)
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from supervisor.utils.filesystem.file_ops import copy_tree_to_workspace
from supervisor.utils.filesystem.file_permissions import remove_file_readonly
from supervisor.utils.filesystem.path_filters import should_skip_path
from supervisor.workspace.ignore_patterns import IgnoreMatcher

logger = logging.getLogger(__name__)


_ARCHIVE_DIR = ".archive"
_ARCHIVE_EXTRA_IGNORE_DIRS = {
    "_deps",
    ". Dune",
}
_ARCHIVE_SUBDIRS = {
    "code": {".py", ".md", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".txt", ".rst"},
    "results": {".json", ".html", ".csv", ".xml"},
    "logs": {".log", ".txt"},
}


@dataclass
class ArchiveResult:
    success: bool
    archive_path: Path | None
    archived_files: list[str]
    message: str


class WorkspaceArchiver:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.archive_root = self.workspace / _ARCHIVE_DIR
        self._archive_counter = self._load_counter()

    def _load_counter(self) -> int:
        counter_file = self.archive_root / ".archive_counter"
        if counter_file.exists():
            try:
                return int(counter_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pass
        return 0

    def _save_counter(self) -> None:
        self.archive_root.mkdir(parents=True, exist_ok=True)
        counter_file = self.archive_root / ".archive_counter"
        if counter_file.exists():
            try:
                remove_file_readonly(str(counter_file))
            except Exception as exc:
                logger.warning("Could not remove read-only on counter file %s: %s", counter_file, exc)
        counter_file.write_text(str(self._archive_counter), encoding="utf-8")

    def _get_archive_subdir(self, filename: str) -> str:
        ext = Path(filename).suffix.lower()
        for subdir, extensions in _ARCHIVE_SUBDIRS.items():
            if ext in extensions:
                return subdir
        return "other"

    def _should_archive(
        self,
        path: Path,
        ignore_matcher: IgnoreMatcher | None = None,
        workspace_ignore: IgnoreMatcher | None = None,
    ) -> bool:
        if not path.is_file():
            return False
        if should_skip_path(path, extra_dirs=_ARCHIVE_EXTRA_IGNORE_DIRS):
            return False
        if ignore_matcher and ignore_matcher.matches(path):
            return False
        if workspace_ignore:
            rel = path.relative_to(self.workspace)
            if workspace_ignore.matches(str(rel)):
                return False
        return True

    def archive_workspace(
        self,
        label: str = "",
        files_to_archive: list[str] | None = None,
        ignore_matcher: IgnoreMatcher | None = None,
    ) -> ArchiveResult:
        """Archive the current workspace content to a timestamped archive folder.

        Automatically loads and applies .opencodeignore patterns from the workspace root.

        Args:
            label: Optional label for the archive
            files_to_archive: Specific files to archive (None = all eligible files)
            ignore_matcher: Optional ignore matcher to filter files

        Returns:
            ArchiveResult with success status, archive path, and list of archived files

        """
        self._archive_counter += 1
        self._save_counter()

        ts = int(time.time())
        slug = (
            label.lower().replace(" ", "_").replace("-", "_")[:30]
            if label
            else "workspace"
        )
        archive_name = f"run_{ts}_{self._archive_counter:04d}_{slug}"
        archive_path = self.archive_root / archive_name

        workspace_ignore = IgnoreMatcher(self.workspace)
        workspace_ignore.load_from_workspace(self.workspace)

        try:
            self.archive_root.mkdir(parents=True, exist_ok=True)
            archive_path.mkdir(parents=True, exist_ok=True)

            for subdir in ["code", "results", "logs", "other"]:
                (archive_path / subdir).mkdir(exist_ok=True)

            archived: list[str] = []

            def _archive_file(src_path: Path) -> None:
                rel = src_path.relative_to(self.workspace)
                subdir = self._get_archive_subdir(rel.name)
                dst = archive_path / subdir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dst)
                archived.append(str(rel))

            if files_to_archive is not None:
                for file_path in files_to_archive:
                    src = self.workspace / file_path
                    if (
                        src.exists()
                        and src.is_file()
                        and self._should_archive(src, ignore_matcher, workspace_ignore)
                    ):
                        _archive_file(src)
            else:
                for src in sorted(self.workspace.rglob("*")):
                    if self._should_archive(src, ignore_matcher, workspace_ignore):
                        _archive_file(src)

            metadata = {
                "timestamp": ts,
                "label": label,
                "archived_files": archived,
                "workspace": str(self.workspace),
            }
            (archive_path / "archive_metadata.json").write_text(
                json.dumps(metadata, indent=2), encoding="utf-8",
            )

            return ArchiveResult(
                success=True,
                archive_path=archive_path,
                archived_files=archived,
                message=f"Archived {len(archived)} files to {archive_path}",
            )

        except Exception as exc:
            logger.error("Archive failed for workspace %s: %s", self.workspace, exc, exc_info=True)
            if archive_path.exists():
                shutil.rmtree(archive_path, ignore_errors=True)
            return ArchiveResult(
                success=False,
                archive_path=None,
                archived_files=[],
                message=f"Archiving failed: {exc}",
            )

    def archive_before_new_run(self) -> ArchiveResult:
        """Archive the current workspace state before starting a new supervisor run.
        This is typically called at the beginning of a supervisor loop execution.
        """
        return self.archive_workspace(label="before_new_run")

    def list_archives(self) -> list[dict]:
        """List all available archives with their metadata."""
        archives = []
        if not self.archive_root.exists():
            return archives

        for archive_dir in sorted(self.archive_root.iterdir()):
            if not archive_dir.is_dir() or archive_dir.name.startswith("."):
                continue
            metadata_file = archive_dir / "archive_metadata.json"
            metadata = {"name": archive_dir.name, "path": str(archive_dir)}
            if metadata_file.exists():
                try:
                    metadata.update(
                        json.loads(metadata_file.read_text(encoding="utf-8")),
                    )
                except (json.JSONDecodeError, OSError):
                    pass
            archives.append(metadata)
        return archives

    def restore_archive(self, archive_path: Path) -> list[str]:
        """Restore files from an archive back to the workspace.
        Returns list of restored file paths.
        """
        restored: list[str] = []
        if not archive_path.is_dir():
            return restored

        for subdir in archive_path.iterdir():
            if not subdir.is_dir():
                continue
            restored.extend(copy_tree_to_workspace(subdir, self.workspace))
        return restored

    def get_archive_stats(self) -> dict:
        """Get statistics about the archive."""
        archives = self.list_archives()
        total_files = sum(len(a.get("archived_files", [])) for a in archives)
        total_size = sum(
            sum(
                f.stat().st_size
                for f in (
                    Path(a["path"]).rglob("*") if Path(a["path"]).exists() else []
                )
            )
            for a in archives
        )
        return {
            "archive_count": len(archives),
            "total_files": total_files,
            "total_size_bytes": total_size,
            "current_counter": self._archive_counter,
        }
