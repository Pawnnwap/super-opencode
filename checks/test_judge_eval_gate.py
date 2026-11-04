"""Judge-pipeline evaluation gate — the go/no-go fixture for judge changes.

Purpose (Engineering Ontology ch. 8.7): the deterministic code has unit
tests, but the *exit-validation pipeline* that contains the judge (scorecard
injection, machine-gate measurement, verdict cross-checks, DONE gating) is
the hallucination firewall of this system. Any change to it — prompt wording,
model swap, verdict parsing, gate rubric — must prove on THIS suite that the
three sample classes behave as required before it may ship:

  POSITIVE   verdicts consistent with the measured evidence MUST be accepted.
  NO-RECORD  missing evidence claimed as a pass MUST block ("no record ≠ pass").
  BOUNDARY   hallucination shapes (fabricated gates, PASS contradicting the
             measurement, unadjudicated machine gates, weakened tests,
             agent-asserted closures) MUST be caught or labelled.

Go/no-go rule: the two safety classes (NO-RECORD, BOUNDARY) require 100% on
this suite — one leak is a regression, not a statistic. POSITIVE samples may
not regress either (a firewall that blocks everything is useless). When a new
judge behavior is observed in production, add its minimized form here before
changing any prompt or parser.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from supervisor.core.llm_support.models import CriterionResult, SupervisorVerdict
from supervisor.utils.config import SupervisorConfig

_PROTOCOL_MD = "## INPUT\nx\n## TARGET\n1. must_pass\n## RESTRICTIONS\nnone\n"


class _Result(CriterionResult):
    pass


def _write_facts(tmp_path: Path, body: dict) -> None:
    """Facts at the stable location the loop reads (workspace root)."""
    from supervisor.protocols.feasibility_gate import default_facts_path

    default_facts_path(tmp_path).write_text(
        json.dumps(body), encoding="utf-8",
    )


def _guard(tmp_path: Path, **config_overrides):
    from supervisor.core.goal_guard import GoalGuard
    from supervisor.protocols.protocol import parse_protocol_text

    (tmp_path / "protocol.md").write_text(_PROTOCOL_MD, encoding="utf-8")
    protocol = parse_protocol_text(_PROTOCOL_MD)
    return GoalGuard(
        SupervisorConfig(
            protocol_path=tmp_path / "protocol.md", workspace=tmp_path,
            **config_overrides,
        ),
        protocol,
    )


def _done_verdict(*results, raw="DONE: yes", met=True):
    return SupervisorVerdict(
        raw=raw,
        all_targets_met=met,
        feedback="",
        criteria_results=list(results),
    )


def _audit_lines(tmp_path: Path) -> list[dict]:
    path = tmp_path / ".opencode" / "goal_audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# =========================================================================== #
# CLASS 1 — POSITIVE: honest verdicts pass                                     #
# =========================================================================== #


def test_positive_verdict_matching_measurement_is_accepted(tmp_path):
    (tmp_path / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    _write_facts(tmp_path, {
        "gates": [{
            "name": "must_pass",
            "status": "open",
            "check": {"kind": "command", "cmd": "python ok.py"},
        }],
    })
    guard = _guard(tmp_path)
    (tmp_path / "deliverable.py").write_text("x = 1\n", encoding="utf-8")

    decision = guard.evaluate_done(
        _done_verdict(_Result("must_pass", met=True, evidence="exit=0")),
        changed_files=["deliverable.py"],
    )

    assert decision.accepted is True
    assert decision.unverified is False
    assert "all criteria met" in decision.reason


def test_positive_audit_carries_proposal_and_evidence_snapshot(tmp_path):
    """8.5.4: the audit must contain the full proposal + evidence hashes."""
    (tmp_path / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "deliverable.py").write_text("x = 1\n", encoding="utf-8")
    _write_facts(tmp_path, {
        "gates": [{
            "name": "must_pass",
            "status": "open",
            "check": {"kind": "command", "cmd": "python ok.py"},
        }],
    })
    guard = _guard(tmp_path)

    guard.evaluate_done(
        _done_verdict(
            _Result("must_pass", met=True, evidence="exit=0"),
            raw="DONE: yes\nNEXT_ACTION: none",
        ),
        changed_files=["deliverable.py"],
    )

    accepted = [r for r in _audit_lines(tmp_path) if r["event"] == "done_accepted"]
    assert accepted, "acceptance must be audited"
    record = accepted[-1]
    assert record["proposal"]["all_targets_met"] is True
    assert record["proposal"]["criteria"][0]["criterion"] == "must_pass"
    assert record["proposal"]["criteria"][0]["met"] is True
    # Evidence snapshot: content hash of the deliverable at decision time.
    assert record["evidence_snapshot"]["deliverable.py"]
    assert len(record["evidence_snapshot"]["deliverable.py"]) == 16


# =========================================================================== #
# CLASS 2 — NO-RECORD: missing evidence claimed as a pass must block           #
# =========================================================================== #


def test_no_record_gate_claims_must_block_done(tmp_path):
    _write_facts(tmp_path, {
        "gates": [{
            "name": "must_pass",
            "status": "open",
            "check": {"kind": "metric", "metric": "x", "op": ">",
                      "value": 1, "source": "absent.json:x"},
        }],
    })
    guard = _guard(tmp_path)
    (tmp_path / "deliverable.py").write_text("x = 1\n", encoding="utf-8")

    decision = guard.evaluate_done(
        _done_verdict(_Result("must_pass", met=True, evidence="probably fine")),
        changed_files=["deliverable.py"],
    )

    assert decision.accepted is False
    assert "ERROR" in decision.reason


def test_no_record_test_gate_crash_blocks_done(tmp_path):
    """P1-1 regression: a crashed test gate is 'no record', never a pass.

    The old code returned ok=True from the except branch — a workspace whose
    gate could not even run passed DONE by breaking its own gate.
    """
    import supervisor.runners.test_runner as tr

    (tmp_path / "deliverable.py").write_text("x = 1\n", encoding="utf-8")
    guard = _guard(tmp_path, goal_verify_tests=True)

    class _CrashingRunner:
        def __init__(self, workspace):
            raise RuntimeError("collection exploded")

    original = tr.OcTestRunner
    tr.OcTestRunner = _CrashingRunner
    try:
        decision = guard.evaluate_done(
            _done_verdict(_Result("must_pass", met=True, evidence="ok")),
            changed_files=["deliverable.py"],
        )
    finally:
        tr.OcTestRunner = original

    assert decision.accepted is False
    assert "test gate crashed" in decision.reason
    assert "no record" in decision.reason


# =========================================================================== #
# CLASS 3 — BOUNDARY: hallucination shapes                                     #
# =========================================================================== #


def test_boundary_fabricated_gate_is_flagged(tmp_path):
    from supervisor.protocols.feasibility_gate import parse_gates
    from supervisor.protocols.gate_rubric import verdict_violations

    facts = {
        "gates": [{
            "name": "must_pass",
            "status": "open",
            "check": {"kind": "command", "cmd": "python ok.py"},
        }],
    }
    gates = parse_gates(facts)
    # The judge invents a gate the rubric never defined.
    verdict = _done_verdict(
        _Result("imaginary_gate", met=True, evidence="made up"),
    )
    violations = verdict_violations(verdict.criteria_results, gates, {})
    assert any("fabricated" in v for v in violations)


def test_boundary_claimed_pass_contradicting_measurement(tmp_path):
    from supervisor.protocols.feasibility_gate import parse_gates
    from supervisor.protocols.gate_rubric import evaluate_rubric, verdict_violations

    (tmp_path / "fail.py").write_text("raise SystemExit(2)\n", encoding="utf-8")
    facts = {
        "gates": [{
            "name": "must_pass",
            "status": "open",
            "check": {"kind": "command", "cmd": "python fail.py"},
        }],
    }
    gates = parse_gates(facts)
    evaluations = evaluate_rubric(gates, tmp_path)
    assert evaluations["must_pass"].outcome == "FAIL"

    verdict = _done_verdict(_Result("must_pass", met=True, evidence="it passed"))
    violations = verdict_violations(verdict.criteria_results, gates, evaluations)
    assert any("claimed PASS but measured FAIL" in v for v in violations)


def test_boundary_unadjudicated_machine_gate(tmp_path):
    from supervisor.protocols.feasibility_gate import parse_gates
    from supervisor.protocols.gate_rubric import evaluate_rubric, verdict_violations

    (tmp_path / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    facts = {
        "gates": [{
            "name": "must_pass",
            "status": "open",
            "check": {"kind": "command", "cmd": "python ok.py"},
        }],
    }
    gates = parse_gates(facts)
    evaluations = evaluate_rubric(gates, tmp_path)

    # The judge answers DONE without adjudicating the machine gate at all.
    verdict = _done_verdict(_Result("something else entirely", met=True))
    violations = verdict_violations(verdict.criteria_results, gates, evaluations)
    assert any("not adjudicated" in v for v in violations)


def test_boundary_missing_evidence_file_is_flagged(tmp_path):
    """A MET criterion citing a deliverable that does not exist is an entity
    error: the regex fires only on path-like tokens (at least one separator),
    so prose such as 'e.g.' is never mistaken for a file."""
    from supervisor.protocols.feasibility_gate import parse_gates
    from supervisor.protocols.gate_rubric import verdict_violations

    gates = parse_gates({"gates": [{"name": "must_pass", "status": "open"}]})
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "present.py").write_text("x = 1\n", encoding="utf-8")

    missing = verdict_violations(
        [_Result("must_pass", met=True, evidence="see artifacts/missing.py")],
        gates,
        {},
        tmp_path,
    )
    assert any("missing file" in v for v in missing)

    present = verdict_violations(
        [_Result("must_pass", met=True, evidence="see artifacts/present.py")],
        gates,
        {},
        tmp_path,
    )
    assert not any("missing file" in v for v in present)

    prose = verdict_violations(
        [_Result("must_pass", met=True, evidence="e.g. the run is fine, i.e. done")],
        gates,
        {},
        tmp_path,
    )
    assert not any("missing file" in v for v in prose)


def test_boundary_weakened_test_file_is_labelled(tmp_path):
    """Editing a verification file on the DONE turn cannot be silently
    accepted: the accept is kept (the operator decides) but the reason must
    carry the red-flag note."""
    (tmp_path / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    _write_facts(tmp_path, {
        "gates": [{
            "name": "must_pass",
            "status": "open",
            "check": {"kind": "command", "cmd": "python ok.py"},
        }],
    })
    (tmp_path / "checks").mkdir()
    (tmp_path / "checks" / "test_thing.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8",
    )
    guard = _guard(tmp_path)

    decision = guard.evaluate_done(
        _done_verdict(_Result("must_pass", met=True, evidence="exit=0")),
        changed_files=["checks/test_thing.py"],
    )

    assert decision.accepted is True
    assert "verification files were modified" in decision.reason


def test_boundary_unverified_cap_accept_is_labelled(tmp_path):
    """P1-2 regression: the relief valve still fires (by design) but every
    surface must carry the PENDING REVIEW / unverified label, and the audit
    must record the verdict's own claim, not the forced acceptance."""
    guard = _guard(tmp_path, max_blocked_stops=0)

    decision = guard.evaluate_done(
        _done_verdict(_Result("must_pass", met=False, evidence="none")),
        changed_files=["deliverable.py"],
    )

    # The watchdog design is preserved: accept rather than loop forever.
    assert decision.accepted is True
    assert decision.unverified is True
    assert decision.reason.startswith("accepted after")
    assert "PENDING REVIEW" in decision.reason
    # The loop reads this to label the terminal event.
    assert guard.last_done_decision.unverified is True
    # Audit honesty: unverified flag present, all_targets_met reflects the
    # verdict's own (contradictory) claim, not a whitewashed True.
    record = [
        r for r in _audit_lines(tmp_path)
        if r["event"] == "done_accepted_unverified"
    ][-1]
    assert record["unverified"] is True
    assert record["pending_review"] is True
    assert record["all_targets_met"] is True  # verdict claimed DONE...
    assert record["unmet_criteria"] == ["must_pass"]  # ...while criteria said no


