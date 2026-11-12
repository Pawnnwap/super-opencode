"""Regression tests for protocol parsing/storage (BOM, fences, JSONL)."""

import pytest

from supervisor.protocols.protocol import (
    load_protocol,
    parse_protocol_text,
    protocol_from_jsonl,
    protocol_to_jsonl,
    read_protocol_text,
    resolve_protocol_path,
    save_protocol,
    strip_code_fences,
)

VALID_PROTOCOL = (
    "## INPUT\n\nRead-only study workspace.\n\n"
    "## TARGET\n\nAdd an indicator script.\n\n"
    "## RESTRICTIONS\n\n- Do not modify .opencode.\n"
)


def test_parse_valid_protocol():
    protocol = parse_protocol_text(VALID_PROTOCOL)

    assert protocol.input_section == "Read-only study workspace."
    assert protocol.target_section == "Add an indicator script."
    assert protocol.restrictions_section == "- Do not modify .opencode."


def test_parse_tolerates_utf8_bom_in_text():
    protocol = parse_protocol_text("\ufeff" + VALID_PROTOCOL)

    assert protocol.input_section == "Read-only study workspace."


def test_load_protocol_tolerates_utf8_bom(tmp_path):
    path = tmp_path / "protocol.md"
    path.write_bytes(b"\xef\xbb\xbf" + VALID_PROTOCOL.encode("utf-8"))

    protocol = load_protocol(path)

    assert protocol.input_section == "Read-only study workspace."


def test_missing_section_reports_near_miss_heading():
    malformed = (
        "##INPUT\n\nbody\n\n"
        "## TARGET\n\nAdd an indicator script.\n\n"
        "## RESTRICTIONS\n\n- Do not modify .opencode.\n"
    )

    with pytest.raises(ValueError) as excinfo:
        parse_protocol_text(malformed)

    message = str(excinfo.value)
    assert "missing: INPUT" in message
    assert "line 1" in message
    assert "'##INPUT'" in message


def test_strip_markdown_wrapper_fence():
    wrapped = "```markdown\n" + VALID_PROTOCOL + "```\n"
    assert strip_code_fences(wrapped).strip() == VALID_PROTOCOL.strip()
    protocol = parse_protocol_text(wrapped)
    assert protocol.input_section == "Read-only study workspace."


def test_strip_plain_wrapper_fence():
    wrapped = "```\n" + VALID_PROTOCOL + "```"
    assert strip_code_fences(wrapped).strip() == VALID_PROTOCOL.strip()


def test_strip_leaves_inner_code_blocks_alone():
    text = VALID_PROTOCOL + "\n```python\nprint('x')\n```\n"
    assert "```python" in strip_code_fences(text)


def test_jsonl_roundtrip():
    protocol = parse_protocol_text(VALID_PROTOCOL)
    raw = protocol_to_jsonl(protocol)
    assert raw.splitlines()[0].startswith('{"type": "protocol"')
    restored = protocol_from_jsonl(raw)
    assert restored.input_section == protocol.input_section
    assert restored.target_section == protocol.target_section
    assert restored.restrictions_section == protocol.restrictions_section
    assert parse_protocol_text(raw).to_markdown() == protocol.to_markdown()


def test_save_protocol_writes_jsonl_and_drops_legacy(tmp_path):
    legacy = tmp_path / "protocol.md"
    legacy.write_text(VALID_PROTOCOL, encoding="utf-8")
    protocol = parse_protocol_text(VALID_PROTOCOL)

    path = save_protocol(tmp_path, protocol)

    assert path.name == "protocol.jsonl" and path.is_file()
    assert not legacy.exists()
    assert load_protocol(path).target_section == "Add an indicator script."


def test_resolve_prefers_jsonl_then_md(tmp_path):
    assert resolve_protocol_path(tmp_path).name == "protocol.jsonl"
    (tmp_path / "protocol.md").write_text(VALID_PROTOCOL, encoding="utf-8")
    assert resolve_protocol_path(tmp_path).name == "protocol.md"
    save_protocol(tmp_path, parse_protocol_text(VALID_PROTOCOL))
    assert resolve_protocol_path(tmp_path).name == "protocol.jsonl"


def test_read_protocol_text_renders_jsonl(tmp_path):
    path = save_protocol(tmp_path, parse_protocol_text(VALID_PROTOCOL))
    text = read_protocol_text(path)
    assert text.startswith("## INPUT")
    assert "## TARGET" in text and "## RESTRICTIONS" in text
