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
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from supervisor.utils.path_filters import should_skip_path

if TYPE_CHECKING:
    from supervisor.workspace.ignore_patterns import IgnoreMatcher

_IGNORE_EXTS = {".pyc", ".pyo", ".egg-info", ".DS_Store", ".bak", ".isorted"}
_MAX_FILE_CHARS = 6_000


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


@dataclass
class CodebaseSnapshot:
    root: Path
    files: list[FileSnapshot] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Formatted views (original methods — unchanged)                       #
    # ------------------------------------------------------------------ #

    def tree(self) -> str:
        """ASCII file tree."""
        lines = [f"{self.root.name}/"]
        paths = sorted(f.rel_path for f in self.files)
        for p in paths:
            depth = p.count("/")
            name = p.rsplit("/", 1)[-1] if "/" in p else p
            lines.append("  " * depth + f"└─ {name}")
        return "\n".join(lines)

    def digest_for_prompt(self, max_files: int = 30) -> str:
        """Compact multi-file listing suitable for an LLM system prompt.
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
                f"```python\n{snap.content}\n```\n",
            )
        return "\n".join(parts)

    def file_hashes(self) -> dict[str, str]:
        """Map of rel_path → sha256 for change detection."""
        return {f.rel_path: f.sha256 for f in self.files}

    def changed_files(self, other: CodebaseSnapshot) -> list[str]:
        """Files that differ between two snapshots (added, removed, modified)."""
        a = self.file_hashes()
        b = other.file_hashes()
        changed: list[str] = []
        for path in set(a) | set(b):
            if a.get(path) != b.get(path):
                changed.append(path)
        return sorted(changed)

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
            "### File tree\n```\n" + self.tree() + "\n```\n",
            "### Structural signatures\n",
        ]
        for snap in ranked:
            if snap.skeleton and not snap.skeleton.is_empty():
                parts.append(
                    f"```python\n{snap.skeleton.to_prompt_str(snap.rel_path)}\n```\n",
                )
            else:
                # Non-Python or unparseable: just name it so the LLM knows it exists
                parts.append(f"- {snap.rel_path}\n")

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
            content=text[:_MAX_FILE_CHARS] if truncated else text,
            truncated=truncated,
            sha256=sha,
            skeleton=skeleton,
        ))
    except OSError:
        pass  # skip unreadable files
