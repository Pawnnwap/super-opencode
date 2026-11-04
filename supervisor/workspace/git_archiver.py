"""supervisor/git_archiver.py — incremental workspace snapshots via git.

Drop-in backend replacing the full-copy :class:`WorkspaceArchiver`: same
public surface (``archive_workspace`` / ``archive_before_new_run`` /
``list_archives`` / ``restore_archive`` / ``get_archive_stats`` +
:class:`ArchiveResult`), so no loop or runner code changes behaviour beyond
which class the factory hands them.

Why: the copy backend wrote EVERY eligible file into EVERY snapshot —
``.archive/`` once grew to 49 GB. Git already solves content-addressed
incremental storage:

- one private bare repo at ``<workspace>/.archive/snapshots.git``; all
  commands run with ``--git-dir``/``--work-tree`` so the workspace's own
  ``.git`` (if any) is never touched and no global git config is needed;
- one snapshot = ``git add -A`` → ``write-tree`` → ``commit-tree`` WITHOUT
  ``-p`` (an orphan commit): unchanged blobs are shared across snapshots by
  hash, and no linear branch pins old history — so ``keep_last`` retention
  (delete the oldest ``snap/*`` tags + ``gc --prune=now``) actually frees
  the objects only those snapshots referenced;
- one lightweight tag ``snap/<snapshot-name>`` per snapshot carries it.

Compatibility with the copy world:

- snapshot names keep the ``run_<ts>_<counter>_<slug>`` shape and the
  shared ``.archive_counter`` file, so switching backends stays monotonic;
- ``ArchiveResult.archive_path`` is the logical ref path
  ``<workspace>/.archive/refs/<name>``; ``restore_archive`` recognizes that
  prefix and checkouts the tag (overlay semantics — files added after the
  snapshot are NOT deleted, same as the copy restore), otherwise it treats
  the argument as a legacy snapshot directory and copies it back;
- ``list_archives`` merges legacy ``run_*`` directories with git tags.

Exclusions are regenerated into the repo's ``info/exclude`` before every
snapshot (never the workspace's ``.gitignore``): the same ignore sources as
the copy backend (canonical skip dirs, dot-prefixed entries, extra dirs,
the workspace's ``.opencodeignore``) plus ``*.log`` — rotating prompt logs
were never code state, and a 64 MB log rewritten per snapshot is exactly
the bloat this backend exists to prevent.

If git is unavailable, or git operations fail twice in a row, the archiver
delegates everything to the copy backend for the rest of the process.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from supervisor.utils.filesystem.path_filters import DEFAULT_IGNORE_DIRS
from supervisor.workspace.ignore_patterns import IgnoreMatcher
from supervisor.workspace.workspace_archiver import (
    _ARCHIVE_EXTRA_IGNORE_DIRS,
    ArchiveResult,
    WorkspaceArchiver,
    load_archive_counter,
    save_archive_counter,
)

logger = logging.getLogger(__name__)

ARCHIVE_DIR = ".archive"
REPO_DIRNAME = "snapshots.git"
REFS_DIRNAME = "refs"
TAG_PREFIX = "snap/"

# *.log deviates from the copy backend deliberately: a rotated log is a new
# blob every snapshot, which re-introduces full-copy growth in object form.
# logs/ is excluded because it is the run's *evidence base*
# (feasibility_facts.json, gate_results_last.json): archiving it swept the
# facts into .archive/run_*_before_new_run right before each fresh cycle, so
# every relaunch started with an empty evidence base and the blocked triggers
# could never accumulate a streak. It is run state, not workspace content.
_EXTRA_EXCLUDE_LINES = ["*.log", "logs/"]

# Metadata caps: commit messages are not the place for a 10k-file listing.
_MAX_META_FILES = 500

# Windows caps CreateProcess argv at 32767 chars; batch pathspecs well below.
_PATHSPEC_BATCH = 60

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")


class GitArchiverError(RuntimeError):
    pass


def _batched(items: list[str], size: int = _PATHSPEC_BATCH):
    for start in range(0, len(items), size):
        yield items[start : start + size]


class GitWorkspaceArchiver:
    """Incremental (git-backed) workspace archiver, copy-backend API."""

    def __init__(self, workspace: Path, keep_last: int = 5, git_executable: str = ""):
        self.workspace = Path(workspace).resolve()
        self.archive_root = self.workspace / ARCHIVE_DIR
        self.repo_dir = self.archive_root / REPO_DIRNAME
        self.refs_root = self.archive_root / REFS_DIRNAME
        self.keep_last = keep_last
        self._git_exe = git_executable or shutil.which("git") or ""
        self._fallback: WorkspaceArchiver | None = (
            None if self._git_exe else WorkspaceArchiver(self.workspace, keep_last)
        )
        if not self._git_exe:
            logger.warning(
                "git not found on PATH — workspace archiver falls back to full-copy snapshots",
            )
        self._failures = 0
        self._lock = threading.Lock()
        self._archive_counter = self._load_counter()

    # ------------------------------------------------------------ plumbing

    def _git(
        self,
        *args: str,
        index: str | None = None,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        cmd = [
            self._git_exe,
            "--git-dir", str(self.repo_dir),
            "--work-tree", str(self.workspace),
            "-c", "user.name=supervisor-archive",
            "-c", "user.email=supervisor@local",
            "-c", "core.autocrlf=false",
            "-c", "core.fileMode=false",
            "-c", "core.quotepath=false",
            *args,
        ]
        env = {**os.environ}
        if index:
            env["GIT_INDEX_FILE"] = index
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=input_text,
            cwd=str(self.workspace),
            env=env,
        )
        if check and proc.returncode != 0:
            raise GitArchiverError(
                f"git {' '.join(args[:3])} failed ({proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[:400]}"
            )
        return proc

    def _ensure_repo(self) -> None:
        if (self.repo_dir / "HEAD").exists():
            return
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [self._git_exe, "init", "--bare", "--quiet", str(self.repo_dir)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise GitArchiverError(f"git init --bare failed: {proc.stderr.strip()[:400]}")

    def _exclude_lines(self, ignore_matcher: IgnoreMatcher | None) -> list[str]:
        workspace_ignore = IgnoreMatcher(self.workspace)
        workspace_ignore.load_from_workspace(self.workspace)
        lines: list[str] = [
            f"{ARCHIVE_DIR}/",
            *(f"{d}/" for d in sorted(DEFAULT_IGNORE_DIRS)),
            *(f"{d}/" for d in sorted(_ARCHIVE_EXTRA_IGNORE_DIRS)),
            ".*",
            *_EXTRA_EXCLUDE_LINES,
        ]
        for matcher in (workspace_ignore, ignore_matcher):
            if matcher is not None:
                lines.extend(matcher.get_patterns())
        # Dedup, keep order, drop empties.
        seen: set[str] = set()
        return [ln for ln in (l.strip() for l in lines)
                if ln and not (ln in seen or seen.add(ln))]

    def _write_excludes(self, ignore_matcher: IgnoreMatcher | None) -> None:
        exclude_path = self.repo_dir / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text(
            "\n".join(self._exclude_lines(ignore_matcher)) + "\n", encoding="utf-8",
        )

    def _load_counter(self) -> int:
        return load_archive_counter(self.archive_root)

    def _save_counter(self) -> None:
        save_archive_counter(self.archive_root, self._archive_counter)

    # ------------------------------------------------------------- public

    def archive_workspace(
        self,
        label: str = "",
        files_to_archive: list[str] | None = None,
        ignore_matcher: IgnoreMatcher | None = None,
    ) -> ArchiveResult:
        if self._fallback is not None:
            return self._fallback.archive_workspace(
                label=label, files_to_archive=files_to_archive, ignore_matcher=ignore_matcher,
            )
        with self._lock:
            try:
                return self._snapshot(label, files_to_archive, ignore_matcher)
            except Exception as exc:
                self._failures += 1
                logger.warning(
                    "git archive attempt failed (%d/2): %s", self._failures, exc,
                )
                if self._failures >= 2:
                    logger.warning(
                        "git archiver disabled for this process — falling back to "
                        "full-copy snapshots",
                    )
                    self._fallback = WorkspaceArchiver(self.workspace, self.keep_last)
                    return self._fallback.archive_workspace(
                        label=label,
                        files_to_archive=files_to_archive,
                        ignore_matcher=ignore_matcher,
                    )
                return ArchiveResult(
                    success=False, archive_path=None, archived_files=[],
                    message=f"Archiving failed: {exc}",
                )

    def archive_before_new_run(self) -> ArchiveResult:
        return self.archive_workspace(label="before_new_run")

    def list_archives(self) -> list[dict]:
        archives: list[dict] = self._legacy_archives()
        if self._fallback is not None or not (self.repo_dir / "HEAD").exists():
            return archives
        try:
            tags = self._git("tag", "-l", f"{TAG_PREFIX}run_*").stdout.split()
            for tag in tags:
                name = tag[len(TAG_PREFIX):]
                metadata = {
                    "name": name,
                    "path": str(self.refs_root / name),
                    "backend": "git",
                }
                message = self._git("log", "-1", "--format=%B", tag).stdout
                json_block = message.split("\n\n", 1)[-1].strip()
                try:
                    metadata.update(json.loads(json_block))
                except json.JSONDecodeError:
                    pass
                archives.append(metadata)
        except (GitArchiverError, OSError) as exc:
            logger.warning("git list_archives failed: %s", exc)
        return sorted(archives, key=lambda a: a.get("name", ""))

    def restore_archive(self, archive_path: Path) -> list[str]:
        archive_path = Path(archive_path)
        try:
            relative = archive_path.resolve().relative_to(self.refs_root)
        except ValueError:
            relative = None
        if relative is None or len(relative.parts) != 1:
            # Legacy snapshot directory → copy it back the old way.
            return WorkspaceArchiver(self.workspace, self.keep_last).restore_archive(
                archive_path,
            )
        tag = f"{TAG_PREFIX}{relative.parts[0]}"
        try:
            tree_files = self._git(
                "ls-tree", "-r", "--name-only", tag,
            ).stdout.splitlines()
            # Dedicated index so the restore never disturbs the snapshot
            # index; checkout <tag> -- . overlays files (never deletes).
            self._git(
                "checkout", tag, "--", ".",
                index=str(self.repo_dir / "index-restore"),
            )
            return tree_files
        except (GitArchiverError, OSError) as exc:
            logger.error("git restore of %s failed: %s", tag, exc)
            return []

    def get_archive_stats(self) -> dict:
        archives = self.list_archives()
        total_size = self._tree_size(self.archive_root)
        return {
            "archive_count": len(archives),
            "total_files": sum(int(a.get("archived_count", 0)) for a in archives),
            "total_size_bytes": total_size,
            "current_counter": self._archive_counter,
            "backend": "copy" if self._fallback is not None else "git",
        }

    # ------------------------------------------------------------ internals

    def _snapshot(
        self,
        label: str,
        files_to_archive: list[str] | None,
        ignore_matcher: IgnoreMatcher | None,
    ) -> ArchiveResult:
        self._ensure_repo()
        self._write_excludes(ignore_matcher)

        self._archive_counter += 1
        self._save_counter()
        ts = int(time.time())
        slug = (
            _SLUG_RE.sub("_", label.lower())[:30].strip("_") if label else "workspace"
        )
        name = f"run_{ts}_{self._archive_counter:04d}_{slug}"
        tag = f"{TAG_PREFIX}{name}"

        index_full = str(self.repo_dir / "index-full")
        workspace_ignore = IgnoreMatcher(self.workspace)
        workspace_ignore.load_from_workspace(self.workspace)
        if files_to_archive is None:
            self._git("add", "-A", "--", ".", index=index_full)
            self._drop_newly_excluded_tracked(index_full, ignore_matcher, workspace_ignore)
        else:
            # Partial snapshot: a throwaway index seeded with only the
            # requested (still-eligible) files, mirroring the copy backend.
            index_partial = str(self.repo_dir / "index-partial")
            self._git("read-tree", "--empty", index=index_partial)
            eligible = [
                f for f in files_to_archive
                if not self._excluded(f, ignore_matcher, workspace_ignore)
                and (self.workspace / f).is_file()
            ]
            for batch in _batched(eligible):
                self._git("add", "-f", "--", *batch, index=index_partial)

        tree = self._git("write-tree", index=index_full if files_to_archive is None
                         else str(self.repo_dir / "index-partial")).stdout.strip()
        files = self._git("ls-tree", "-r", "--name-only", tree).stdout.splitlines()

        metadata = {
            "timestamp": ts,
            "label": label,
            "archived_files": files[:_MAX_META_FILES],
            "archived_count": len(files),
            "workspace": str(self.workspace),
        }
        message = f"{name}\n\n{json.dumps(metadata, ensure_ascii=False)}"
        commit = self._git("commit-tree", tree, input_text=message).stdout.strip()
        # No -p passed to commit-tree: an orphan commit per snapshot is what
        # makes tag deletion + gc actually free that snapshot's unique data.
        self._git("tag", tag, commit)

        pruned = self._prune_old_tags()
        message_out = f"Archived {len(files)} files to {tag} (git snapshot)"
        if pruned:
            message_out += f"; pruned {len(pruned)} old snapshot(s)"

        self._failures = 0
        return ArchiveResult(
            success=True,
            archive_path=self.refs_root / name,
            archived_files=files[:_MAX_META_FILES],
            message=message_out,
        )

    def _excluded(
        self,
        rel_path: str,
        ignore_matcher: IgnoreMatcher | None,
        workspace_ignore: IgnoreMatcher | None = None,
    ) -> bool:
        from supervisor.utils.filesystem.path_filters import should_skip_path

        path = Path(rel_path)
        if should_skip_path(path, extra_dirs=_ARCHIVE_EXTRA_IGNORE_DIRS):
            return True
        if rel_path.endswith(".log"):
            return True
        if ignore_matcher and ignore_matcher.matches(rel_path):
            return True
        if workspace_ignore is None:
            workspace_ignore = IgnoreMatcher(self.workspace)
            workspace_ignore.load_from_workspace(self.workspace)
        return workspace_ignore.matches(rel_path)

    def _drop_newly_excluded_tracked(
        self,
        index: str,
        ignore_matcher: IgnoreMatcher | None,
        workspace_ignore: IgnoreMatcher | None = None,
    ) -> None:
        """Untrack files that were committed before a new exclusion matched.

        ``git add`` keeps already-tracked files even when they newly match
        info/exclude; without this, a newly-added .opencodeignore (or the
        ``*.log`` rule) would keep snapshotting old offenders forever.
        """
        tracked = self._git("ls-files", index=index).stdout.splitlines()
        to_drop = [
            f for f in tracked
            if f and self._excluded(f, ignore_matcher, workspace_ignore)
        ]
        for batch in _batched(to_drop):
            self._git("rm", "-r", "--cached", "--quiet", "--ignore-unmatch",
                      "--", *batch, index=index)

    def _prune_old_tags(self) -> list[str]:
        if self.keep_last <= 0:
            return []
        tags = sorted(
            t for t in self._git("tag", "-l", f"{TAG_PREFIX}run_*").stdout.split()
            if t.startswith(TAG_PREFIX)
        )
        excess = tags[: max(0, len(tags) - self.keep_last)]
        for tag in excess:
            self._git("tag", "-d", tag)
        if excess:
            # Orphan snapshots: with the tag gone nothing references the
            # commit, so prune=now actually reclaims its unique blobs. A gc
            # hiccup (concurrent lock, disk) must not fail the snapshot.
            gc = self._git("gc", "--prune=now", "--quiet", check=False)
            if gc.returncode != 0:
                logger.warning(
                    "archive gc skipped: %s", (gc.stderr or "").strip()[:200],
                )
        return [t[len(TAG_PREFIX):] for t in excess]

    def _legacy_archives(self) -> list[dict]:
        archives: list[dict] = []
        if not self.archive_root.is_dir():
            return archives
        for archive_dir in sorted(self.archive_root.iterdir()):
            if not archive_dir.is_dir() or archive_dir.name.startswith("."):
                continue
            if archive_dir.name in (REPO_DIRNAME, REFS_DIRNAME):
                continue
            metadata_file = archive_dir / "archive_metadata.json"
            metadata = {"name": archive_dir.name, "path": str(archive_dir)}
            if metadata_file.exists():
                try:
                    metadata.update(json.loads(metadata_file.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, OSError):
                    pass
            archives.append(metadata)
        return archives

    @staticmethod
    def _tree_size(root: Path) -> int:
        total = 0
        if not root.exists():
            return 0
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total
