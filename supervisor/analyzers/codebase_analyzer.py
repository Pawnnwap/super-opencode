"""supervisor/codebase_analyzer.py

Reads the supervisor codebase itself and produces:
  - A structured file tree string
  - Per-file content (truncated for very large files)
  - A compact "digest" suitable for injection into an LLM system prompt
  - A code-skimmed digest (signatures + docstrings only, ~10x fewer tokens)

Used by the self-evolution subsystem so the supervisor understands
exactly what it is judging before it asks opencode to modify code.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from supervisor.utils.filesystem.path_filters import should_skip_path
from supervisor.utils.text_utils import head_tail_excerpt

if TYPE_CHECKING:
    from supervisor.workspace.ignore_patterns import IgnoreMatcher

_IGNORE_EXTS = {".pyc", ".pyo", ".egg-info", ".DS_Store", ".bak", ".isorted"}
_MAX_FILE_CHARS = 6_000
# Line diffing is bounded: beyond this many lines the SequenceMatcher cost
# dwarfs the value of an exact count, so the delta is reported as approximate.
_MAX_DIFF_LINES = 4_000

# Digest guards: workspaces accumulate generated artifacts over a long run
# (literature/, logs/, ab/, output/, ...). An unbounded tree listing once
# pushed a 5,720-file listing — ~80k tokens — into the judge's system prompt
# on every turn, crowding the agent's evidence out of the request entirely.
_MAX_TREE_ENTRIES = 200
_MAX_DIGEST_CHARS = 48_000  # ~12k tokens ceiling for a whole digest block


# ------------------------------------------------------------------ #
# Code skimming                                                        #
# ------------------------------------------------------------------ #

@dataclass
class CodeSkeleton:
    """Structural 'headline' view of a Python file — signatures only, no bodies."""

    classes: list[dict] = field(default_factory=list)
    functions: list[dict] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    module_docstring: str | None = None

    def to_prompt_str(self, path: str = "") -> str:
        """Render the skeleton as a compact string for LLM consumption."""
        lines = [f"# FILE: {path}"] if path else []
        if self.module_docstring:
            lines.append(f'"""{self.module_docstring.splitlines()[0]}"""')
        if self.imports:
            lines.extend(self.imports)
        for cls in self.classes:
            lines.append(cls["signature"])
            if cls.get("docstring"):
                lines.append(f'    """{cls["docstring"].splitlines()[0]}"""')
            for method in cls.get("methods", []):
                lines.append(f"    {method['signature']}")
                if method.get("docstring"):
                    lines.append(f'        """{method["docstring"].splitlines()[0]}"""')
        for fn in self.functions:
            lines.append(fn["signature"])
            if fn.get("docstring"):
                lines.append(f'    """{fn["docstring"].splitlines()[0]}"""')
        return "\n".join(lines)

    def is_empty(self) -> bool:
        return not (self.classes or self.functions)


def _annotation_str(node: ast.expr | None) -> str:
    return ast.unparse(node) if node else ""


def _build_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Reconstruct a function signature with type hints; body replaced with '...'"""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    all_args = node.args
    defaults: list = [None] * (len(all_args.args) - len(all_args.defaults)) + list(all_args.defaults)

    args = []
    for arg, default in zip(all_args.args, defaults):
        ann = f": {_annotation_str(arg.annotation)}" if arg.annotation else ""
        dflt = f" = {ast.unparse(default)}" if default else ""
        args.append(f"{arg.arg}{ann}{dflt}")
    if all_args.vararg:
        ann = f": {_annotation_str(all_args.vararg.annotation)}" if all_args.vararg.annotation else ""
        args.append(f"*{all_args.vararg.arg}{ann}")
    if all_args.kwarg:
        ann = f": {_annotation_str(all_args.kwarg.annotation)}" if all_args.kwarg.annotation else ""
        args.append(f"**{all_args.kwarg.arg}{ann}")

    ret = f" -> {_annotation_str(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({', '.join(args)}){ret}: ..."


