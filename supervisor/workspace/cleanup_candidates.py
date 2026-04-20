from __future__ import annotations

import re
from pathlib import Path

_VERSION_PATTERNS = [
    re.compile(r"\.bak$"),
    re.compile(r"\.backup$"),
    re.compile(r"\.old$"),
    re.compile(r"\.orig$"),
    re.compile(r"\.tmp$"),
    re.compile(r"~\d+$"),
    re.compile(r"\.v\d+$"),
    re.compile(r"_backup_\d+$"),
    re.compile(r"_old_\d+$"),
    re.compile(r"\.\d+$"),
]

_SOURCE_EXTS = {
    ".py",
    ".pyc",
    ".pyo",
    ".pyd",
    ".md",
    ".txt",
    ".rst",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".css",
    ".scss",
    ".html",
    ".xml",
    ".sh",
    ".bat",
    ".ps1",
}

_IMPORT_PATTERNS = [
    (re.compile(r"^(?:from|import)\s+([\w.]+)", re.MULTILINE), "py"),
    (re.compile(r'require\s*\(\s*["\']([^"\']+)["\']\s*\)', re.MULTILINE), "js"),
    (re.compile(r'import\s+.*?from\s+["\']([^"\']+)["\']', re.MULTILINE), "js"),
    (re.compile(r'#include\s*["<]([^">]+)[">]', re.MULTILINE), "c"),
]


def build_cleanup_inquiry(workspace: Path, candidates: list[str]) -> str:
    if not candidates:
        return ""

    workspace_rel = workspace.relative_to(workspace) if workspace.is_absolute() else workspace
    inquiry = (
        f"Workspace: {workspace_rel}\n\n"
        "Following files may be outdated or unused:\n"
    )
    for index, candidate in enumerate(candidates, 1):
        inquiry += f"  {index}. {candidate}\n"

    inquiry += (
        "\nPlease analyze these files and respond with a JSON list of file paths "
        "that should be archived. These files will be moved to .archive/ "
        "instead of being deleted, preserving historical versions.\n"
        "Consider:\n"
        "- Files clearly temporary, backup, or cache files\n"
        "- Files not referenced by other code\n"
        "- Files that appear to be duplicate or superseded versions\n"
        "- Any __pycache__ directories\n\n"
        "IMPORTANT: Never select protected paths (.opencode/, .checkpoints/, .archive/) "
        "for archiving.\n\n"
        "Respond ONLY with a JSON array of file paths to archive, nothing else. "
        'Example: ["file1.bak", "file2.tmp"]'
    )
    return inquiry


def identify_cleanup_candidates(workspace: Path) -> list[str]:
    candidates: list[str] = []
    candidates.extend(_identify_versioned_backups(workspace))
    candidates.extend(_identify_orphaned_files(workspace))
    return candidates


def _should_ignore(workspace: Path, path: Path) -> bool:
    if not path.is_file():
        if not (path.is_dir() and path.name == "__pycache__"):
            return True
    rel = path.relative_to(workspace)
    if ".checkpoints" in rel.parts:
        return True
    if path == workspace / ".checkpoints":
        return True
    ignore_dirs = {".git", ".venv", "venv", "node_modules", ".mypy_cache", ".opencode"}
    if any(part in ignore_dirs for part in rel.parts):
        return True
    return False


def _is_versioned_backup(name: str) -> bool:
    return any(pattern.search(name) for pattern in _VERSION_PATTERNS)


def _get_base_name(path: Path) -> str:
    base = path.name
    changed = True
    while changed:
        changed = False
        for pattern in _VERSION_PATTERNS:
            new_base = pattern.sub("", base)
            if new_base != base:
                base = new_base
                changed = True
                break
    return base


def _identify_versioned_backups(workspace: Path) -> list[str]:
    candidates: list[str] = []
    backup_groups: dict[str, list[Path]] = {}
    all_files: dict[str, Path] = {}

    for path in workspace.rglob("*"):
        if _should_ignore(workspace, path):
            continue
        all_files[path.name] = path
        if _is_versioned_backup(path.name):
            base = _get_base_name(path)
            backup_groups.setdefault(base, []).append(path)

    for base_name, backups in backup_groups.items():
        if base_name in all_files:
            backups.append(all_files[base_name])
        backups_sorted = sorted(backups, key=lambda p: len(p.name))
        for backup in backups_sorted[1:]:
            candidates.append(str(backup.relative_to(workspace)))

    return candidates


def _identify_orphaned_files(workspace: Path) -> list[str]:
    candidates: list[str] = []
    referenced_paths: set[str] = set()

    for path in workspace.rglob("*"):
        if _should_ignore(workspace, path):
            continue
        if path.suffix not in _SOURCE_EXTS and not path.name.endswith(".h"):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            for pattern, ptype in _IMPORT_PATTERNS:
                for match in pattern.finditer(content):
                    ref = match.group(1)
                    if ptype == "py":
                        ref = ref.replace(".", "/")
                        if not ref.endswith(".py"):
                            ref += ".py"
                    referenced_paths.add(ref)
        except Exception:
            pass

    for path in workspace.rglob("*"):
        if _should_ignore(workspace, path):
            continue
        rel_str = str(path.relative_to(workspace))

        if path.is_dir() and path.name == "__pycache__":
            candidates.append(rel_str)
            continue

        if path.suffix in {".pyc", ".pyo", ".pyc.tmp"} or path.name.endswith(".pyc"):
            candidates.append(rel_str)

    return candidates
