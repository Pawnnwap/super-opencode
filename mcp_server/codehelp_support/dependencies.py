from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from .packages import _codehelp_search_package_version

_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^]]+\])?")


def _dependency_name(requirement: str) -> str | None:
    match = _REQUIREMENT_NAME.match(requirement.split("#", 1)[0])
    return match.group(1) if match else None


def _risk_for_requirement(requirement: str, locked_version: str) -> str:
    if not requirement:
        return "unconstrained"
    if "==" in requirement:
        return "pinned" if not locked_version or locked_version in requirement else "lock drift"
    if any(operator in requirement for operator in ("~=", "^", "~", "<", ">")):
        return "bounded range"
    return "unconstrained"


def _add_dependency(
    found: dict[tuple[str, str], dict[str, Any]],
    *,
    name: str,
    ecosystem: str,
    declared: str,
    source_file: str,
    group: str,
) -> None:
    key = (ecosystem, name.lower())
    item = found.setdefault(
        key,
        {
            "name": name,
            "ecosystem": ecosystem,
            "declared_version": "",
            "locked_version": "",
            "sources": [],
            "groups": [],
        },
    )
    if declared and not item["declared_version"]:
        item["declared_version"] = declared
    if source_file not in item["sources"]:
        item["sources"].append(source_file)
    if group not in item["groups"]:
        item["groups"].append(group)


def _read_pyproject(root: Path, found: dict[tuple[str, str], dict[str, Any]]) -> None:
    path = root / "pyproject.toml"
    if not path.is_file():
        return
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return
    project = payload.get("project", {})
    for requirement in project.get("dependencies", []):
        if isinstance(requirement, str) and (name := _dependency_name(requirement)):
            _add_dependency(found, name=name, ecosystem="pypi", declared=requirement, source_file="pyproject.toml", group="runtime")
    for group, requirements in project.get("optional-dependencies", {}).items():
        for requirement in requirements:
            if isinstance(requirement, str) and (name := _dependency_name(requirement)):
                _add_dependency(found, name=name, ecosystem="pypi", declared=requirement, source_file="pyproject.toml", group=f"optional:{group}")


def _read_requirements(root: Path, found: dict[tuple[str, str], dict[str, Any]]) -> None:
    for path in sorted(root.glob("requirements*.txt")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for requirement in lines:
            requirement = requirement.strip()
            if not requirement or requirement.startswith(("#", "-")):
                continue
            name = _dependency_name(requirement)
            if name:
                _add_dependency(found, name=name, ecosystem="pypi", declared=requirement, source_file=path.name, group="requirements")


def _read_package_json(root: Path, found: dict[tuple[str, str], dict[str, Any]]) -> None:
    path = root / "package.json"
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for group in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        for name, declared in payload.get(group, {}).items():
            if isinstance(name, str) and isinstance(declared, str):
                _add_dependency(found, name=name, ecosystem="npm", declared=declared, source_file="package.json", group=group)


def _read_package_lock(root: Path, found: dict[tuple[str, str], dict[str, Any]]) -> None:
    path = root / "package-lock.json"
    if not path.is_file():
        return
    try:
        packages = json.loads(path.read_text(encoding="utf-8")).get("packages", {})
    except (OSError, json.JSONDecodeError):
        return
    for location, item in packages.items():
        if not location.startswith("node_modules/") or not isinstance(item, dict):
            continue
        name = location.removeprefix("node_modules/")
        dependency = found.get(("npm", name.lower()))
        if dependency and isinstance(item.get("version"), str):
            dependency["locked_version"] = item["version"]
            if path.name not in dependency["sources"]:
                dependency["sources"].append(path.name)


def _codehelp_analyze_dependencies(
    codebase_path: str = ".",
    package_name: str = "",
    include_latest: bool = False,
    max_latest_queries: int = 20,
) -> dict[str, Any]:
    root = Path(codebase_path).resolve()
    if not root.is_dir():
        return {"status": "error", "error_type": "invalid_path", "error": f"codebase_path is not a directory: {root}"}

    found: dict[tuple[str, str], dict[str, Any]] = {}
    _read_pyproject(root, found)
    _read_requirements(root, found)
    _read_package_json(root, found)
    _read_package_lock(root, found)

    dependencies = sorted(found.values(), key=lambda item: (item["ecosystem"], item["name"].lower()))
    if package_name.strip():
        target = package_name.strip().lower()
        dependencies = [item for item in dependencies if item["name"].lower() == target]
    latest_checked = 0
    max_latest_queries = max(1, min(max_latest_queries, 20))
    for item in dependencies:
        item["compatibility_risk"] = _risk_for_requirement(item["declared_version"], item["locked_version"])
        if include_latest and latest_checked < max_latest_queries:
            item["latest"] = _codehelp_search_package_version(item["name"], item["ecosystem"])
            latest_checked += 1
        elif include_latest:
            item["latest"] = {
                "status": "skipped",
                "reason": f"Latest-version lookups capped at {max_latest_queries} dependencies.",
            }

    sources = sorted({source for item in dependencies for source in item["sources"]})

    return {
        "status": "ok",
        "codebase_path": str(root),
        "found": len(dependencies),
        "dependencies": dependencies,
        "provenance": [{"source": source} for source in sources],
    }
