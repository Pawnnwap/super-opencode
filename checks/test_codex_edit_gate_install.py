"""Install/remove tests for the global Codex edit-gate hook registration."""

from __future__ import annotations

from pathlib import Path

import tomllib
from services.config.codex_edit_gate_install import (
    HOOK_MARK,
    HOOK_TABLE,
    RUST_HOOK_MARK,
    STOP_HOOK_TABLE,
    WINDOWS_HOOK_MARK,
    install_gate,
    install_rust_gate,
    install_windows_gate,
    remove_gate,
    remove_rust_gate,
    remove_windows_gate,
)

CONFIG_TEMPLATE = """model = "some-model"
model_provider = "local-ds"

[model_providers.local-ds]
name = "Local"
base_url = "http://127.0.0.1:8111/v1"
wire_api = "responses"

[mcp_servers.web_tools]
command = 'python'
args = ['web_tools.py']

[[hooks.SessionStart]]
hooks = [
  { type = "command", command = 'powershell -NoProfile -File keep-me.ps1' },
]

[hooks.state]

[hooks.state.'C:\\Users\\a\\.codex\\config.toml:session_start:0:0']
trusted_hash = "sha256:abc123"

[features]
daemon_auto_start = true
"""


def _home(tmp_path) -> Path:
    home = tmp_path / "codex_home"
    home.mkdir()
    (home / "config.toml").write_text(CONFIG_TEMPLATE, encoding="utf-8")
    return home


def _gate_source(tmp_path) -> Path:
    source = tmp_path / "py_edit_gate.py"
    source.write_text("print('gate')\n", encoding="utf-8")
    return source


def test_install_registers_hook_and_preserves_every_other_setting(tmp_path):
    home = _home(tmp_path)
    assert install_gate(home, _gate_source(tmp_path), interpreter="py-test") is True

    text = (home / "config.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert data["model"] == "some-model"
    assert data["model_providers"]["local-ds"]["base_url"] == "http://127.0.0.1:8111/v1"
    assert data["mcp_servers"]["web_tools"]["args"] == ["web_tools.py"]
    assert data["features"]["daemon_auto_start"] is True
    assert data["hooks"]["state"][
        "C:\\Users\\a\\.codex\\config.toml:session_start:0:0"
    ]["trusted_hash"]
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"].endswith(
        "keep-me.ps1"
    )

    post = data["hooks"]["PostToolUse"]
    assert len(post) == 1
    assert post[0]["matcher"] == "apply_patch|Edit|Write|MultiEdit|edit_file|write_file"
    command = post[0]["hooks"][0]["command"]
    assert command.startswith("py-test ")
    assert command.endswith("py_edit_gate.py")
    assert post[0]["hooks"][0]["timeout"] == 25

    assert (home / "scripts" / "py_edit_gate.py").is_file()
    assert (home / "logs").is_dir()
    backups = list(home.glob("config.toml.bak-editgate-*"))
    assert len(backups) == 1
    assert "hooks.PostToolUse" not in backups[0].read_text(encoding="utf-8")


