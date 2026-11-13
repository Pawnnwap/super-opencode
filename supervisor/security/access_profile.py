"""Workspace-local access & protection profile.

The wizard persists access/protection settings inside the workspace so they
travel with it (replicas, relaunches, archives) instead of living only in the
operator's settings file. :func:`merge_profile` makes the workspace
authoritative at launch time.

Every save self-protects the local configuration files: they are unioned into
``protected_files`` so the agent is told never to modify them.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from supervisor.security.access_policy import validate_whitelist_entry

PROFILE_NAME = "access_protection.json"
IGNORE_FILE_NAME = ".opencodeignore"
FACTS_FILE_NAME = "feasibility_facts.json"
PROTOCOL_FILE_NAME = "protocol.jsonl"
LEGACY_PROTOCOL_FILE_NAME = "protocol.md"

# Local files super-opencode writes for a run; protected from agent edits.
CONFIG_FILES: tuple[str, ...] = (
    PROFILE_NAME,
    IGNORE_FILE_NAME,
    FACTS_FILE_NAME,
    PROTOCOL_FILE_NAME,
    LEGACY_PROTOCOL_FILE_NAME,
)

PROFILE_KEYS: tuple[str, ...] = (
    "access_whitelist",
    "access_policy_enabled",
    "access_policy_strict",
    "protected_files",
)

# Wizard-owned evidence, not operator settings: never seeded from app state,
# but preserved across every profile save.
PROFILE_FLAGS: tuple[str, ...] = (
    # The Access & Protection step is complete for THIS workspace - either the
    # auto-generator finished cleanly or the operator configured it by hand.
    # The wizard's "Ready for Live Run!" button is gated on it.
    "access_configured",
)


def profile_path(workspace: Path) -> Path:
    return Path(workspace) / PROFILE_NAME


def load_profile(workspace: Path) -> dict[str, Any]:
    """Known profile keys from the workspace; {} when missing or unreadable."""
    try:
        data = json.loads(profile_path(workspace).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: data[key]
        for key in (*PROFILE_KEYS, *PROFILE_FLAGS)
        if key in data
    }


def _clean(values: Mapping[str, Any]) -> dict[str, Any]:
    whitelist = [
        str(item).strip()
        for item in (values.get("access_whitelist") or [])
        if str(item).strip()
    ]
    protected: list[str] = []
    seen: set[str] = set()
    for item in values.get("protected_files") or []:
        text = str(item).strip().replace("\\", "/")
        if text and text.lower() not in seen:
            seen.add(text.lower())
            protected.append(text)
    for name in CONFIG_FILES:
        if name.lower() not in seen:
            protected.append(name)
    return {
        "access_whitelist": whitelist,
        "access_policy_enabled": bool(values.get("access_policy_enabled", True)),
        "access_policy_strict": bool(values.get("access_policy_strict", False)),
        "protected_files": protected,
        "access_configured": bool(values.get("access_configured", False)),
    }


def save_profile(workspace: Path, values: Mapping[str, Any]) -> Path:
    """Write the profile; config files are always protected.

    Wizard-owned flags (:data:`PROFILE_FLAGS`) are carried over from the file
    on disk when the caller does not supply them: an operator-settings save
    must not erase the evidence that the auto-generator ran.
    """
    path = profile_path(workspace)
    merged = dict(values)
    missing = [flag for flag in PROFILE_FLAGS if flag not in merged]
    if missing:
        existing = load_profile(workspace)
        for flag in missing:
            merged[flag] = existing.get(flag, False)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(_clean(merged), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def mark_configured(workspace: Path, configured: bool = True) -> None:
    """Record that the Access & Protection step is complete for this workspace."""
    values = load_profile(workspace)
    values["access_configured"] = bool(configured)
    save_profile(workspace, values)


def merge_profile(state: Mapping[str, Any], workspace: Path | str) -> dict[str, Any]:
    """Session state with the workspace profile overlaid (workspace wins)."""
    merged = dict(state)
    merged.update(load_profile(Path(workspace)))
    return merged


def apply_generation(
    workspace: Path,
    whitelist_paths: Iterable[str],
    ignore_patterns: str,
) -> dict[str, Any]:
    """Merge one LLM suggestion batch into the profile and write the ignore file.

    Whitelist candidates are re-validated (absolute, existing, outside cwd);
    the return value is a UI-agnostic summary.
    """
    from supervisor.workspace.ignore_patterns import write_ignore_file

    workspace = Path(workspace)
    current = load_profile(workspace)
    accepted: list[str] = []
    rejected: list[tuple[str, str]] = []
    seen = {str(item).lower() for item in current.get("access_whitelist", [])}
    for raw in whitelist_paths:
        resolved, error = validate_whitelist_entry(raw, workspace)
        if resolved is None:
            rejected.append((str(raw), error))
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        accepted.append(str(resolved))
    merged = dict(current)
    merged["access_whitelist"] = [*current.get("access_whitelist", []), *accepted]
    save_profile(workspace, merged)
    written = bool(str(ignore_patterns).strip())
    if written:
        write_ignore_file(workspace, str(ignore_patterns))
    return {
        "whitelist_added": accepted,
        "whitelist_rejected": rejected,
        "ignore_written": written,
    }


__all__ = [
    "CONFIG_FILES",
    "FACTS_FILE_NAME",
    "IGNORE_FILE_NAME",
    "PROFILE_FLAGS",
    "PROFILE_KEYS",
    "PROFILE_NAME",
    "PROTOCOL_FILE_NAME",
    "apply_generation",
    "load_profile",
    "mark_configured",
    "merge_profile",
    "profile_path",
    "save_profile",
]
