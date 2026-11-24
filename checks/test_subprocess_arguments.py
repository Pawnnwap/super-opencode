"""Mutable configuration must never become shell syntax."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.runtime import app_bootstrap as boot


def test_npm_registry_is_one_validated_argument():
    command = boot._npm_install_cmd("opencode-ai", "https://registry.example/path")

    assert command == [
        "npm",
        "install",
        "-g",
        "opencode-ai@latest",
        "--registry",
        "https://registry.example/path",
    ]


@pytest.mark.parametrize(
    "registry",
    [
        "https://registry.example; calc.exe",
        "https://registry.example && calc.exe",
        "https://user:pass@registry.example",
        "not-a-url",
    ],
)
def test_invalid_npm_registry_is_refused(registry):
    with pytest.raises(ValueError, match="registry"):
        boot._npm_install_cmd("opencode-ai", registry)


def test_codex_upgrade_uses_argv_without_a_shell(tmp_path, monkeypatch):
    seen = {}

    class _Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    def _popen(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(boot.subprocess, "Popen", _popen)

    boot._run_codex_upgrade(
        ["npm", "install", "-g", "@openai/codex@latest"], str(tmp_path)
    )

    assert seen["command"] == ["npm", "install", "-g", "@openai/codex@latest"]
    assert seen["kwargs"]["shell"] is False


def test_launcher_validates_and_quotes_upstream():
    source = (Path(__file__).resolve().parents[1] / "start_suprun.ps1").read_text(
        encoding="utf-8-sig",
    )

    assert "function Test-HttpUrl" in source
    assert "$env:SOP_UPSTREAM" in source
    assert 'codex_compat_proxy.py {2} "{3}"' in source
    assert "codex_compat_proxy.py {2} {3}" not in source


def test_launcher_persists_scope_and_uses_scope_aware_readiness():
    source = (Path(__file__).resolve().parents[1] / "start_suprun.ps1").read_text(
        encoding="utf-8-sig",
    )

    assert "proxy_only =" in source
    assert "app_only =" in source
    assert "$needProxy" in source
    assert "$needApp" in source
    assert "$needWorker" in source
