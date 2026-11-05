"""BLOCKED exit: a demonstrably unreachable target closes the run, no loop.

Regression cover for the 2026-10-30 batch study_20261030_194414_adad: four runs
sat 28h+ re-judging a target whose gates peak at 7/10, because termination
required all_targets_met and every escape hatch sat behind that same flag.
"""
import json
from pathlib import Path
from types import SimpleNamespace

from services.jobs.job_manager import JobManager, final_state_from_logs
from supervisor.core.loop_base import BaseLoop
from supervisor.utils.config import SupervisorConfig

_REPO = Path(__file__).resolve().parents[1]


def _loop(tmp_path, **overrides):
    protocol = tmp_path / "protocol.md"
    protocol.write_text(
        "# PROTOCOL\n\n## TARGET\n\nReach 9 AB gates.\n", encoding="utf-8",
    )
    config = SupervisorConfig(
        workspace=tmp_path, protocol_path=protocol, **overrides,
    )
    loop = BaseLoop(config)
    # No feasibility facts in these fixtures, so the deterministic trigger is
    # inert and each test exercises one path. The gate-count reader stays real:
    # it reads <workspace>/gate_results_last.json, absent unless written.
    loop._feasibility_block_reason = lambda: ""
    return loop


def _verdict(*unmet, met=False):
    return SimpleNamespace(all_targets_met=met, unmet_criteria=list(unmet))


def _harvest(tmp_path, passing):
    rows = [{"gate": f"g{i}", "conclusion": "PASS"} for i in range(passing)]
    rows += [
        {"gate": f"g{i}", "conclusion": "UNMET"} for i in range(passing, 10)
    ]
    (tmp_path / "gate_results_last.json").write_text(
        json.dumps({"run_id": "test", "results": rows}), encoding="utf-8",
    )


def test_repeated_unmet_criteria_closes_the_run_as_blocked(tmp_path):
    loop = _loop(
        tmp_path,
        blocked_after_stagnant_verdicts=3,
        min_turns_before_blocked=1,
    )
    loop._task_turn = 5
    verdict = _verdict("T2 AB >= 9/10 gates")
    assert [loop._blocked_assessment(verdict) for _ in range(2)] == ["", ""]
    reason = loop._blocked_assessment(verdict)
    assert "3 consecutive judged turns" in reason
    assert "T2 AB >= 9/10 gates" in reason


def test_turn_floor_blocks_an_early_exit(tmp_path):
    loop = _loop(
        tmp_path,
        blocked_after_stagnant_verdicts=2,
        min_turns_before_blocked=12,
    )
    loop._task_turn = 3
    verdict = _verdict("T2 AB >= 9/10 gates")
    assert [loop._blocked_assessment(verdict) for _ in range(6)] == [""] * 6


def test_file_churn_does_not_reset_the_streak(tmp_path):
    """Cosmetic edits are exactly what the trigger must see through.

    The original rule required `not changed_files`, so an agent that rewrote
    summary.md or deleted __pycache__ every turn made the streak unreachable -
    measured as zero Blocked exits across 549 judged turns of two studies.
    """
    loop = _loop(
        tmp_path,
        blocked_after_stagnant_verdicts=3,
        min_turns_before_blocked=0,
    )
    loop._task_turn = 20
    verdict = _verdict("T2 AB >= 9/10 gates")
    loop._blocked_assessment(verdict)
    loop._blocked_assessment(verdict)
    loop._last_changed_files = ["summary.md", "tests/__pycache__"]
    reason = loop._blocked_assessment(verdict)
    assert "3 consecutive judged turns" in reason


def test_new_unmet_set_is_not_a_streak(tmp_path):
    loop = _loop(
        tmp_path,
        blocked_after_stagnant_verdicts=2,
        min_turns_before_blocked=0,
    )
    loop._task_turn = 20
    assert loop._blocked_assessment(_verdict("T2 gates")) == ""
    assert loop._blocked_assessment(_verdict("T3 tier")) == ""


