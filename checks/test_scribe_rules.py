"""The REQUIREMENTS SCRIBE rules and how the wizard composes them.

Measured against the live endpoint (spark-x2.6) on 2026-11-19: with the rules
the model kept a named file a file, invented no base classes or helper
modules for a bare "refactor" note, and reported a create-vs-forbid pair
instead of silently resolving it. Without the rules the same model produced
"place it in the same directory", "follow the naming convention", and other
requirements the user never stated.
"""

from pathlib import Path

from supervisor.prompts.templates import SCRIBE_RULES_PATH, load_scribe_rules
from supervisor.protocols import protocol_wizard


def test_rules_file_ships_and_is_non_trivial():
    rules = load_scribe_rules()

    assert Path(SCRIBE_RULES_PATH).is_file()
    assert len(rules) > 800


def test_rules_state_every_operational_rule():
    rules = load_scribe_rules()

    for marker in (
        "R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8",
        "SUGGESTIONS",
        "suggestion, not adopted",
    ):
        assert marker in rules, marker


def test_rules_forbid_invented_artifacts_and_bare_helpers():
    rules = load_scribe_rules()

    assert "class names" in rules
    assert "base class" in rules


def test_wizard_prompt_carries_the_rules():
    prompt = protocol_wizard._wizard_system_prompt()

    assert "REQUIREMENTS SCRIBE" in prompt
    assert "R5 Self-check contradictions" in prompt


def test_wizard_prompt_keeps_the_three_section_output():
    prompt = protocol_wizard._wizard_system_prompt()

    # The scribe's own four-part report must not replace the protocol.md shape.
    assert "OUTPUT OVERRIDE" in prompt
    assert "three-section protocol.md" in prompt
    assert "## TARGET" in prompt


def test_wizard_prompt_is_plain_when_rules_are_missing(monkeypatch):
    monkeypatch.setattr(
        "supervisor.prompts.templates.load_scribe_rules", lambda: "",
    )

    prompt = protocol_wizard._wizard_system_prompt()

    assert "REQUIREMENTS SCRIBE" not in prompt
    assert "Protocol has 3 sections" in prompt
