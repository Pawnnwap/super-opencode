from supervisor.protocols.protocol_analyzer import ProtocolAnalyzer
from supervisor.protocols.target_audit import audit_target


def test_target_audit_rejects_unmeasured_improvement():
    audit = audit_target("Improve memory retrieval quality.", "Do not edit files outside supervisor/.")

    assert audit.rejects_target
    assert not audit.is_actionable
    assert any("rejected" in issue for issue in audit.issues)


def test_target_audit_accepts_tested_deliverable():
    audit = audit_target(
        "1. Add target_state.py. 2. Add tests and require pytest to pass before completion.",
        "- Do not introduce test regressions.",
    )

    assert audit.is_actionable
    assert audit.has_deliverable
    assert audit.has_acceptance_evidence
    assert audit.has_completion_state
    assert audit.has_failure_condition


def test_protocol_analysis_surfaces_rejected_target_as_error():
    analysis = ProtocolAnalyzer().analyze_text(
        "## INPUT\n\nExisting service.\n\n"
        "## TARGET\n\nImprove reliability.\n\n"
        "## RESTRICTIONS\n\n- Do not edit files outside supervisor/.\n",
    )

    assert any(
        issue.section == "TARGET" and issue.severity.value == "error"
        for issue in analysis.issues
    )
