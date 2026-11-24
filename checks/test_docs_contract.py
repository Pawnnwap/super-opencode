"""Operator docs and rule prompts describe the current contracts."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_manual_test_command_collects_the_whole_suite():
    manual = _text("MANUAL.md")
    assert "pytest checks -q" in manual
    assert "pytest checks\\test_*.py" not in manual


def test_scribe_prompt_declares_five_sections():
    scribe = _text("supervisor/prompts/scribe_rules.md")
    assert "exactly these five sections" in scribe
    assert "exactly these four sections" not in scribe
    assert "SUGGESTIONS" in scribe


def test_docs_identify_jsonl_as_canonical_protocol():
    readme = _text("README.md")
    assert "protocol.jsonl" in readme
    assert "canonical" in readme.lower()


def test_docs_state_archive_retention_and_rule_gate_limit():
    readme = _text("README.md")
    manual = _text("MANUAL.md")
    assert "newest" in readme.lower() and "retained" in readme.lower()
    assert "not a kernel sandbox" in manual.lower()


def test_codex_home_comment_does_not_deny_configured_mcp_servers():
    config = _text(".codex_home_soprun/config.toml")
    assert "no MCP servers" not in config
    assert "[mcp_servers.web_tools]" in config
