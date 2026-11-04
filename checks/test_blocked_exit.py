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
    audit = tmp_path / ".opencode" / "goal_audit.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(
        json.dumps({
            "event": "goal_terminal",
            "reason": "Tier UNMET; replanning routed to successor",
            "ts": "2026-11-03T23:54:07+00:00",
        }) + "\n",
        encoding="utf-8",
    )
    verdict = _structured(("T4 tier", False))
    loop._blocked_assessment(verdict)
    reason = loop._blocked_assessment(verdict)
    assert "declared this run terminally closed" in reason
    assert "goal_terminal" in reason


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
