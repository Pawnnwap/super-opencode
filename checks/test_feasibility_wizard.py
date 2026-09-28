"""Feasibility gate-list model: parser, derivation, rows, evidence.

The LLM call itself is exercised only live; everything deterministic around
it (fence-tolerant JSON recovery, gate-list verdict derivation, row
conversion, bounded evidence excerpts) is covered here.
"""

import json

from supervisor.protocols.feasibility_gate import (
    check_feasibility,
    gates_to_facts,
    parse_gates,
    parse_llm_facts,
    render_feasibility_block,
)
from supervisor.protocols.feasibility_wizard import (
    collect_evidence,
    count_target_gates,
    facts_to_gate_rows,
    legacy_facts_to_gate_rows,
    workspace_sources,
)


def test_count_target_gates_counts_numbered_items():
    protocol = (
        "## INPUT\n- data.csv\n\n"
        "## TARGET\n1. first gate\n2. second gate\n10. tenth gate\n\n"
        "## RESTRICTIONS\n- none\n"
    )
    assert count_target_gates(protocol) == 3


def test_count_target_gates_missing_section():
    assert count_target_gates("## INPUT\nnothing") is None
    assert count_target_gates("") is None


def test_workspace_sources_reports_protocol_and_artifacts(tmp_path):
    # empty workspace: no protocol, no artifacts
    empty = workspace_sources(tmp_path)
    assert empty == {
        "protocol_found": False,
        "target_gates": None,
        "artifacts": [],
    }

    (tmp_path / "protocol.md").write_text(
        "## TARGET\n1. one\n2. two\n", encoding="utf-8",
    )
    (tmp_path / "status.json").write_text("{}", encoding="utf-8")
    loaded = workspace_sources(tmp_path)
    assert loaded["protocol_found"] is True
    assert loaded["target_gates"] == 2
    assert loaded["artifacts"] == ["status.json"]


def test_parse_llm_facts_plain_json():
    assert parse_llm_facts('{"required_gates": 10}') == {"required_gates": 10}


def test_parse_llm_facts_fenced_and_wrapped():
    reply = (
        "Here is the draft:\n"
        "```json\n"
        '{"required_gates": 3, "known_ceiling": {"count": 2, "of": 3}}\n'
        "```\n"
        "Let me know if you want changes."
    )
    facts = parse_llm_facts(reply)
    assert facts["required_gates"] == 3
    assert facts["known_ceiling"]["count"] == 2


def test_parse_llm_facts_non_object_json_raises():
    # a JSON array is not a facts object — surface it as a draft failure
    try:
        parse_llm_facts("[1, 2, 3]")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a non-object reply")


def test_parse_llm_facts_garbage_raises():
    for garbage in ("", "no braces at all", "{broken"):
        try:
            parse_llm_facts(garbage)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {garbage!r}")


def test_collect_evidence_gathers_bounded_tails(tmp_path):
    (tmp_path / "status.json").write_text(
        json.dumps({"attempts": 15, "best": "8/10 gates"}), encoding="utf-8",
    )
    long_report = "R" * 5000
    (tmp_path / "REPORT.md").write_text(long_report, encoding="utf-8")

    evidence = collect_evidence(tmp_path)

    assert "--- status.json ---" in evidence
    assert "8/10 gates" in evidence
    assert "--- REPORT.md ---" in evidence
    # per-file cap keeps the total bounded even with big artifacts
    assert len(evidence) < 6000 + len("--- REPORT.md ---\n") * 2 + 100


def test_collect_evidence_empty_workspace(tmp_path):
    assert collect_evidence(tmp_path) == ""


def test_facts_to_gate_rows_from_gates_draft():
    rows = facts_to_gate_rows(
        {
            "gates": [
                {"name": "ind_a", "definition": "page.shrp > 2.0"},
                {"name": "corr", "status": "blocked", "evidence": "status.json"},
                {"name": "", "status": "blocked"},
                {"name": "mono", "status": "nonsense"},
            ],
        }
    )
    assert rows == [
        {
            "name": "ind_a", "definition": "page.shrp > 2.0", "status": "open",
            "evidence": "", "ref": None, "check": None,
        },
        {
            "name": "corr", "definition": "", "status": "blocked",
            "evidence": "status.json", "ref": None, "check": None,
        },
        {
            "name": "mono", "definition": "", "status": "open",
            "evidence": "", "ref": None, "check": None,
        },
    ]


def test_facts_to_gate_rows_converts_legacy_draft():
    rows = facts_to_gate_rows(
        {
            "required_gates": 2,
            "gate_thresholds": {"ind_a": "page.shrp > 2.0"},
            "known_ceiling": {
                "blocked_gates": ["corr"],
                "evidence": "12 attempts, corr never passed",
            },
        }
    )
    assert rows == [
        {"name": "ind_a", "definition": "page.shrp > 2.0", "status": "open", "evidence": ""},
        {"name": "corr", "definition": "", "status": "blocked", "evidence": "12 attempts, corr never passed"},
    ]


def test_legacy_facts_to_gate_rows_garbage():
    assert legacy_facts_to_gate_rows("junk") == []
    assert legacy_facts_to_gate_rows({}) == []


def test_gates_to_facts_roundtrip_and_empty():
    rows = [
        {"name": "a", "definition": "x", "status": "passing", "evidence": "e"},
        {"name": " ", "status": "blocked"},
    ]
    facts = gates_to_facts(rows, "note")
    assert facts == {
        "gates": [
            {"name": "a", "definition": "x", "status": "passing", "evidence": "e"},
        ],
        "notes": "note",
    }
    assert gates_to_facts([{"name": ""}]) is None
    assert gates_to_facts([]) is None
    assert parse_gates(facts)[0].name == "a"


def test_check_feasibility_gates_blocked_is_unreachable():
    facts = gates_to_facts(
        [
            {"name": "ind_a", "definition": "shrp>2", "status": "passing"},
            {"name": "corr", "definition": "band", "status": "blocked",
             "evidence": "never passed in 12 attempts"},
        ],
    )
    assessment = check_feasibility(facts)
    assert assessment.reachable is False
    assert assessment.required_gates == 2
    assert assessment.known_ceiling == 1
    assert assessment.blocked_gates == ("corr",)
    assert "corr" in assessment.reason
    block = render_feasibility_block(assessment)
    assert "gate corr [✗ blocked]: band — never passed in 12 attempts" in block
    assert "gate ind_a [✓ passing]: shrp>2" in block


def test_check_feasibility_gates_all_open_is_reachable():
    facts = gates_to_facts([{"name": "a"}, {"name": "b", "status": "passing"}])
    assessment = check_feasibility(facts)
    assert assessment.reachable is True
    assert assessment.blocked_gates == ()
    assert "1 of 2 gate(s) passed" in assessment.reason


def test_check_feasibility_legacy_counts_still_supported():
    assessment = check_feasibility(
        {"required_gates": 10, "known_ceiling": {"count": 8, "of": 10}},
    )
    assert assessment.reachable is False
    assert assessment.known_ceiling == 8
    assert assessment.gates == ()
