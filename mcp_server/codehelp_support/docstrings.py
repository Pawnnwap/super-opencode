from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

_MAX_FILES = 500
_MAX_RESULTS = 20


def _iter_py_files(root: Path, max_files: int = _MAX_FILES):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if not dirname.startswith(".")
            and dirname not in ("__pycache__", ".venv", "venv", "node_modules", ".git")
        ]
        for filename in filenames:
            if filename.endswith(".py"):
                yield Path(dirpath) / filename
                count += 1
                if count >= max_files:
                    return


def _get_docstring(node: ast.AST) -> str | None:
    if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    return ast.get_docstring(node)


def _parts_match(target_parts: list[str], node_parts: list[str]) -> bool:
    if len(target_parts) > len(node_parts):
        return False
    return node_parts[-len(target_parts):] == target_parts


def _match_target(target: str, filepath: Path, root: Path) -> list[dict[str, Any]]:
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    parts = [part.strip() for part in target.split(".") if part.strip()]
    results: list[dict[str, Any]] = []
    rel_path = str(filepath.relative_to(root))

    def file_matches_package(target_parts: list[str]) -> bool:
        if not target_parts:
            return False
        stem = filepath.stem
        parent_name = filepath.parent.name
        if len(target_parts) == 1:
            return stem == target_parts[0] or (stem == "__init__" and parent_name == target_parts[0])
        return False

    if len(parts) <= 1 and file_matches_package(parts):
        doc = ast.get_docstring(tree)
        if doc:
            results.append({
                "kind": "module",
                "name": target,
                "file": rel_path,
                "line": 1,
                "docstring": doc,
            })

    def walk_nodes(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified_name = f"{prefix}.{child.name}" if prefix else child.name
                doc = ast.get_docstring(child)
                if doc and _parts_match(parts, qualified_name.split(".")):
                    results.append({
                        "kind": "class" if isinstance(child, ast.ClassDef) else "function",
                        "name": qualified_name,
                        "file": rel_path,
                        "line": child.lineno,
                        "docstring": doc,
                    })
                walk_nodes(child, qualified_name)

    walk_nodes(tree)
    return results


def _codehelp_search_docstrings(target: str, codebase_path: str = ".") -> dict[str, Any]:
    root = Path(codebase_path).resolve()
    if not root.is_dir():
        return {"error": f"codebase_path is not a directory: {root}"}

    target = target.strip()
    if not target:
        return {"error": "target must not be empty"}

    all_results: list[dict[str, Any]] = []
    files_scanned = 0
    for py_file in _iter_py_files(root):
        all_results.extend(_match_target(target, py_file, root))
        files_scanned += 1
        if len(all_results) >= _MAX_RESULTS:
            break

    if not all_results:
        return {
            "target": target,
            "found": 0,
            "files_scanned": files_scanned,
            "message": "No docstrings found matching the target. The name may not exist in this codebase.",
        }

    return {
        "target": target,
        "found": len(all_results),
        "files_scanned": files_scanned,
        "results": all_results[:_MAX_RESULTS],
    }
