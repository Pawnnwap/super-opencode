"""Agents receive only the policy and environment required for a run."""

from __future__ import annotations

from types import SimpleNamespace

from supervisor.runners.process_common import default_child_env
from supervisor.runtime.process_base import OwnedProcess
from supervisor.security.access_policy import AccessPolicy, build_policy


class _Process(OwnedProcess):
    def resolve_exe(self):
        return "python"

    def build_argv(self):
        return ["python", "--version"]


def test_default_policy_denies_interpreter_scripts(tmp_path):
    policy = build_policy(tmp_path)

    assert policy.strict_dynamic is True
    assert policy.scan_command("python script.py")


def test_config_policy_does_not_auto_whitelist_secret_directories(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    for relative in (".codex", ".config/opencode", ".pi", ".agents/skills"):
        (home / relative).mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
    config = SimpleNamespace(
        workspace=tmp_path / "workspace",
        access_whitelist=(),
        access_policy_enabled=True,
    )

    policy = AccessPolicy.from_config(config)

    assert home / ".codex" not in policy.whitelist
    assert home / ".config" / "opencode" not in policy.whitelist
    assert home / ".pi" not in policy.whitelist
    assert home / ".agents" / "skills" in policy.whitelist


def test_child_environment_excludes_unrelated_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "C:/Windows")
    monkeypatch.setenv("SYSTEMROOT", "C:/Windows")
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "another-secret")
    monkeypatch.setenv("SOP_RUN_ID", "run_explicit")
    process = _Process(run_id="run_base", cwd=tmp_path)

    env = process.base_env()
    default_env = default_child_env()

    assert env["PATH"] == "C:/Windows"
    assert env["SOP_RUN_ID"] == "run_base"
    assert "OPENAI_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in default_env


def test_explicit_child_environment_can_inject_required_credentials(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PATH", "C:/Windows")
    process = _Process(
        run_id="run_explicit",
        cwd=tmp_path,
        env={"SUPER_OPENCODE_CODEX_KEY": "required"},
    )

    env = process.base_env()

    assert env["SUPER_OPENCODE_CODEX_KEY"] == "required"
    assert "OPENAI_API_KEY" not in env