def test_met_verdict_clears_the_streak(tmp_path):
    loop = _loop(
        tmp_path,
        blocked_after_stagnant_verdicts=3,
        min_turns_before_blocked=0,
    )
    loop._task_turn = 20
    verdict = _verdict("T2 AB >= 9/10 gates")
    loop._blocked_assessment(verdict)
    loop._blocked_assessment(verdict)
    assert loop._blocked_assessment(_verdict(met=True)) == ""
    assert loop._unmet_streak == 0


def test_blocked_exit_can_be_disabled(tmp_path):
    loop = _loop(
        tmp_path,
        blocked_after_stagnant_verdicts=2,
        min_turns_before_blocked=0,
        enable_blocked_exit=False,
    )
    loop._task_turn = 40
    verdict = _verdict("T2 AB >= 9/10 gates")
    assert [loop._blocked_assessment(verdict) for _ in range(5)] == [""] * 5


def test_unreachable_facts_block_without_repeating_the_judge(tmp_path):
    loop = _loop(
        tmp_path,
        blocked_after_unreachable_turns=2,
        min_turns_before_blocked=0,
    )
    loop._task_turn = 20
    loop._feasibility_block_reason = lambda: "known ceiling 7 of 10 gates"
    _harvest(tmp_path, passing=2)
    verdict = _verdict("T2 AB >= 9/10 gates")
    assert loop._blocked_assessment(verdict) == ""
    assert loop._blocked_assessment(verdict) == ""
    reason = loop._blocked_assessment(verdict)
    assert "known ceiling 7 of 10 gates" in reason
    assert "no passing-gate improvement" in reason


def test_won_gates_defer_the_feasibility_block(tmp_path):
    loop = _loop(
        tmp_path,
        blocked_after_unreachable_turns=1,
        min_turns_before_blocked=0,
    )
    loop._task_turn = 20
    loop._feasibility_block_reason = lambda: "known ceiling 7 of 10 gates"
    verdict = _verdict("T2 AB >= 9/10 gates")
    _harvest(tmp_path, passing=2)
    assert loop._blocked_assessment(verdict) == ""
    _harvest(tmp_path, passing=3)
    assert loop._blocked_assessment(verdict) == ""


def test_last_terminal_event_wins_in_the_job_store():
    assert final_state_from_logs(
        [{"level": "success"}, {"level": "error"}, {"level": "warn"}],
    ) == "FAILED"
    assert final_state_from_logs(
        [{"level": "warn"}, {"level": "blocked"}],
    ) == "BLOCKED"
    assert final_state_from_logs([{"level": "tool"}]) == "FAILED"
    assert "BLOCKED" in JobManager.TERMINAL_STATES


def test_board_and_pills_know_the_blocked_state():
    board = (_REPO / "services" / "webui" / "components" / "board.py").read_text(
        encoding="utf-8",
    )
    layout = (_REPO / "services" / "webui" / "layout.py").read_text(
        encoding="utf-8",
    )
    manager = (_REPO / "services" / "jobs" / "job_manager.py").read_text(
        encoding="utf-8",
    )
    assert '"BLOCKED"' in board
    assert "pill-blocked" in layout
    # The board metrics line counts blocked jobs instead of dropping them; the
    # keys now come from the shared vocabulary rather than a literal table.
    assert "ACTIVE_JOB_STATES | TERMINAL_JOB_STATES" in board
    assert '"BLOCKED"' in (_REPO / "services" / "jobs" / "job_states.py").read_text(
        encoding="utf-8",
    )
    assert "blocked" in board.split("metrics_label.set_text")[1]
    # A blocked run's report reaches the job store via blocked_report.md.
    assert "blocked_report.md" in manager


# --------------------------------------------------------------------------- #
# Attainment-based progress (2026-11-03 studies)                              #
# --------------------------------------------------------------------------- #

