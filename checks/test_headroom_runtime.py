from __future__ import annotations

import json
from pathlib import Path

from supervisor.runners.opencode_support.headroom import (
    HeadroomPlan,
    HeadroomProxyLease,
    build_headroom_environment,
    resolve_headroom_plan,
)
from supervisor.runners.opencode_support import process as opencode_process
from supervisor.runners.opencode_support import command_builder as opencode_commands
from supervisor.runners.opencode_runner import OpencodeRunner


def test_native_provider_uses_headroom_when_executable_exists(tmp_path):
    executable = tmp_path / "headroom.exe"
    executable.write_text("", encoding="utf-8")

    plan = resolve_headroom_plan(
        enabled=True,
        model="openai/gpt-4.1",
        find_headroom_fn=lambda _explicit: str(executable),
    )

    assert plan.enabled is True
    assert plan.executable == str(executable)
    assert plan.reason == "native openai provider"


def test_custom_provider_stays_direct_without_explicit_override():
    plan = resolve_headroom_plan(
        enabled=True,
        model="my-gateway/qwen3-coder",
        find_headroom_fn=lambda _explicit: "headroom",
    )

    assert plan.enabled is False
    assert "custom provider 'my-gateway'" in plan.reason


def test_missing_headroom_soft_falls_back_to_direct_execution():
    plan = resolve_headroom_plan(
        enabled=True,
        model="anthropic/claude-sonnet-4-6",
        find_headroom_fn=lambda _explicit: (_ for _ in ()).throw(FileNotFoundError()),
    )

    assert plan.enabled is False
    assert plan.reason == "Headroom executable not found"


def test_launch_environment_is_child_scoped_and_points_mcp_to_live_proxy(tmp_path):
    environment = build_headroom_environment(
        {"UNCHANGED": "yes"},
        executable="C:/tools/headroom.exe",
        port=18999,
        workspace=tmp_path / "project",
    )

    config = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
    assert environment["UNCHANGED"] == "yes"
    assert environment["HEADROOM_PROXY_URL"] == "http://127.0.0.1:18999"
    assert environment["HEADROOM_PROJECT"] == "project"
    assert config["provider"]["openai"]["options"]["baseURL"].endswith(":18999/v1")
    assert config["mcp"]["headroom"]["command"] == [
        "C:/tools/headroom.exe",
        "mcp",
        "serve",
        "--proxy-url",
        "http://127.0.0.1:18999",
    ]


def test_process_uses_child_scoped_headroom_environment_for_active_lease(monkeypatch, tmp_path):
    class Runner:
        enable_headroom = True
        headroom_executable = ""
        headroom_allow_custom_provider = False
        timeout = 30
        workspace = tmp_path / "project"
        _headroom_lease = HeadroomProxyLease(port=18888, managed=True)

    monkeypatch.setattr(
        opencode_process,
        "resolve_headroom_plan",
        lambda **_kwargs: HeadroomPlan(True, "native openai provider", "headroom.exe"),
    )
    monkeypatch.setattr(opencode_process, "is_headroom_proxy_healthy", lambda _port: True)

    environment, status = opencode_process._prepare_child_environment(
        Runner(),
        "openai/gpt-4.1",
    )

    assert status == "Headroom active on port 18888: native openai provider"
    assert json.loads(environment["OPENCODE_CONFIG_CONTENT"])["mcp"]["headroom"]["command"][0] == "headroom.exe"


def test_runner_releases_headroom_lease_on_stop(monkeypatch, tmp_path):
    released = []
    runner = OpencodeRunner(workspace=tmp_path, timeout=10, enable_headroom=True)
    runner._headroom_lease = HeadroomProxyLease(port=18888, managed=True)
    monkeypatch.setattr(
        "supervisor.runners.opencode_runner.release_headroom_proxy",
        released.append,
    )

    runner.stop()

    assert released == [HeadroomProxyLease(port=18888, managed=True)]
    assert runner._headroom_lease is None


def test_dot_model_file_is_resolved_before_headroom_decision(monkeypatch, tmp_path):
    model_file = tmp_path / ".opencode_model"
    model_file.write_text("anthropic/claude-sonnet-4-6\n", encoding="utf-8")
    monkeypatch.setattr(opencode_commands, "_DOT_MODEL_FILE", model_file)

    model = opencode_commands.resolve_model(None, None)
    plan = resolve_headroom_plan(
        enabled=True,
        model=model,
        find_headroom_fn=lambda _explicit: "headroom",
    )

    assert model == "anthropic/claude-sonnet-4-6"
    assert plan.enabled is True
