"""Gate rubric trust layer: the go/no-go rejection demonstrations.

Ontology-×-LLM discipline (ch. 8): each test demonstrates one gate catching
one failure class — fabricated entities, unevidenced PASS claims, DONE over
measured FAIL/ERROR ("no record ≠ pass") — plus the PROV harvest and the
guarded blocked-transition that never auto-applies.
"""

from pathlib import Path

from supervisor.protocols.feasibility_gate import gates_to_facts, parse_gates
from supervisor.protocols.gate_rubric import (
    apply_harvest,
    build_harvest,
    evaluate_rubric,
    render_scorecard,
    validate_refs,
    verdict_violations,
)


class _Result:
    """Minimal stand-in for CriterionResult."""

    def __init__(self, criterion: str, met: bool, evidence: str = ""):
        self.criterion = criterion
        self.met = met
        self.evidence = evidence


def _rubric(tmp_path: Path, gates: list[dict]) -> tuple:
    facts = gates_to_facts(gates)
    assert facts is not None
    parsed = parse_gates(facts)
    assert parsed
    return parsed, evaluate_rubric(parsed, tmp_path)


# --------------------------------------------------------------------------- #
# Layer 2 — deterministic evaluation                                          #
# --------------------------------------------------------------------------- #


def test_command_gate_pass_and_fail(tmp_path):
    (tmp_path / "pass.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "fail.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    gates, evaluations = _rubric(
        tmp_path,
        [
            {"name": "cmd_pass", "check": {
                "kind": "command", "cmd": "python pass.py",
            }},
            {"name": "cmd_fail", "check": {
                "kind": "command", "cmd": "python fail.py",
            }},
        ],
    )
    assert evaluations["cmd_pass"].outcome == "PASS"
    assert evaluations["cmd_fail"].outcome == "FAIL"
    # PROV tuple: conclusion + fact + source + timestamp
    assert evaluations["cmd_fail"].fact.startswith("exit=3")
    assert evaluations["cmd_fail"].source == "python fail.py"
    assert evaluations["cmd_fail"].timestamp > 0


def test_metric_gate_and_no_record_discipline(tmp_path):
    (tmp_path / "status.json").write_text(
        '{"best": {"shrp": 3.4}, "notes": "x"}', encoding="utf-8",
    )
    gates, evaluations = _rubric(
        tmp_path,
        [
            {"name": "shrp_ok", "check": {
                "kind": "metric", "metric": "page.shrp", "op": ">",
                "value": 3.0, "source": "status.json:best.shrp",
            }},
            {"name": "shrp_missing", "check": {
                "kind": "metric", "metric": "page.shrp", "op": ">",
                "value": 3.0, "source": "status.json:nope.shrp",
            }},
            {"name": "file_missing", "check": {
                "kind": "metric", "metric": "page.shrp", "op": ">",
                "value": 3.0, "source": "absent.json:best.shrp",
            }},
        ],
    )
    assert evaluations["shrp_ok"].outcome == "PASS"
    assert "satisfies" in evaluations["shrp_ok"].fact
    # missing key / missing file -> ERROR ("no record"), never a guess
    assert evaluations["shrp_missing"].outcome == "ERROR"
    assert evaluations["shrp_missing"].fact.startswith("no record")
    assert evaluations["file_missing"].outcome == "ERROR"


def test_manual_gates_are_not_machine_evaluated(tmp_path):
    gates, evaluations = _rubric(
        tmp_path, [{"name": "quality", "definition": "code is clean"}],
    )
    assert evaluations == {}


# --------------------------------------------------------------------------- #
# Layer 1 — closed-vocabulary scorecard                                        #
# --------------------------------------------------------------------------- #


def test_scorecard_closed_vocabulary_and_determined_facts(tmp_path):
    gates, evaluations = _rubric(
        tmp_path,
        [
            {"name": "auto_gate", "check": {"kind": "manual_placeholder"}},
        ],
    )
    # kind that is neither command nor metric degrades to ERROR, so the gate
    # shows up as determined
    block = render_scorecard(gates, evaluations)
    assert "closed list" in block
    assert "do NOT re-judge" in block
    assert "Gates outside this list do not exist" in block


# --------------------------------------------------------------------------- #
# Layer 3 — exit validation (the rejection demonstrations)                     #
# --------------------------------------------------------------------------- #


def test_reject_fabricated_gate_name(tmp_path):
    gates, _ = _rubric(tmp_path, [{"name": "real_gate"}])
    violations = verdict_violations(
        [_Result("phantom_gate_xyz", met=True)], gates, {},
    )
    assert any("fabricated" in v for v in violations)


def test_reject_pass_claim_contradicting_measurement(tmp_path):
    gates, evaluations = _rubric(
        tmp_path,
        [{"name": "corr", "check": {
            "kind": "metric", "metric": "x", "op": ">", "value": 1.0,
            "source": "none.json:x",
        }}],
    )
    assert evaluations["corr"].outcome == "ERROR"  # no record
    violations = verdict_violations(
        [_Result("corr", met=True, evidence="judge says so")],
        gates, evaluations,
    )
    assert any("claimed PASS but measured" in v for v in violations)


def test_reject_done_over_unadjudicated_machine_gate(tmp_path):
    gates, evaluations = _rubric(
        tmp_path,
        [{"name": "cmd", "check": {"kind": "command", "cmd": "python -c 0"}}],
    )
    violations = verdict_violations(
        [_Result("something else entirely", met=True)], gates, evaluations,
    )
    assert any("not adjudicated" in v for v in violations)


def test_no_violations_for_clean_verdict(tmp_path):
    (tmp_path / "s.json").write_text('{"x": 2.5}', encoding="utf-8")
    gates, evaluations = _rubric(
        tmp_path,
        [
            {"name": "cmd", "check": {"kind": "command", "cmd": "python -c 0"}},
            {"name": "manual_gate"},
        ],
    )
    assert evaluations["cmd"].outcome == "PASS"
    assert verdict_violations(
        [_Result("cmd", met=True), _Result("manual_gate", met=True)],
        gates, evaluations,
    ) == []


# --------------------------------------------------------------------------- #
# PROV harvest + guarded transitions                                          #
# --------------------------------------------------------------------------- #


def _harvest_with(outcome: str, run: str = "r1") -> dict:
    return {
        "run_id": run,
        "protocol_revision": "abc123",
        "results": [
            {
                "gate": "g", "conclusion": outcome, "fact": "exit=0",
                "source": "python pass.py", "kind": "command",
                "timestamp": 1.0,
            },
        ],
    }


def test_harvest_pass_flips_passing_with_provenance():
    facts = {"gates": [{"name": "g", "status": "open"}]}
    updated = apply_harvest(dict(facts), _harvest_with("PASS"))
    gate = updated["gates"][0]
    assert gate["status"] == "passing"
    assert "run r1" in gate["evidence"] and "abc123" in gate["evidence"]


def test_harvest_fail_streak_proposes_but_never_applies_blocked():
    facts = {"gates": [{"name": "g", "status": "open"}]}
    for run in ("r1", "r2", "r3"):
        updated = apply_harvest(dict(facts), _harvest_with("FAIL", run))
        facts = updated
    gate = facts["gates"][0]
    # The irreversible transition is PROPOSED, not applied (operator confirm)
    assert gate["status"] == "open"
    assert gate["fail_streak"] == 3
    assert facts["pending_blocked"] == [
        {"gate": "g", "streak": 3, "evidence": "exit=0; python pass.py; run r3 rev abc123"},
    ]


def test_harvest_error_is_not_a_transition():
    facts = {"gates": [{"name": "g", "status": "open", "fail_streak": 2}]}
    updated = apply_harvest(dict(facts), _harvest_with("ERROR"))
    assert updated["gates"][0]["status"] == "open"
    assert updated["gates"][0]["fail_streak"] == 2
    assert updated.get("pending_blocked") == []


def test_harvest_pass_clears_stale_pending_proposal():
    facts = {
        "gates": [{"name": "g", "status": "open"}],
        "pending_blocked": [{"gate": "g", "streak": 3, "evidence": "old"}],
    }
    updated = apply_harvest(dict(facts), _harvest_with("PASS"))
    assert updated["gates"][0]["status"] == "passing"
    assert updated.get("pending_blocked") == []


def test_build_harvest_records_manual_results_with_provenance(tmp_path):
    gates, _ = _rubric(tmp_path, [{"name": "manual_gate"}])
    harvest = build_harvest(
        gates, {}, {"manual_gate": (False, "unknown — no artifact")},
        run_id="r9", protocol_text="## TARGET\n1. x",
    )
    entry = harvest["results"][0]
    assert entry["conclusion"] == "UNMET"
    assert entry["fact"] == "unknown — no artifact"
    assert entry["source"] == "judge verdict"
    assert harvest["protocol_revision"]


# --------------------------------------------------------------------------- #
# Drift guard                                                                  #
# --------------------------------------------------------------------------- #


def test_validate_refs_flags_out_of_range_and_duplicates():
    gates, _ = _rubric(
        Path("."),
        [
            {"name": "a", "ref": 1},
            {"name": "b", "ref": 1},
            {"name": "c", "ref": 9},
        ],
    )
    problems = validate_refs(gates, 3)
    assert any("#9 outside" in p for p in problems)
    assert any("duplicate" in p for p in problems)
    assert validate_refs(gates[:1], 3) == []


# --------------------------------------------------------------------------- #
# Exit-lock seam: goal guard blocks DONE over measured facts                   #
# --------------------------------------------------------------------------- #


def test_goal_guard_blocks_done_over_failing_machine_gate(tmp_path):
    from supervisor.core.goal_guard import GoalGuard
    from supervisor.core.llm_support.models import SupervisorVerdict
    from supervisor.protocols.protocol import parse_protocol_text
    from supervisor.utils.config import SupervisorConfig

    (tmp_path / "fail.py").write_text("raise SystemExit(2)\n", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "feasibility_facts.json").write_text(
        '{"gates": [{"name": "must_pass", "status": "open", '
        '"check": {"kind": "command", "cmd": "python fail.py"}}]}',
        encoding="utf-8",
    )
    protocol = parse_protocol_text(
        "## INPUT\nx\n## TARGET\n1. must_pass\n## RESTRICTIONS\nnone\n",
    )
    guard = GoalGuard(
        SupervisorConfig(
            protocol_path=tmp_path / "protocol.md", workspace=tmp_path,
        ),
        protocol,
    )
    verdict = SupervisorVerdict(
        raw="DONE: yes", all_targets_met=True, feedback="",
        criteria_results=[_Result("must_pass", met=True, evidence="trust me")],
    )

    decision = guard.evaluate_done(verdict, changed_files=["some_file.py"])

    assert decision.accepted is False
    assert "gate rubric exit check" in decision.reason
    assert "FAIL" in decision.reason


def test_goal_guard_blocks_done_over_no_record_gate(tmp_path):
    from supervisor.core.goal_guard import GoalGuard
    from supervisor.core.llm_support.models import SupervisorVerdict
    from supervisor.protocols.protocol import parse_protocol_text
    from supervisor.utils.config import SupervisorConfig

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "feasibility_facts.json").write_text(
        '{"gates": [{"name": "metric_gate", "status": "open", '
        '"check": {"kind": "metric", "metric": "x", "op": ">", "value": 1, '
        '"source": "absent.json:x"}}]}',
        encoding="utf-8",
    )
    protocol = parse_protocol_text(
        "## INPUT\nx\n## TARGET\n1. metric_gate\n## RESTRICTIONS\nnone\n",
    )
    guard = GoalGuard(
        SupervisorConfig(
            protocol_path=tmp_path / "protocol.md", workspace=tmp_path,
        ),
        protocol,
    )
    verdict = SupervisorVerdict(
        raw="DONE: yes", all_targets_met=True, feedback="",
        criteria_results=[_Result("metric_gate", met=True)],
    )

    decision = guard.evaluate_done(verdict, changed_files=["some_file.py"])

    # "no record ≠ pass": ERROR blocks DONE exactly like FAIL
    assert decision.accepted is False
    assert "ERROR" in decision.reason