def _structured(*pairs):
    """Verdict whose criteria carry per-item MET/UNMET states."""
    from supervisor.core.llm_support.models import CriterionResult

    return SimpleNamespace(
        all_targets_met=False,
        criteria_results=[
            CriterionResult(criterion=text, met=met) for text, met in pairs
        ],
        unmet_criteria=[
            CriterionResult(criterion=text, met=False)
            for text, met in pairs
            if not met
        ],
    )


def test_reworded_criteria_do_not_reset_the_streak(tmp_path):
    """The judge rewords the same TARGET item every turn; that is not progress.

    Measured on one study: the judge restated the tier criterion as
    "T4 tier ...", "Target 4 (tier ...)", "Target 3 tier ..." across 110
    verdicts, so an exact-text signature never repeated.
    """
    loop = _loop(
        tmp_path,
        blocked_after_stagnant_verdicts=3,
        min_turns_before_blocked=0,
    )
    loop._task_turn = 20
    variants = [
        "T4 tier in {上座,好强,主席台} with rating_evidence.txt",
        "Target 4 evaluation tier qualification and rating evidence",
        "**T4** qualifying tier caption present in rating_evidence.txt",
        "T3 tier in {上座,好强,主席台}, best effort recorded",
    ]
    reasons = [
        loop._blocked_assessment(_structured((text, False)))
        for text in variants
    ]
    assert reasons[0] == ""
    assert reasons[1] == ""
    assert "consecutive judged turns attained nothing new" in reasons[-1]


def test_a_gate_pass_counts_as_progress(tmp_path):
    loop = _loop(
        tmp_path,
        blocked_after_stagnant_verdicts=3,
        min_turns_before_blocked=0,
    )
    loop._task_turn = 20
    loop._feasibility_block_reason = lambda: ""
    verdict = _structured(("T4 tier", False))
    _harvest(tmp_path, passing=6)  # baseline: no gate has newly passed yet
    # Turn 1 only registers the baseline; turns 2-4 are the stagnant streak.
    assert loop._blocked_assessment(verdict) == ""
    assert loop._blocked_assessment(verdict) == ""
    assert loop._blocked_assessment(verdict) == ""
    assert "consecutive judged turns" in loop._blocked_assessment(verdict)
    # A newly passing gate is real progress and re-arms the streak.
    _harvest(tmp_path, passing=7)
    assert loop._blocked_assessment(verdict) == ""
    assert loop._progress.streak == 0


def test_agent_declared_closure_closes_the_run(tmp_path):
    """The agent's own goal_terminal was ignored; it must close the run.

    One study logged goal_terminal at turn 32 and then paid for 271 more
    judged turns because the harness never read its own journal.
    """
    loop = _loop(tmp_path, min_turns_before_blocked=0)
    loop._task_turn = 40
    loop._feasibility_block_reason = lambda: ""
    from datetime import UTC, datetime

    audit = tmp_path / ".opencode" / "goal_audit.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(
        json.dumps({
            "event": "goal_terminal",
            "reason": "Tier UNMET; replanning routed to successor",
            "ts": datetime.now(UTC).isoformat(),
        }) + "\n",
        encoding="utf-8",
    )
    verdict = _structured(("T4 tier", False))
    loop._blocked_assessment(verdict)
    reason = loop._blocked_assessment(verdict)
    assert "declared this run terminally closed" in reason
    assert "goal_terminal" in reason


def test_audit_closure_from_a_previous_cycle_is_ignored(tmp_path):
    """An explicit relaunch must not be re-closed by an old audit event."""
    import time

    from supervisor.core.target_state import read_terminal_assertion

    audit = tmp_path / ".opencode" / "goal_audit.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(
        json.dumps({
            "event": "goal_terminal",
            "reason": "previous cycle closed",
            "ts": "2020-01-01T00:00:00+00:00",
        }) + "\n",
        encoding="utf-8",
    )
    assert read_terminal_assertion(tmp_path, since_ts=time.time()) == ""
    # Without the run-scope (direct audit consumers), the event is still read.
    assert "goal_terminal" in read_terminal_assertion(tmp_path)


