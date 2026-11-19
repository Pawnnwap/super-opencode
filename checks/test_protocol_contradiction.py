"""Regression checks for protocol self-contradiction detection.

Three 2026-11-19 incidents motivated this module: a file path probed as a
missing directory before the run started, bare targets silently expanded into
base classes plus utility modules, and a TARGET ordering files into a
directory its RESTRICTIONS forbade writing to. A protocol carrying that last
pair can never be satisfied; the run burns its budget proving it and closes
BLOCKED.
"""

from supervisor.protocols.contradiction_audit import (
    audit_contradictions,
    audit_path_types,
)
from supervisor.protocols.protocol_analyzer import ProtocolAnalyzer
from supervisor.protocols.protocol_wizard import ProtocolGateError, ProtocolWizard

FACCREATE_TARGET = (
    "1. Create the indicator file faccreate/orderfacwavexx.m.\n"
    "2. Create faccreate/verify_refactor.m and run pytest until it passes.\n"
    "3. Done when pytest passes with no regression."
)
FACCREATE_RESTRICTIONS = (
    "- Do not create any file under faccreate other than orderfacwavexx.m."
)


class ScriptedWizard(ProtocolWizard):
    def __init__(self, replies):
        self._model = "test-model"
        self.replies = list(replies)
        self.prompts: list[str] = []

    def _chat(self, user_msg, on_progress=None, stop_event=None):
        self.prompts.append(user_msg)
        return self.replies.pop(0)


def test_create_inside_forbidden_directory_is_blocking():
    audit = audit_contradictions(FACCREATE_TARGET, FACCREATE_RESTRICTIONS)

    assert audit.blocking
    assert not audit.ok
    assert any("verify_refactor.m" in item.message for item in audit.contradictions)


def test_carve_out_file_is_not_reported():
    audit = audit_contradictions(FACCREATE_TARGET, FACCREATE_RESTRICTIONS)
    messages = " ".join(item.message for item in audit.contradictions)

    assert "orderfacwavexx.m under" not in messages


def test_consistent_protocol_is_clean():
    audit = audit_contradictions(
        "1. Create src/indicator.py in the workspace.\n"
        "2. Done when pytest passes.",
        "- Do not edit files outside the workspace.",
    )

    assert audit.ok and not audit.blocking
    assert audit.issues() == ()


def test_outside_restriction_allows_writes_inside_the_named_directory():
    # "outside services/" permits writes inside services/; reading it as a
    # forbidden directory reported a legal target as impossible.
    audit = audit_contradictions(
        "1. Refactor the duplicate parsers into services/common.py.\n"
        "2. Done when the full pytest suite passes with no regression.",
        "- Do not edit files outside services/.",
    )

    assert audit.ok and not audit.blocking
    assert audit.issues() == ()


def test_trailing_separator_mismatch_is_reported():
    conflicts = audit_path_types(
        r"1. Refactor H:\MSB\scripts\factor_gen\demo_cancel_microfactor\.",
        r"- Do not modify H:\MSB\scripts\factor_gen\demo_cancel_microfactor.",
    )

    assert conflicts and "trailing separator" in conflicts[0]


def test_trailing_separator_conflict_is_reported_for_relative_paths():
    conflicts = audit_path_types(
        "1. Refactor the code in scripts/gen/.",
        "- Do not modify scripts/gen.",
    )

    assert conflicts and "trailing separator" in conflicts[0]


def test_analyzer_raises_contradiction_as_error():
    analysis = ProtocolAnalyzer().analyze_text(
        "## INPUT\n\nExisting scripts.\n\n"
        f"## TARGET\n\n{FACCREATE_TARGET}\n\n"
        f"## RESTRICTIONS\n\n{FACCREATE_RESTRICTIONS}\n",
    )

    blocking = [
        issue for issue in analysis.issues
        if issue.severity.value == "error" and "cannot be satisfied" in issue.message
    ]
    assert blocking, analysis.to_markdown()


def test_wizard_never_returns_a_contradictory_draft():
    contradictory = (
        "## INPUT\n\nExisting scripts.\n\n"
        f"## TARGET\n\n{FACCREATE_TARGET}\n\n"
        f"## RESTRICTIONS\n\n{FACCREATE_RESTRICTIONS}\n"
    )
    repaired = (
        "## INPUT\n\nExisting scripts.\n\n"
        "## TARGET\n\n1. Create faccreate/orderfacwavexx.m.\n"
        "2. Done when pytest passes.\n\n"
        f"## RESTRICTIONS\n\n{FACCREATE_RESTRICTIONS}\n"
    )
    wizard = ScriptedWizard([contradictory, repaired])

    _md, protocol = wizard.refine("in", "tgt", "res")

    assert "verify_refactor.m" not in protocol.target_section
    assert len(wizard.prompts) == 2


def test_wizard_raises_when_contradiction_survives_every_round():
    contradictory = (
        "## INPUT\n\nExisting scripts.\n\n"
        f"## TARGET\n\n{FACCREATE_TARGET}\n\n"
        f"## RESTRICTIONS\n\n{FACCREATE_RESTRICTIONS}\n"
    )
    wizard = ScriptedWizard([contradictory] + [contradictory] * 3)

    try:
        wizard.refine("in", "tgt", "res")
    except ProtocolGateError as exc:
        assert any("cannot be satisfied" in issue for issue in exc.issues)
    else:  # pragma: no cover - the gate must never let this through
        raise AssertionError("contradictory protocol was returned")


def test_batch_launch_refuses_a_contradictory_protocol(tmp_path):
    import pytest

    from services.webui.batch_launch import refuse_self_contradictory_protocol

    protocol = tmp_path / "protocol.md"
    protocol.write_text(
        "## INPUT\n\nExisting scripts.\n\n"
        f"## TARGET\n\n{FACCREATE_TARGET}\n\n"
        f"## RESTRICTIONS\n\n{FACCREATE_RESTRICTIONS}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as excinfo:
        refuse_self_contradictory_protocol(protocol)
    assert "can never be satisfied" in str(excinfo.value)


def test_batch_launch_accepts_a_consistent_protocol(tmp_path):
    from services.webui.batch_launch import refuse_self_contradictory_protocol

    protocol = tmp_path / "protocol.md"
    protocol.write_text(
        "## INPUT\n\nExisting scripts.\n\n"
        "## TARGET\n\n1. Create faccreate/orderfacwavexx.m.\n"
        "2. Done when pytest passes.\n\n"
        f"## RESTRICTIONS\n\n{FACCREATE_RESTRICTIONS}\n",
        encoding="utf-8",
    )

    refuse_self_contradictory_protocol(protocol)
