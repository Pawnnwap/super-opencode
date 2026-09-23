"""Regression tests for protocol.md parsing robustness (BOM, near-miss headings)."""

import pytest

from supervisor.protocols.protocol import load_protocol, parse_protocol_text

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