def test_blocked_exit_writes_the_closed_target_state(tmp_path):
    """The harness could not express status="closed"; now it can."""
    from supervisor.core.target_state import (
        initialise_target_state,
        load_target_state,
        record_target_iteration,
    )

    initialise_target_state(tmp_path, target="Reach 9 gates.")
    record_target_iteration(
        tmp_path,
        iteration=1,
        direction="attempt",
        evidence="e",
        accepted=False,
        reason="r",
    )
    loop = _loop(tmp_path)
    loop.supervisor = SimpleNamespace(
        report_final_status=lambda **_: "report",
    )
    loop._write = lambda *a, **k: None
    loop._record_structured_failure = lambda *a, **k: None
    loop._memory_goal = lambda: ""
    list(loop._on_blocked(_structured(("T4 tier", False)), "ceiling"))
    assert load_target_state(tmp_path).status == "closed"


def test_a_closed_target_state_is_not_reopened(tmp_path):
    from supervisor.core.target_state import (
        initialise_target_state,
        load_target_state,
        mark_target_closed,
    )

    initialise_target_state(tmp_path, target="Reach 9 gates.")
    mark_target_closed(tmp_path, "ceiling")
    initialise_target_state(tmp_path, target="Reach 9 gates.")
    assert load_target_state(tmp_path).status == "closed"


def test_gate_facts_live_outside_the_archived_tree(tmp_path):
    """logs/ is archived every cycle; the evidence base must survive it."""
    from supervisor.protocols.feasibility_gate import (
        default_facts_path,
        default_gate_results_path,
    )

    facts = default_facts_path(tmp_path)
    assert facts.parent == tmp_path
    assert default_gate_results_path(tmp_path).parent == tmp_path

    legacy = tmp_path / "logs" / "feasibility_facts.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{}", encoding="utf-8")
    assert default_facts_path(tmp_path) == legacy


def test_archiver_skips_the_run_state_directory(tmp_path):
    from supervisor.workspace.workspace_archiver import WorkspaceArchiver

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "feasibility_facts.json").write_text("{}", encoding="utf-8")
    (tmp_path / "deliverable.py").write_text("x = 1\n", encoding="utf-8")
    archiver = WorkspaceArchiver(tmp_path, keep_last=0)
    result = archiver.archive_workspace(label="t")
    assert "deliverable.py" in result.archived_files
    assert not any(path.startswith("logs/") for path in result.archived_files)


# --------------------------------------------------------------------------- #
# Stand-down directives (2026-11-05 run_152ccb68)                             #
# --------------------------------------------------------------------------- #

def _standdown(action, *unmet):
    """Verdict with an explicit NEXT_ACTION and no structured criteria."""
    return SimpleNamespace(
        all_targets_met=False,
        unmet_criteria=list(unmet),
        next_action=action,
    )


def test_standdown_next_action_closes_the_run(tmp_path):
    """A judge that keeps saying "await termination" is a closed loop.

    run_152ccb68 carried "None in this workspace. Await supervisor
    control-channel termination" for dozens of turns while targets stayed
    UNMET; the attainment rule only saw identical criteria, so no trigger
    bound to the directive itself.
    """
    loop = _loop(
        tmp_path,
        blocked_after_stagnant_verdicts=3,
        min_turns_before_blocked=0,
    )
    loop._task_turn = 20
    directive = (
        "None in this workspace. Await supervisor control-channel termination, "
        "then seed the Lane-A successor."
    )
    assert loop._blocked_assessment(_standdown(directive)) == ""
    assert loop._blocked_assessment(_standdown(directive)) == ""
    reason = loop._blocked_assessment(_standdown(directive))
    assert "stand down" in reason
    assert "await supervisor control-channel termination" in reason.lower()


