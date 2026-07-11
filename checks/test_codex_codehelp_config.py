from __future__ import annotations

import subprocess
from pathlib import Path

from services.config.codex_config import CODEHELP_SERVER_NAME, ensure_codehelp_codex_mcp


def _result(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_registers_missing_codehelp_server_without_replacing_other_entries(tmp_path, monkeypatch):
    script = tmp_path / "mcp_server" / "codehelp.py"
    script.parent.mkdir()
    script.write_text("# codehelp", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return _result(1, stderr="No MCP server named") if len(calls) == 1 else _result(0)

    monkeypatch.setattr("services.config.codex_config.subprocess.run", fake_run)

    assert ensure_codehelp_codex_mcp(
        tmp_path,
        python_executable="python-test",
        codex_executable="codex-test",
    )
    assert calls == [
        ["codex-test", "mcp", "get", CODEHELP_SERVER_NAME],
        [
            "codex-test",
            "mcp",
            "add",
            CODEHELP_SERVER_NAME,
            "--",
            "python-test",
            str(script.resolve()),
        ],
    ]


def test_keeps_existing_codehelp_server_unchanged(tmp_path, monkeypatch):
    script = tmp_path / "mcp_server" / "codehelp.py"
    script.parent.mkdir()
    script.write_text("# codehelp", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return _result(0, stdout="already configured")

    monkeypatch.setattr("services.config.codex_config.subprocess.run", fake_run)

    assert ensure_codehelp_codex_mcp(tmp_path, codex_executable="codex-test")
    assert calls == [["codex-test", "mcp", "get", CODEHELP_SERVER_NAME]]
