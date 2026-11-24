from __future__ import annotations

import json
import os
from pathlib import Path

from services.config.opencode_config import atomic_write_json, get_opencode_config_file


def _managed_hashline(project_root: Path) -> dict:
    return {
        "type": "local",
        "command": ["python", str(project_root / "mcp_server" / "hashline.py")],
        "enabled": True,
        "environment": {},
    }


def test_atomic_write_json_uses_restrictive_unique_temp_files(tmp_path):
    path = tmp_path / "opencode.json"
    atomic_write_json(path, {"provider": {"key": "secret"}})

    assert json.loads(path.read_text(encoding="utf-8"))["provider"]["key"] == "secret"
    assert not list(tmp_path.glob("*.tmp"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_migrates_managed_hashline_to_native_edit_tools(tmp_path):
    config_dir = tmp_path / "config"
    project_root = tmp_path / "project"
    config_dir.mkdir()
    project_root.mkdir()
    config_path = config_dir / "opencode.json"
    config_path.write_text(
        json.dumps(
            {
                "mcp": {
                    "hashline": _managed_hashline(project_root),
                    "other": {"type": "remote", "url": "https://example.test/mcp"},
                },
                "permission": {"read": "deny", "edit": "deny", "bash": "ask"},
            },
        ),
        encoding="utf-8",
    )

    get_opencode_config_file(config_dir, project_root)

    result = json.loads(config_path.read_text(encoding="utf-8"))
    assert "hashline" not in result["mcp"]
    assert result["mcp"]["other"]["url"] == "https://example.test/mcp"
    assert "codehelp" in result["mcp"]
    assert result["permission"] == {"read": "allow", "edit": "allow", "bash": "ask"}


def test_preserves_user_hashline_and_native_permissions(tmp_path):
    config_dir = tmp_path / "config"
    project_root = tmp_path / "project"
    config_dir.mkdir()
    project_root.mkdir()
    config_path = config_dir / "opencode.json"
    config_path.write_text(
        json.dumps(
            {
                "mcp": {"hashline": {"type": "remote", "url": "https://user.test/mcp"}},
                "permission": {"read": "ask", "edit": "ask"},
            },
        ),
        encoding="utf-8",
    )

    get_opencode_config_file(config_dir, project_root)

    result = json.loads(config_path.read_text(encoding="utf-8"))
    assert result["mcp"]["hashline"]["url"] == "https://user.test/mcp"
    assert result["permission"] == {"read": "ask", "edit": "ask"}


def test_migrates_trailing_comma_config_without_losing_plugins(tmp_path):
    config_dir = tmp_path / "config"
    project_root = tmp_path / "project"
    config_dir.mkdir()
    project_root.mkdir()
    config_path = config_dir / "opencode.json"
    config_path.write_text(
        """{{
  "mcp": {{"hashline": {{"type": "local", "command": ["python", "{cmd}"]}}}},
  "plugin": ["example-plugin"],
}}
""".format(cmd=str(project_root / "mcp_server" / "hashline.py").replace("\\", "\\\\")),
        encoding="utf-8",
    )

    get_opencode_config_file(config_dir, project_root)

    result = json.loads(config_path.read_text(encoding="utf-8"))
    assert result["plugin"] == ["example-plugin"]
    assert "hashline" not in result["mcp"]


def test_migrates_jsonc_without_corrupting_urls_or_comment_like_strings(tmp_path):
    config_dir = tmp_path / "config"
    project_root = tmp_path / "project"
    config_dir.mkdir()
    project_root.mkdir()
    config_path = config_dir / "opencode.json"
    config_path.write_text(
        """{
  // Keep user provider.
  "provider": {
    "gateway": {
      "options": {"baseURL": "https://gateway.example.test/v1//path"},
      "label": "literal // text and /* text */",
    },
  },
  /* Keep this plugin too. */
  "plugin": ["example-plugin",],
}
""",
        encoding="utf-8",
    )

    get_opencode_config_file(config_dir, project_root)

    result = json.loads(config_path.read_text(encoding="utf-8"))
    gateway = result["provider"]["gateway"]
    assert gateway["options"]["baseURL"] == "https://gateway.example.test/v1//path"
    assert gateway["label"] == "literal // text and /* text */"
    assert result["plugin"] == ["example-plugin"]