def test_standdown_recognition_is_narrow():
    from supervisor.core.loop_base import _is_standdown_action

    assert _is_standdown_action("**None in this workspace.**")
    assert _is_standdown_action(
        "None authorized. Study is closed by terminal ruling.",
    )
    assert _is_standdown_action("Remain in standdown; initiate no code")
    assert _is_standdown_action("Take zero in-workspace action")
    assert _is_standdown_action("Terminate this lane from the control channel")
    assert _is_standdown_action("Stop taking in-workspace actions")
    assert _is_standdown_action("Close this thread cleanly")
    # Diagnostics and real plans must not count as stand-downs.
    assert not _is_standdown_action("None of the gates pass; implement X")
    assert not _is_standdown_action("Design and implement a NEW indicator")
    assert not _is_standdown_action(
        "Stop the current approach and implement the missing signal",
    )
    assert not _is_standdown_action("Execute the single-file deletion above")


def test_a_new_gate_pass_resets_the_standdown_streak(tmp_path):
    loop = _loop(
        tmp_path,
        blocked_after_stagnant_verdicts=3,
        min_turns_before_blocked=0,
    )
    loop._task_turn = 20
    _harvest(tmp_path, passing=6)
    directive = "Take zero in-workspace action; supervisor must terminate."
    assert loop._blocked_assessment(_standdown(directive)) == ""
    assert loop._blocked_assessment(_standdown(directive)) == ""
    assert loop._standdown_streak == 2
    _harvest(tmp_path, passing=7)
    assert loop._blocked_assessment(_standdown(directive)) == ""
    assert loop._standdown_streak == 0


# --------------------------------------------------------------------------- #
# Closure documents (ESCALATION.md terminal ruling)                            #
# --------------------------------------------------------------------------- #

_RULING = (
    "## TERMINAL RULING 2026-11-04 23:28 - CLOSED, TARGET NOT MET, NO REOPENING\n"
    "No further design iteration will be entertained.\n"
)


def test_closure_document_ruling_is_read(tmp_path):
    from supervisor.core.target_state import read_terminal_assertion

    (tmp_path / "ESCALATION.md").write_text(_RULING, encoding="utf-8")
    assertion = read_terminal_assertion(tmp_path)
    assert "ESCALATION.md" in assertion
    assert "TERMINAL RULING" in assertion.upper()


def test_closure_document_ruling_closes_the_run(tmp_path):
    loop = _loop(tmp_path, min_turns_before_blocked=0)
    loop._task_turn = 20
    (tmp_path / "ESCALATION.md").write_text(_RULING, encoding="utf-8")
    reason = loop._blocked_assessment(_verdict("T2 AB >= 9/10 gates"))
    assert "declared this run terminally closed" in reason
    assert loop._blocked_agent_asserted is True


def test_closure_document_from_a_previous_cycle_is_ignored(tmp_path):
    import os

    from supervisor.core.target_state import read_terminal_assertion

    loop = _loop(tmp_path, min_turns_before_blocked=0)
    loop._task_turn = 20
    path = tmp_path / "ESCALATION.md"
    path.write_text(_RULING, encoding="utf-8")
    old = loop._run_started_at - 3600
    os.utime(path, (old, old))
    assert read_terminal_assertion(
        tmp_path, since_ts=loop._run_started_at,
    ) == ""
    assert "declared this run terminally closed" not in loop._blocked_assessment(
        _verdict("T2 AB >= 9/10 gates"),
    )


def test_a_closed_heading_without_a_terminal_ruling_does_not_close(tmp_path):
    from supervisor.core.target_state import read_terminal_assertion

    (tmp_path / "ESCALATION.md").write_text(
        "# ESCALATION - CLOSED 22:57 (ruling 'go v10' executed)\n", encoding="utf-8",
    )
    assert read_terminal_assertion(tmp_path) == ""


def _write_gate_facts(tmp_path, status):
    (tmp_path / "feasibility_facts.json").write_text(
        json.dumps({
            "gates": [
                {
                    "name": "g1",
                    "definition": "all gates pass",
                    "status": status,
                    "evidence": "",
                    "ref": 1,
                },
            ],
        }),
        encoding="utf-8",
    )