def test_second_install_is_idempotent_and_refreshes_in_place(tmp_path):
    home = _home(tmp_path)
    source = _gate_source(tmp_path)
    assert install_gate(home, source, interpreter="py-test") is True
    first = (home / "config.toml").read_text(encoding="utf-8")
    assert install_gate(home, source, interpreter="py-test") is True
    assert (home / "config.toml").read_text(encoding="utf-8") == first
    assert first.count(HOOK_TABLE) == 1

    assert install_gate(home, source, interpreter="py-new") is True
    text = (home / "config.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert text.count(HOOK_TABLE) == 1
    assert data["hooks"]["PostToolUse"][0]["hooks"][0]["command"].startswith("py-new ")
    assert len(data["hooks"]["SessionStart"]) == 1


def test_no_autofix_flag_reaches_the_hook_command(tmp_path):
    home = _home(tmp_path)
    assert (
        install_gate(
            home, _gate_source(tmp_path), interpreter="py-test", allow_autofix=False
        )
        is True
    )
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert data["hooks"]["PostToolUse"][0]["hooks"][0]["command"].endswith(
        "--no-autofix"
    )


def test_remove_keeps_the_session_hook_and_the_installed_script(tmp_path):
    home = _home(tmp_path)
    install_gate(home, _gate_source(tmp_path), interpreter="py-test")
    assert remove_gate(home) is True
    text = (home / "config.toml").read_text(encoding="utf-8")
    assert HOOK_MARK not in text
    data = tomllib.loads(text)
    assert "PostToolUse" not in data["hooks"]
    assert len(data["hooks"]["SessionStart"]) == 1
    assert data["features"]["daemon_auto_start"] is True
    assert (home / "scripts" / "py_edit_gate.py").is_file()
    assert remove_gate(home) is True  # idempotent


def test_refuses_to_touch_an_unparseable_config(tmp_path):
    home = tmp_path / "broken_home"
    home.mkdir()
    (home / "config.toml").write_text("model = [unclosed\n", encoding="utf-8")
    warnings: list[str] = []
    assert (
        install_gate(home, _gate_source(tmp_path), on_warning=warnings.append) is False
    )
    assert any("does not parse" in msg for msg in warnings)
    assert (home / "config.toml").read_text(encoding="utf-8") == "model = [unclosed\n"


def test_missing_source_script_is_reported(tmp_path):
    home = _home(tmp_path)
    warnings: list[str] = []
    assert (
        install_gate(home, tmp_path / "nothing.py", on_warning=warnings.append) is False
    )
    assert any("Gate script missing" in msg for msg in warnings)
    assert HOOK_TABLE not in (home / "config.toml").read_text(encoding="utf-8")


def test_apostrophe_in_command_is_refused_instead_of_mangled(tmp_path):
    home = _home(tmp_path)
    warnings: list[str] = []
    assert (
        install_gate(
            home,
            _gate_source(tmp_path),
            interpreter="C:\\o'brien\\python.exe",
            on_warning=warnings.append,
        )
        is False
    )
    assert any("apostrophe" in msg for msg in warnings)
    assert HOOK_MARK not in (home / "config.toml").read_text(encoding="utf-8")


def _rust_source(tmp_path) -> Path:
    source = tmp_path / "rs_edit_gate.py"
    source.write_text("print('rust gate')\n", encoding="utf-8")
    return source


def test_rust_gate_registers_edit_and_stop_hooks(tmp_path):
    home = _home(tmp_path)
    assert install_rust_gate(home, _rust_source(tmp_path), interpreter="py-test") is True
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    post = data["hooks"]["PostToolUse"][0]
    assert post["matcher"] == "apply_patch|Edit|Write|MultiEdit|edit_file|write_file"
    assert post["hooks"][0]["command"].endswith("rs_edit_gate.py")
    stop = data["hooks"]["Stop"][0]
    assert stop["hooks"][0]["command"].endswith("rs_edit_gate.py --stop")
    assert stop["hooks"][0]["timeout"] == 60
    assert "matcher" not in stop
    assert len(data["hooks"]["SessionStart"]) == 1
    assert data["features"]["daemon_auto_start"] is True
    assert (home / "scripts" / "rs_edit_gate.py").is_file()


def test_rust_gate_install_is_idempotent(tmp_path):
    home = _home(tmp_path)
    source = _rust_source(tmp_path)
    assert install_rust_gate(home, source, interpreter="py-test") is True
    first = (home / "config.toml").read_text(encoding="utf-8")
    assert install_rust_gate(home, source, interpreter="py-test") is True
    assert (home / "config.toml").read_text(encoding="utf-8") == first
    assert first.count(STOP_HOOK_TABLE) == 1


def test_rust_gate_can_share_one_installed_script(tmp_path):
    home = _home(tmp_path)
    shared = tmp_path / "shared" / "rs_edit_gate.py"
    shared.parent.mkdir()
    shared.write_text("print('shared')\n", encoding="utf-8")
    assert install_rust_gate(home, shared, interpreter="py-test", shared_script=shared) is True
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert str(shared) in data["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert not (home / "scripts").exists()


def test_removing_rust_gate_keeps_the_python_gate(tmp_path):
    home = _home(tmp_path)
    install_gate(home, _gate_source(tmp_path), interpreter="py-test")
    install_rust_gate(home, _rust_source(tmp_path), interpreter="py-test")
    assert remove_rust_gate(home) is True
    text = (home / "config.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert RUST_HOOK_MARK not in text
    assert HOOK_MARK in text
    assert "Stop" not in data["hooks"]
    assert len(data["hooks"]["PostToolUse"]) == 1
    assert len(data["hooks"]["SessionStart"]) == 1
    assert remove_rust_gate(home) is True  # idempotent


def _win_source(tmp_path) -> Path:
    source = tmp_path / "win_edit_gate.py"
    source.write_text("print('win gate')\n", encoding="utf-8")
    return source


def test_windows_gate_registers_one_post_tool_use_block(tmp_path):
    home = _home(tmp_path)
    assert install_windows_gate(home, _win_source(tmp_path), interpreter="py-test") is True
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    post = data["hooks"]["PostToolUse"][0]
    assert post["matcher"] == "apply_patch|Edit|Write|MultiEdit|edit_file|write_file"
    assert post["hooks"][0]["command"].endswith("win_edit_gate.py")
    assert post["hooks"][0]["timeout"] == 25
    assert "Stop" not in data["hooks"]  # nothing runs at turn end for scripts
    assert len(data["hooks"]["SessionStart"]) == 1
    assert data["features"]["daemon_auto_start"] is True
    assert (home / "scripts" / "win_edit_gate.py").is_file()
    assert (home / "logs").is_dir()


def test_windows_gate_install_is_idempotent(tmp_path):
    home = _home(tmp_path)
    source = _win_source(tmp_path)
    assert install_windows_gate(home, source, interpreter="py-test") is True
    first = (home / "config.toml").read_text(encoding="utf-8")
    assert install_windows_gate(home, source, interpreter="py-test") is True
    assert (home / "config.toml").read_text(encoding="utf-8") == first
    assert first.count(HOOK_TABLE) == 1


def test_windows_gate_optional_flags_reach_the_hook_command(tmp_path):
    home = _home(tmp_path)
    assert (
        install_windows_gate(
            home, _win_source(tmp_path), interpreter="py-test", allow_format=True, allow_lint=True
        )
        is True
    )
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert data["hooks"]["PostToolUse"][0]["hooks"][0]["command"].endswith("--format --lint")


def test_all_three_gates_coexist_and_removals_are_independent(tmp_path):
    home = _home(tmp_path)
    assert install_gate(home, _gate_source(tmp_path), interpreter="py-test") is True
    assert install_rust_gate(home, _rust_source(tmp_path), interpreter="py-test") is True
    assert install_windows_gate(home, _win_source(tmp_path), interpreter="py-test") is True
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    commands = [entry["hooks"][0]["command"] for entry in data["hooks"]["PostToolUse"]]
    assert len(commands) == 3
    assert any(c.endswith("py_edit_gate.py") for c in commands)
    assert any(c.endswith("rs_edit_gate.py") for c in commands)
    assert any(c.endswith("win_edit_gate.py") for c in commands)

    assert remove_windows_gate(home) is True
    text = (home / "config.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert WINDOWS_HOOK_MARK not in text
    assert HOOK_MARK in text and RUST_HOOK_MARK in text
    assert len(data["hooks"]["PostToolUse"]) == 2
    assert len(data["hooks"]["Stop"]) == 1
    assert remove_windows_gate(home) is True  # idempotent


def test_windows_gate_can_share_one_installed_script(tmp_path):
    home = _home(tmp_path)
    shared = tmp_path / "shared" / "win_edit_gate.py"
    shared.parent.mkdir()
    shared.write_text("print('shared')\n", encoding="utf-8")
    assert install_windows_gate(home, shared, interpreter="py-test", shared_script=shared) is True
    data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert str(shared) in data["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert not (home / "scripts").exists()


def test_missing_windows_gate_source_is_reported(tmp_path):
    home = _home(tmp_path)
    warnings: list[str] = []
    assert (
        install_windows_gate(home, tmp_path / "nothing.py", on_warning=warnings.append) is False
    )
    assert any("Windows gate script missing" in msg for msg in warnings)
    assert HOOK_TABLE not in (home / "config.toml").read_text(encoding="utf-8")
