"""Checks for the per-engine access guards and the AI whitelist advisor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from supervisor.security.access_policy import build_policy
import supervisor.security.engine_guards as engine_guards
from supervisor.security.engine_guards import (
    GUARD_MARKER,
    install_access_guard,
    opencode_permission_config,
    policy_path_for,
)
from supervisor.security.guard_cli import decision_payload, violations_for_event
from supervisor.security.whitelist_advisor import parse_suggestions, suggest_whitelist


@pytest.fixture(autouse=True)
def isolated_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(engine_guards, "POLICY_STORE_DIR", tmp_path / "store")


@pytest.fixture()
def layout(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ref = tmp_path / "ref"
    ref.mkdir()
    policy = build_policy(ws, [ref])
    return ws, ref, policy


def test_snapshot_written(layout):
    ws, ref, policy = layout
    result = install_access_guard(ws, policy, "opencode", codex_home=ws / "codex_home")
    snapshot = policy_path_for(ws, policy)
    assert snapshot in result.artifacts
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    assert data["workspace"].replace("\\", "/").endswith("ws")
    assert {root["mode"] for root in data["roots"]} == {"rw", "ro"}
    assert str(ref).replace("\\", "/") in json.dumps(data)


def test_opencode_rules_deny_outside_and_writes_to_whitelist(layout):
    ws, ref, policy = layout
    rules = opencode_permission_config(policy)
    assert rules["external_directory"]["*"] == "deny"
    boundary = str(ref).replace("\\", "/") + "/*"
    assert rules["external_directory"][boundary] == "allow"
    assert rules["read"][boundary] == "allow"
    assert rules["edit"][boundary] == "deny"


def test_opencode_install_preserves_foreign_config_and_is_idempotent(layout):
    ws, _, policy = layout
    config_path = ws / ".opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "permission": {"bash": {"git status *": "allow"}},
            },
        ),
        encoding="utf-8",
    )

    install_access_guard(ws, policy, "opencode", codex_home=ws / "codex_home")
    first = json.loads(config_path.read_text(encoding="utf-8"))
    install_access_guard(ws, policy, "opencode", codex_home=ws / "codex_home")
    second = json.loads(config_path.read_text(encoding="utf-8"))

    assert second["theme"] == "dark"
    assert second["permission"]["bash"]["git status *"] == "allow"
    assert second["permission"]["external_directory"]["*"] == "deny"
    # V1 rejects the V2 "permissions" key outright, so it must never be written.
    assert "permissions" not in second
    assert sorted(second["permission"]["external_directory"]) == sorted(
        first["permission"]["external_directory"],
    )
    assert len(second["permission"]["external_directory"]) == 2  # "*" + whitelist
    assert (ws / ".opencode" / "plugin" / "supervisor_access_guard.js").is_file()


def test_opencode_install_adds_v1_permission_defaults(layout):
    ws, _, policy = layout
    config_path = ws / ".opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}", encoding="utf-8")
    install_access_guard(ws, policy, "opencode", codex_home=ws / "codex_home")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["permission"]["bash"] == "allow"
    assert data["permission"]["external_directory"]["*"] == "deny"
    assert data["autoapprove"] is True


def test_opencode_install_strips_legacy_v2_key(layout):
    ws, _, policy = layout
    config_path = ws / ".opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    legacy_rule = {"action": "external_directory", "resource": "*", "effect": "deny"}
    config_path.write_text(
        json.dumps({"permissions": [legacy_rule]}), encoding="utf-8"
    )
    marker = config_path.parent / ".supervisor_generated_permissions.json"
    marker.write_text(json.dumps({"v2": [legacy_rule]}), encoding="utf-8")

    install_access_guard(ws, policy, "opencode", codex_home=ws / "codex_home")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "permissions" not in data


def test_disable_removes_only_our_opencode_pieces(layout):
    ws, ref, policy = layout
    config_path = ws / ".opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"permission": {"bash": {"git status *": "allow"}}}),
        encoding="utf-8",
    )
    install_access_guard(ws, policy, "opencode", codex_home=ws / "codex_home")
    install_access_guard(ws, build_policy(ws, [], enabled=False), "opencode", codex_home=ws / "codex_home")

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["permission"]["bash"]["git status *"] == "allow"
    assert data["permission"].get("external_directory", {}) == {}
    assert str(ref).replace("\\", "/") + "/*" not in json.dumps(data)
    assert not (ws / ".opencode" / "plugin" / "supervisor_access_guard.js").exists()
    assert not policy_path_for(ws, policy).exists()


def test_pi_install_writes_extension_and_flags(layout):
    ws, _, policy = layout
    result = install_access_guard(ws, policy, "pi", codex_home=ws / "codex_home")
    extension = ws / ".pi" / "extensions" / "supervisor-access-guard.ts"
    assert extension.is_file()
    assert GUARD_MARKER in extension.read_text(encoding="utf-8")
    assert result.flags == ["-e", str(extension)]


def test_codex_install_registers_pretooluse_hook(layout, tmp_path):
    ws, _, policy = layout
    home = tmp_path / "codex_home"
    home.mkdir()
    hooks_path = home / "hooks.json"
    hooks_path.write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo keep"}]}]}}),
        encoding="utf-8",
    )

    result = install_access_guard(ws, policy, "codex", codex_home=home)
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    entries = data["hooks"]["PreToolUse"]
    commands = [handler["command"] for entry in entries for handler in entry["hooks"]]
    assert any("supervisor.security.guard_cli" in command for command in commands)
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo keep"
    assert "--dangerously-bypass-hook-trust" in result.flags

    # Second install replaces our entry rather than duplicating it.
    install_access_guard(ws, policy, "codex", codex_home=home)
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    guard_entries = [
        entry
        for entry in data["hooks"]["PreToolUse"]
        if any(GUARD_MARKER in str(h.get("statusMessage", "")) for h in entry["hooks"])
    ]
    assert len(guard_entries) == 1


def test_codex_disable_removes_hook(layout, tmp_path):
    ws, _, policy = layout
    home = tmp_path / "codex_home"
    home.mkdir()
    install_access_guard(ws, policy, "codex", codex_home=home)
    install_access_guard(ws, build_policy(ws, [], enabled=False), "codex", codex_home=home)
    data = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
    commands = [
        handler["command"]
        for entry in data.get("hooks", {}).get("PreToolUse", [])
        for handler in entry.get("hooks", [])
    ]
    assert not any("supervisor.security.guard_cli" in command for command in commands)


class TestGuardCli:
    def _event(self, tool: str, tool_input: dict, cwd: Path) -> dict:
        return {"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)}

    def test_denies_read_outside(self, layout, tmp_path):
        ws, _, policy = layout
        event = self._event("read_file", {"path": str(tmp_path / "secret.txt")}, ws)
        violations = violations_for_event(policy, event)
        assert violations
        payload = decision_payload(violations, policy)
        assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_allows_cwd(self, layout):
        ws, _, policy = layout
        event = self._event("write", {"path": str(ws / "a.py")}, ws)
        assert violations_for_event(policy, event) == []

    def test_denies_whitelist_write(self, layout):
        ws, ref, policy = layout
        event = self._event("apply_patch", {"path": str(ref / "x.txt")}, ws)
        violations = violations_for_event(policy, event)
        assert violations and "read-only" in violations[0].reason

    def test_apply_patch_headers_are_scanned(self, layout, tmp_path):
        ws, _, policy = layout
        target = tmp_path / "outside.txt"
        patch = f"*** Begin Patch\n*** Update File: {target}\n+x\n*** End Patch"
        event = self._event("apply_patch", {"input": patch}, ws)
        violations = violations_for_event(policy, event)
        assert violations

    def test_shell_command_is_scanned(self, layout, tmp_path):
        ws, _, policy = layout
        event = self._event("shell", {"command": f'type "{tmp_path / "secret.txt"}"'}, ws)
        assert violations_for_event(policy, event)

    def test_cli_is_inert_without_policy_env(self, tmp_path):
        proc = subprocess.run(
            [sys.executable, "-m", "supervisor.security.guard_cli", "--policy-env", "SOP_ACCESS_POLICY"],
            input=json.dumps({"tool_name": "shell", "tool_input": {"command": "ls"}}),
            capture_output=True,
            text=True,
            env={k: v for k, v in os.environ.items() if k != "SOP_ACCESS_POLICY"},
            check=False,
        )
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""

    def test_cli_check_command_mode(self, layout, tmp_path):
        ws, ref, _ = layout
        install = install_access_guard(
            ws, build_policy(ws, [ref]), "opencode", codex_home=tmp_path / "ch",
        )
        env = {**os.environ, **install.env}

        def run(command: str) -> dict:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "supervisor.security.guard_cli",
                    "--policy-env",
                    "SOP_ACCESS_POLICY",
                    "--check-command",
                ],
                input=json.dumps({"command": command, "cwd": str(ws)}),
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            assert proc.returncode == 0, proc.stderr
            return json.loads(proc.stdout)

        assert run(f'cp "{ref}/a.txt" "{ws}/b.txt"')["allow"] is True
        assert run(f'cp "{ws}/a.txt" "{ref}/b.txt"')["allow"] is False
        assert run('python -c "print(1)"')["allow"] is False

    def test_missing_policy_fails_closed(self, tmp_path):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "supervisor.security.guard_cli",
                "--policy",
                str(tmp_path / "missing.json"),
            ],
            input=json.dumps({"tool_name": "shell", "tool_input": {"command": "ls"}}),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "deny" in proc.stdout

    def test_cli_denies_with_policy_env(self, layout, tmp_path):
        ws, _, _ = layout
        snapshot = policy_path_for(ws)
        install_access_guard(ws, build_policy(ws, []), "codex", codex_home=tmp_path / "ch")
        env = dict(os.environ)
        env["SOP_ACCESS_POLICY"] = str(snapshot)
        proc = subprocess.run(
            [sys.executable, "-m", "supervisor.security.guard_cli", "--policy-env", "SOP_ACCESS_POLICY"],
            input=json.dumps(
                {
                    "tool_name": "shell",
                    "tool_input": {"command": f'cat "{tmp_path / "secret"}"'},
                    "cwd": str(ws),
                },
            ),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0
        assert "deny" in proc.stdout


class TestAdvisor:
    def test_accepts_valid_and_rejects_inside(self, layout):
        ws, ref, _ = layout
        raw = json.dumps(
            [
                {"path": str(ref), "reason": "corpus", "confidence": 0.9},
                {"path": str(ws / "inside"), "reason": "bad", "confidence": 0.9},
                {"path": "C:/Windows", "reason": "bad", "confidence": 1.0},
            ],
        )
        result = parse_suggestions(raw, ws)
        assert [str(item.path) for item in result.accepted] == [str(ref.resolve())]
        assert len(result.rejected) == 2

    def test_dedupes_and_skips_existing(self, layout):
        ws, ref, _ = layout
        raw = json.dumps([{"path": str(ref)}, {"path": str(ref)}])
        result = parse_suggestions(raw, ws, existing=[str(ref)])
        assert result.accepted == []
        assert result.rejected

    def test_handles_code_fence(self, layout):
        ws, ref, _ = layout
        raw = "```json\n[" + json.dumps({"path": str(ref)}) + "]\n```"
        assert len(parse_suggestions(raw, ws).accepted) == 1

    def test_suggest_passes_context_and_validates(self, layout):
        ws, ref, _ = layout
        seen: list[list[dict[str, str]]] = []

        def fake_llm(messages):
            seen.append(messages)
            return json.dumps([{"path": str(ref), "reason": "fixtures"}])

        result = suggest_whitelist(ws, "## TARGET\nread fixtures", fake_llm)
        assert seen and "read fixtures" in seen[0][1]["content"]
        assert result.accepted[0].path == ref.resolve()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