def _block_with_agent_closure(tmp_path, **loop_overrides):
    from supervisor.core.target_state import (
        initialise_target_state,
        load_target_state,
    )

    initialise_target_state(tmp_path, target="Reach 9 gates.")
    loop = _loop(tmp_path, **loop_overrides)
    loop.supervisor = SimpleNamespace(
        report_final_status=lambda **_: "report",
    )
    loop._write = lambda *a, **k: None
    loop._record_structured_failure = lambda *a, **k: None
    loop._memory_goal = lambda: ""
    loop._blocked_agent_asserted = True
    list(loop._on_blocked(_verdict("T2 AB >= 9/10 gates"), "agent declared closure"))
    return load_target_state(tmp_path)


def test_agent_asserted_closure_with_positive_facts_records_blocked(tmp_path):
    """A measured passing gate outweighs the agent's closure for `closed`.

    The run is still over; the reopenable `blocked` is the honest state, and
    it must never be left at `running`.
    """
    _write_gate_facts(tmp_path, "passing")
    assert _block_with_agent_closure(tmp_path).status == "blocked"


def test_neutral_facts_do_not_contradict_an_agent_closure(tmp_path):
    """"No gate is known-blocked" is not evidence of reachability.

    run_152ccb68's facts were all `open`; treating that as a contradiction
    turned an explicit terminal ruling into a reopenable `blocked` status.
    """
    _write_gate_facts(tmp_path, "open")
    assert _block_with_agent_closure(tmp_path).status == "closed"


def test_known_ceiling_meeting_the_requirement_is_positive_evidence(tmp_path):
    """Facts without a gate list still contradict a closure when measured."""
    from supervisor.core.target_state import (
        initialise_target_state,
        load_target_state,
    )

    initialise_target_state(tmp_path, target="Reach 5 gates.")
    (tmp_path / "feasibility_facts.json").write_text(
        json.dumps({
            "required_gates": 5,
            "known_ceiling": {"count": 5, "of": 5},
        }),
        encoding="utf-8",
    )
    loop = _loop(tmp_path)
    loop.supervisor = SimpleNamespace(report_final_status=lambda **_: "report")
    loop._write = lambda *a, **k: None
    loop._record_structured_failure = lambda *a, **k: None
    loop._memory_goal = lambda: ""
    loop._blocked_agent_asserted = True
    list(loop._on_blocked(_verdict("T2"), "agent declared closure"))
    assert load_target_state(tmp_path).status == "blocked"


def test_a_ruling_that_predates_the_run_is_not_reactivated(tmp_path):
    """Appending a note refreshes mtime; the old ruling must stay inert."""
    from supervisor.core.target_state import (
        closure_ruling_lines,
        read_terminal_assertion,
    )

    (tmp_path / "ESCALATION.md").write_text(_RULING, encoding="utf-8")
    loop = _loop(tmp_path, min_turns_before_blocked=0)
    loop._task_turn = 20
    baseline = closure_ruling_lines(tmp_path)
    assert read_terminal_assertion(
        tmp_path,
        since_ts=loop._run_started_at,
        baseline=baseline,
    ) == ""

    with (tmp_path / "ESCALATION.md").open("a", encoding="utf-8") as handle:
        handle.write("\n## Notes\nunrelated edit\n")
    assert "declared this run terminally closed" not in loop._blocked_assessment(
        _verdict("T2 AB >= 9/10 gates"),
    )

    with (tmp_path / "ESCALATION.md").open("a", encoding="utf-8") as handle:
        handle.write(
            "\n## TERMINAL RULING 2026-11-05 11:30 - CLOSED, NO REOPENING\n",
        )
    assert "declared this run terminally closed" in loop._blocked_assessment(
        _verdict("T2 AB >= 9/10 gates"),
    )


# --------------------------------------------------------------------------- #
# Durable target state keys acceptance on attainment, not churn                #
# --------------------------------------------------------------------------- #