def test_boundary_agent_closure_contradicted_by_facts_stays_open(tmp_path):
    """P1-3 regression: the agent's own terminal assertion may end the run
    (BLOCKED, no more turns bought), but when recorded feasibility facts
    still say the target is reachable it must NOT write the irreversible
    closed status — that write is reserved for corroborated evidence."""
    from supervisor.core.loop_base import BaseLoop
    from supervisor.core.target_state import (
        initialise_target_state,
        load_target_state,
    )

    initialise_target_state(tmp_path, target="Reach 9 gates.")
    _write_facts(tmp_path, {
        "gates": [
            {"name": "g1", "status": "open"},
            {"name": "g2", "status": "passing"},
        ],
    })
    audit = tmp_path / ".opencode" / "goal_audit.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(
        json.dumps({
            "event": "goal_terminal",
            "reason": "agent says it is done pursuing",
        }) + "\n",
        encoding="utf-8",
    )

    protocol = tmp_path / "protocol.md"
    protocol.write_text("# PROTOCOL\n\n## TARGET\n\nReach 9 gates.\n", encoding="utf-8")
    loop = BaseLoop(SupervisorConfig(workspace=tmp_path, protocol_path=protocol))
    loop._feasibility_block_reason = lambda: ""
    loop._blocked_agent_asserted = True
    loop.supervisor = SimpleNamespace(report_final_status=lambda **_: "report")
    loop._write = lambda *a, **k: None
    loop._record_structured_failure = lambda *a, **k: None
    loop._memory_goal = lambda: ""

    list(loop._on_blocked(SimpleNamespace(all_targets_met=False, unmet_criteria=[]), "agent closure"))

    # Facts say reachable (no blocked gates) → the state must stay resumable.
    assert load_target_state(tmp_path).status != "closed"