def _skim_python_source(source: str) -> CodeSkeleton | None:
    """Parse Python source and extract structural skeleton only."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    skeleton = CodeSkeleton()
    skeleton.module_docstring = ast.get_docstring(tree)

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            skeleton.imports.append(ast.unparse(node))
        elif isinstance(node, ast.ClassDef):
            bases = [ast.unparse(b) for b in node.bases]
            base_str = f"({', '.join(bases)})" if bases else ""
            class_info = {
                "name": node.name,
                "signature": f"class {node.name}{base_str}:",
                "docstring": ast.get_docstring(node),
                "methods": [],
            }
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    class_info["methods"].append({
                        "name": item.name,
                        "signature": _build_function_signature(item),
                        "docstring": ast.get_docstring(item),
                    })
            skeleton.classes.append(class_info)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            skeleton.functions.append({
                "name": node.name,
                "signature": _build_function_signature(node),
                "docstring": ast.get_docstring(node),
            })

    return skeleton


# ------------------------------------------------------------------ #
# Core data classes                                                    #
# ------------------------------------------------------------------ #

@dataclass
class FileSnapshot:
    rel_path: str        # relative to repo root
    content: str         # (possibly truncated)
    truncated: bool
    sha256: str          # full-content hash
    skeleton: CodeSkeleton | None = None  # None for non-Python or unparseable files
    mtime: float = 0.0   # last write time, for recency ordering


@dataclass
class FileDelta:
    """What changed in one file between two snapshots."""

    rel_path: str
    status: str          # "added" | "modified" | "removed"
    added: int = 0
    removed: int = 0
    approx: bool = False  # diff exceeded the line budget

    def render(self) -> str:
        """Compact one-line marker for a file-selection listing."""
        sign = {"added": "+", "modified": "~", "removed": "-"}.get(self.status, "?")
        if self.status == "added":
            return f"{sign}new"
        if self.status == "removed":
            return f"{sign}deleted"
        body = f"+{self.added}/-{self.removed}"
        return f"{sign}{body}{'~' if self.approx else ''}"


@dataclass
class CodebaseSnapshot:
    root: Path
    files: list[FileSnapshot] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Formatted views (original methods — unchanged)                       #
    # ------------------------------------------------------------------ #

    def tree(self, max_entries: int | None = None) -> str:
        """ASCII file tree.

        ``max_entries`` bounds the file lines (root line and a trailing
        overflow notice are extra). None renders every file — only for
        callers that display or diff the tree, never for prompt injection.
        """
        lines = [f"{self.root.name}/"]
        paths = sorted(f.rel_path for f in self.files)
        overflow = 0
        if max_entries is not None and len(paths) > max_entries:
            overflow = len(paths) - max_entries
            paths = paths[:max_entries]
        for p in paths:
            depth = p.count("/")
            name = p.rsplit("/", 1)[-1] if "/" in p else p
            lines.append("  " * depth + f"└─ {name}")
        if overflow:
            lines.append(f"  … (+{overflow} more files not listed)")
        return "\n".join(lines)

    def _tree_for_digest(self, budget_chars: int) -> str:
        """Largest bounded tree that fits ``budget_chars``."""
        rendered = self.tree(max_entries=_MAX_TREE_ENTRIES)
        if len(rendered) <= budget_chars or len(self.files) <= 1:
            return rendered
        # The capped tree alone is over budget: halve the entry cap until it
        # fits — a partial map beats an 80k-token listing.
        cap = _MAX_TREE_ENTRIES
        while cap > 1:
            cap //= 2
            rendered = self.tree(max_entries=cap)
            if len(rendered) <= budget_chars:
                return rendered
        return self.tree(max_entries=1)

    def digest_for_prompt(self, max_files: int = 30) -> str:
        """Compact multi-file listing suitable for an LLM system prompt.
        Keeps the most important files (Python source first, then others).
        The whole block is capped at ``_MAX_DIGEST_CHARS`` so it can never
        crowd the agent's evidence out of the judge request.
        """
        ranked = sorted(
            self.files,
            key=lambda f: (0 if f.rel_path.endswith(".py") else 1, f.rel_path),
        )[:max_files]

        header = f"## Codebase snapshot  ({len(self.files)} files total)\n"
        tree_block = "### File tree\n```\n" + self._tree_for_digest(
            _MAX_DIGEST_CHARS // 2,
        ) + "\n```\n"
        parts: list[str] = [header, tree_block]
        used = len(header) + len(tree_block)
        for snap in ranked:
            suffix = "  [TRUNCATED]" if snap.truncated else ""
            section = (
                f"### {snap.rel_path}{suffix}\n"
                f"```python\n{snap.content}\n```\n"
            )
            if used + len(section) > _MAX_DIGEST_CHARS:
                break
            parts.append(section)
            used += len(section)
        return "\n".join(parts)

    def file_hashes(self) -> dict[str, str]:
        """Map of rel_path → sha256 for change detection."""
        return {f.rel_path: f.sha256 for f in self.files}

    def signature(self) -> str:
        """Stable fingerprint of workspace content (equal = unchanged)."""
        hashes = self.file_hashes()
        payload = "\n".join(f"{path}:{hashes[path]}" for path in sorted(hashes))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def changed_files(self, other: CodebaseSnapshot) -> list[str]:
        """Files that differ between two snapshots (added, removed, modified)."""
        a = self.file_hashes()
        b = other.file_hashes()
        changed: list[str] = []
        for path in set(a) | set(b):
            if a.get(path) != b.get(path):
                changed.append(path)
        return sorted(changed)

    def deltas(self, newer: CodebaseSnapshot) -> dict[str, FileDelta]:
        """Per-file line delta against an earlier snapshot, keyed by path.

        This is what lets the judge's file-selection step see *what moved*
        since the previous turn, instead of a flat alphabetical list where a
        freshly rewritten deliverable is indistinguishable from a stale
        artifact. Removed files are reported for the record; the caller
        decides whether they belong in a candidate listing.
        """
        before = {f.rel_path: f for f in self.files}
        after = {f.rel_path: f for f in newer.files}
        out: dict[str, FileDelta] = {}
        for path in sorted(set(before) | set(after)):
            old = before.get(path)
            new = after.get(path)
            if old is None and new is not None:
                out[path] = FileDelta(
                    path, "added", added=_line_count(new.content),
                )
                continue
            if new is None and old is not None:
                out[path] = FileDelta(
                    path, "removed", removed=_line_count(old.content),
                )
                continue
            if old is None or new is None or old.sha256 == new.sha256:
                continue
            added, removed, approx = _line_delta(old.content, new.content)
            out[path] = FileDelta(path, "modified", added, removed, approx)
        return out

    def recency_order(self) -> list[FileSnapshot]:
        """Files newest-first — the order an investigator wants by default."""
        return sorted(
            self.files,
            key=lambda f: (-(f.mtime or 0.0), f.rel_path),
        )

    # ------------------------------------------------------------------ #
    # Skimmed views (new methods)                                          #
    # ------------------------------------------------------------------ #

    def skimmed_digest_for_prompt(self, max_files: int = 30) -> str:
        """Token-efficient structural digest: signatures + docstrings only, no bodies.
        Use this as a first-pass map so an LLM can identify which files to load
        in full via digest_for_prompt() or direct file reads.

        Typically 85-95% fewer tokens than digest_for_prompt() for the same files.
        Python files with parseable skeletons use skeleton view; others fall back
        to a path-only listing so the tree remains complete.
        """
        ranked = sorted(
            self.files,
            key=lambda f: (0 if f.rel_path.endswith(".py") else 1, f.rel_path),
        )[:max_files]

        parts: list[str] = [
            f"## Codebase skeleton  ({len(self.files)} files total — signatures only)\n",
            "### File tree\n```\n" + self._tree_for_digest(
                _MAX_DIGEST_CHARS // 2,
            ) + "\n```\n",
            "### Structural signatures\n",
        ]
        used = sum(len(p) for p in parts)
        for snap in ranked:
            if snap.skeleton and not snap.skeleton.is_empty():
                section = (
                    f"```python\n{snap.skeleton.to_prompt_str(snap.rel_path)}\n```\n"
                )
            else:
                # Non-Python or unparseable: just name it so the LLM knows it exists
                section = f"- {snap.rel_path}\n"
            if used + len(section) > _MAX_DIGEST_CHARS:
                break
            parts.append(section)
            used += len(section)

        return "\n".join(parts)

    def skimmed_file(self, rel_path: str) -> str | None:
        """Return the skeleton prompt string for a single file by relative path.
        Returns None if the file isn't in the snapshot or has no skeleton.
        Useful when an LLM has identified a specific file and wants a cheap
        structural look before deciding whether to request the full content.
        """
        for snap in self.files:
            if snap.rel_path == rel_path:
                if snap.skeleton and not snap.skeleton.is_empty():
                    return snap.skeleton.to_prompt_str(snap.rel_path)
                return None
        return None

    def skim_token_savings(self) -> dict:
        """Estimate token savings across the whole snapshot.
        Useful for logging / telemetry to validate the approach is working.
        """
        full_chars = sum(len(f.content) for f in self.files)
        skim_chars = sum(
            len(f.skeleton.to_prompt_str(f.rel_path))
            if f.skeleton and not f.skeleton.is_empty()
            else len(f.rel_path)   # just the path for non-skimmable files
            for f in self.files
        )
        ratio = skim_chars / full_chars if full_chars else 1.0
        return {
            "full_tokens_approx": full_chars // 4,
            "skim_tokens_approx": skim_chars // 4,
            "reduction_pct": round((1 - ratio) * 100, 1),
            "skimmable_files": sum(1 for f in self.files if f.skeleton and not f.skeleton.is_empty()),
            "total_files": len(self.files),
        }


# ------------------------------------------------------------------ #
# Public factory                                                       #
# ------------------------------------------------------------------ #

def _line_count(text: str) -> int:
    return text.count("\n") + (0 if not text or text.endswith("\n") else 1)


def _line_delta(old: str, new: str) -> tuple[int, int, bool]:
    """(added, removed, approximate) line counts between two texts.

    Bounded: a 500-line deliverable versus its predecessor is cheap, while an
    unbounded matcher over a generated 200k-line JSON would stall the turn.
    Over the budget the size difference is reported as approximate.
    """
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    if max(len(old_lines), len(new_lines)) > _MAX_DIFF_LINES:
        growth = len(new_lines) - len(old_lines)
        return (max(0, growth), max(0, -growth), True)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            removed += i2 - i1
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "insert":
            added += j2 - j1
    return added, removed, False


def snapshot_codebase(root: Path, ignore_matcher: IgnoreMatcher | None = None) -> CodebaseSnapshot:
    """Walk *root* and build a CodebaseSnapshot."""
    snap = CodebaseSnapshot(root=root)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if should_skip_path(path):
            continue
        if path.suffix in _IGNORE_EXTS:
            continue
        if ignore_matcher and ignore_matcher.matches(path):
            continue
        _add_file(snap, path, root)
    return snap


def _add_file(snap: CodebaseSnapshot, path: Path, root: Path) -> None:
    try:
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()[:12]
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = f"<binary file, {len(raw)} bytes>"
        truncated = len(text) > _MAX_FILE_CHARS

        # Build skeleton for Python files before truncation so the full
        # AST is available; bodies are discarded anyway so truncation
        # doesn't affect skeleton quality.
        skeleton: CodeSkeleton | None = None
        if path.suffix == ".py":
            skeleton = _skim_python_source(text)

        snap.files.append(FileSnapshot(
            rel_path=str(path.relative_to(root)),
            # Keep both ends of oversized files: imports/metadata at the head,
            # exports/main guard at the tail — a head-only cut drops exactly
            # the part that identifies what the module provides.
            content=(
                head_tail_excerpt(text, _MAX_FILE_CHARS, end_ratio=0.4)
                if truncated else text
            ),
            truncated=truncated,
            sha256=sha,
            skeleton=skeleton,
            mtime=mtime,
        ))
    except OSError:
        pass  # skip unreadable files