def test_churn_is_not_attainment_in_the_durable_state(tmp_path):
    """Hygiene churn must not keep target_state at running/stagnation 0.

    run_152ccb68 read `running`, iteration 123, stagnation 0 after a terminal
    ruling because every turn's evidence was a file diff.
    """
    from supervisor.core.goal_guard import GoalGuard
    from supervisor.core.target_state import (
        initialise_target_state,
        load_target_state,
    )
    from supervisor.protocols.protocol import Protocol

    protocol = Protocol(
        raw="",
        input_section="",
        target_section="Reach 9 AB gates.",
        restrictions_section="",
    )
    config = SupervisorConfig(
        workspace=tmp_path,
        protocol_path=tmp_path / "protocol.md",
    )
    initialise_target_state(tmp_path, target="Reach 9 AB gates.")
    guard = GoalGuard(config, protocol)
    verdict = SimpleNamespace(
        feedback="cleaned up a backup file",
        next_action="keep working",
        all_targets_met=False,
        criteria_results=[],
    )
    for _ in range(2):
        guard.evaluate_turn(
            verdict,
            worktree_sig="sig",
            worktree_changed=True,
            output="deleted a .bak and rewrote summary.md",
            attainment_progressed=False,
        )
    state = load_target_state(tmp_path)
    assert state.status == "replan_required"
    assert state.stagnation_count == 2
    assert "attainment_progressed=False" in state.evidence[-1]


def test_unstructured_verdicts_record_an_unmeasured_turn(tmp_path):
    """No criteria means no attainment measurement: churn is not acceptance.

    The durable state must neither reset a real stagnation streak because a
    file changed nor claim stagnation that was never measured.
    """
    from supervisor.core.goal_guard import GoalGuard
    from supervisor.core.target_state import (
        initialise_target_state,
        load_target_state,
        record_target_iteration,
    )
    from supervisor.protocols.protocol import Protocol

    protocol = Protocol(
        raw="",
        input_section="",
        target_section="Reach 9 AB gates.",
        restrictions_section="",
    )
    config = SupervisorConfig(
        workspace=tmp_path,
        protocol_path=tmp_path / "protocol.md",
    )
    initialise_target_state(tmp_path, target="Reach 9 AB gates.")
    for _ in range(2):
        record_target_iteration(
            tmp_path,
            iteration=1,
            direction="attempt",
            evidence="e",
            accepted=False,
            reason="r",
        )
    assert load_target_state(tmp_path).status == "replan_required"

    guard = GoalGuard(config, protocol)
    verdict = SimpleNamespace(
        feedback="work in progress",
        next_action="keep working",
        all_targets_met=False,
        criteria_results=[],
    )
    guard.evaluate_turn(
        verdict,
        worktree_sig="sig",
        worktree_changed=True,
        output="wrote a file",
        attainment_progressed=None,
    )
    state = load_target_state(tmp_path)
    assert state.status == "replan_required"
    assert state.stagnation_count == 2
    assert "attainment=unmeasured" in state.evidence[-1]


def test_attainment_is_recorded_when_blocked_exit_is_disabled(tmp_path):
    """Disabling the BLOCKED exit must not blind the durable target state."""
    loop = _loop(
        tmp_path,
        blocked_after_stagnant_verdicts=2,
        min_turns_before_blocked=0,
        enable_blocked_exit=False,
    )
    loop._task_turn = 20
    verdict = _structured(("T4 tier", False))
    assert loop._blocked_assessment(verdict) == ""  # cold start: baseline
    assert loop._blocked_assessment(verdict) == ""  # stagnation, but exit off
    assert loop._last_attainment_progressed is False


def test_audit_closure_written_before_the_run_is_ignored_without_ts(tmp_path):
    """A timeless old audit event must not re-close a relaunch."""
    from supervisor.core.target_state import (
        audit_journal_size,
        read_terminal_assertion,
    )

    audit = tmp_path / ".opencode" / "goal_audit.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(
        json.dumps({"event": "goal_terminal", "reason": "previous cycle"}) + "\n",
        encoding="utf-8",
    )
    offset = audit_journal_size(tmp_path)
    assert read_terminal_assertion(tmp_path, offset=offset) == ""
    with audit.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"event": "goal_terminal", "reason": "this run"}) + "\n",
        )
    assert "this run" in read_terminal_assertion(tmp_path, offset=offset)