def test_boundary_agent_closure_with_no_contradicting_facts_closes(tmp_path):
    """Without contradicting evidence, the assertion stands and closes."""
    from supervisor.core.loop_base import BaseLoop
    from supervisor.core.target_state import (
        initialise_target_state,
        load_target_state,
    )

    initialise_target_state(tmp_path, target="Reach 9 gates.")
    protocol = tmp_path / "protocol.md"
    protocol.write_text("# PROTOCOL\n\n## TARGET\n\nReach 9 gates.\n", encoding="utf-8")
    loop = BaseLoop(SupervisorConfig(workspace=tmp_path, protocol_path=protocol))
    loop._feasibility_block_reason = lambda: ""
    loop._blocked_agent_asserted = True
    loop.supervisor = SimpleNamespace(report_final_status=lambda **_: "report")
    loop._write = lambda *a, **k: None
    loop._record_structured_failure = lambda *a, **k: None
    loop._memory_goal = lambda: ""

    list(loop._on_blocked(SimpleNamespace(all_targets_met=False, unmet_criteria=[]), "agent closure"))

    assert load_target_state(tmp_path).status == "closed"


# =========================================================================== #
# P2-2 regression: terminal detection reads the WHOLE audit journal           #
# =========================================================================== #


def test_terminal_assertion_survives_a_large_audit_journal(tmp_path):
    """A terminal event older than the old 2 MB tail window must still fire —
    long studies are exactly where the infinite-relaunch bug is expensive."""
    from supervisor.core.target_state import read_terminal_assertion

    audit = tmp_path / ".opencode" / "goal_audit.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    terminal = json.dumps({
        "event": "goal_terminal",
        "reason": "certified ceiling",
    })
    filler = json.dumps({"event": "turn", "reason": "x" * 300})
    with audit.open("w", encoding="utf-8") as handle:
        handle.write(terminal + "\n")
        # >2 MB of later, non-terminal events would push it out of the tail.
        for _ in range(8000):
            handle.write(filler + "\n")

    assertion = read_terminal_assertion(tmp_path)
    assert "goal_terminal" in assertion


def test_last_terminal_event_wins_across_the_whole_file(tmp_path):
    from supervisor.core.target_state import read_terminal_assertion

    audit = tmp_path / ".opencode" / "goal_audit.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "goal_terminal", "reason": "old"}) + "\n")
        handle.write(json.dumps({"event": "run_closed_by_agent", "reason": "new"}) + "\n")
        handle.write(json.dumps({"event": "turn", "reason": "noise"}) + "\n")

    assertion = read_terminal_assertion(tmp_path)
    assert "run_closed_by_agent" in assertion
    assert "old" not in assertion


# =========================================================================== #
# Go/no-go aggregate: the safety classes must be leak-free in one run         #
# =========================================================================== #


@pytest.mark.parametrize(
    "module_name",
    [
        "supervisor.protocols.gate_rubric",
        "supervisor.core.goal_guard",
        "supervisor.core.goal_progress",
        "supervisor.core.target_state",
        "supervisor.core.llm_support.models",
    ],
)
def test_pipeline_modules_import_clean(module_name):
    """The pipeline modules this gate certifies are importable as a set."""
    __import__(module_name, fromlist=["*"])
