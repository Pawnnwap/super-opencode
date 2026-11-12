"""Checks for the workspace access & protection profile (access_protection.json)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from supervisor.security.access_profile import (
    CONFIG_FILES,
    PROFILE_NAME,
    apply_generation,
    load_profile,
    merge_profile,
    profile_path,
    save_profile,
)


@pytest.fixture()
def layout(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "x.txt").write_text("data", encoding="utf-8")
    return ws, ref


def test_save_roundtrip_and_auto_protects_config_files(layout):
    ws, ref = layout
    path = save_profile(ws, {
        "access_whitelist": [str(ref)],
        "access_policy_enabled": True,
        "access_policy_strict": True,
        "protected_files": ["notes.md", "notes.md"],
    })
    assert path == ws / PROFILE_NAME
    assert profile_path(ws) == path
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["access_policy_strict"] is True
    assert data["access_whitelist"] == [str(ref)]
    assert data["protected_files"].count("notes.md") == 1
    for name in CONFIG_FILES:
        assert name in data["protected_files"]
    assert load_profile(ws)["access_policy_enabled"] is True


def test_merge_profile_overrides_state(layout):
    ws, ref = layout
    save_profile(ws, {"access_whitelist": [str(ref)], "access_policy_enabled": False})
    state = {"access_whitelist": ["C:\\stale"], "access_policy_enabled": True, "other": 1}
    merged = merge_profile(state, ws)
    assert merged["access_whitelist"] == [str(ref)]
    assert merged["access_policy_enabled"] is False
    assert merged["other"] == 1


def test_merge_profile_noop_without_file(layout):
    ws, _ = layout
    state = {"access_whitelist": ["x"]}
    assert merge_profile(state, ws) == state


def test_apply_generation_validates_and_persists(layout):
    ws, ref = layout
    summary = apply_generation(
        ws,
        [str(ref), str(ws / "inside"), "relative/path"],
        "__pycache__/\n*.log\n",
    )
    assert summary["whitelist_added"] == [str(ref.resolve())]
    reasons = {path for path, _reason in summary["whitelist_rejected"]}
    assert str(ws / "inside") in reasons
    assert "relative/path" in reasons
    assert summary["ignore_written"] is True
    assert load_profile(ws)["access_whitelist"] == [str(ref.resolve())]
    ignore = (ws / ".opencodeignore").read_text(encoding="utf-8")
    assert "__pycache__/" in ignore and "*.log" in ignore


def test_apply_generation_keeps_existing_entries(layout):
    ws, ref = layout
    save_profile(ws, {"access_whitelist": [str(ref)]})
    summary = apply_generation(ws, [str(ref)], "")
    assert summary["whitelist_added"] == []
    assert summary["ignore_written"] is False
    assert load_profile(ws)["access_whitelist"] == [str(ref)]


def test_build_config_uses_workspace_profile(tmp_path):
    from services.config.supervisor_config_builder import build_supervisor_config

    ws = tmp_path / "ws"
    ws.mkdir()
    ref = tmp_path / "ref"
    ref.mkdir()
    save_profile(ws, {
        "access_whitelist": [str(ref)],
        "access_policy_enabled": False,
        "protected_files": ["notes.md"],
    })
    state = {
        "max_retries": 1,
        "context_threshold": 80,
        "engine": "opencode",
        "opencode_model": "m",
        "opencode_model_backup": "",
        "opencode_executable": "opencode",
        "supervisor_model": "s",
        "supervisor_model_backup": "",
        "timeout": 10,
        "max_tokens": 1000,
        "access_whitelist": ["C:\\stale"],
        "access_policy_enabled": True,
        "protected_files": [],
    }
    config = build_supervisor_config(state, ws / "protocol.md", ws)
    assert config.access_whitelist == (str(ref),)
    assert config.access_policy_enabled is False
    for name in CONFIG_FILES:
        assert name in config.protected_files
    assert "notes.md" in config.protected_files


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