def test_loop_ignores_audit_closure_from_before_the_run(tmp_path):
    audit = tmp_path / ".opencode" / "goal_audit.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(
        json.dumps({"event": "goal_terminal", "reason": "previous cycle"}) + "\n",
        encoding="utf-8",
    )
    loop = _loop(tmp_path, min_turns_before_blocked=0)
    loop._task_turn = 20
    assert "declared this run terminally closed" not in loop._blocked_assessment(
        _verdict("T2 AB >= 9/10 gates"),
    )
    with audit.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"event": "goal_terminal", "reason": "this run"}) + "\n",
        )
    assert "declared this run terminally closed" in loop._blocked_assessment(
        _verdict("T2 AB >= 9/10 gates"),
    )


# --------------------------------------------------------------------------- #
# Judge-directed closure (run_c1b12a13, 2026-11-05)                           #
# --------------------------------------------------------------------------- #

def test_judge_closure_order_bypasses_the_turn_floor(tmp_path):
    """A judge that orders the protocol failure condition must close now.

    run_c1b12a13 did three design iterations and six submissions inside one
    judged turn; its judge invoked the failure condition and ordered a
    closeout, but the 12-turn floor kept re-triggering the agent.
    """
    loop = _loop(tmp_path, min_turns_before_blocked=12)
    loop._task_turn = 1
    directive = (
        "Invoke the protocol failure condition. The structural analysis proves "
        "the gate bar is unreachable. Close the study: write a one-page handoff "
        "in design_notes.md. Do NOT burn additional slots on the variant."
    )
    reason = loop._blocked_assessment(_standdown(directive, "ab_pass_gate_score"))
    assert "directed the run to close the study" in reason
    assert "protocol failure condition" in reason
    assert loop._blocked_agent_asserted is False


def test_closure_directive_recognition():
    from supervisor.core.loop_base import _is_closure_directive

    assert _is_closure_directive("Invoke the protocol failure condition.")
    assert _is_closure_directive("Close the study: write a handoff.")
    assert _is_closure_directive("The study is closed; no further action.")
    assert _is_closure_directive(
        "The study is terminally closed. No further agent action is authorized.",
    )
    assert _is_closure_directive("The study has been closed by directive.")
    assert _is_closure_directive("Do not burn additional slots.")
    # Pre-registered conditionals are plans, not current orders.
    assert not _is_closure_directive(
        "If all arms fail, invoke the protocol failure condition.",
    )
    assert not _is_closure_directive("Write the indicator and submit it.")


def test_a_conditional_failure_condition_is_not_a_closure_order(tmp_path):
    loop = _loop(tmp_path, min_turns_before_blocked=12)
    loop._task_turn = 1
    verdict = _standdown(
        "If all three arms fail, invoke the protocol failure condition.",
        "ab_pass_gate_score",
    )
    assert loop._blocked_assessment(verdict) == ""


def test_agent_closure_bypasses_the_turn_floor(tmp_path):
    loop = _loop(tmp_path, min_turns_before_blocked=12)
    loop._task_turn = 1
    (tmp_path / "ESCALATION.md").write_text(_RULING, encoding="utf-8")
    reason = loop._blocked_assessment(_verdict("T2 AB >= 9/10 gates"))
    assert "declared this run terminally closed" in reason


def test_a_judge_closure_order_respects_disabled_blocked_exit(tmp_path):
    loop = _loop(
        tmp_path,
        min_turns_before_blocked=0,
        enable_blocked_exit=False,
    )
    loop._task_turn = 1
    assert loop._blocked_assessment(
        _standdown("Invoke the protocol failure condition."),
    ) == ""
