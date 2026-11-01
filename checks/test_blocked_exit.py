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
    # it reads workspace/logs/gate_results_last.json, absent unless written.
    loop._feasibility_block_reason = lambda: ""
    return loop


def _verdict(*unmet, met=False):
    return SimpleNamespace(all_targets_met=met, unmet_criteria=list(unmet))


def _harvest(tmp_path, passing):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    rows = [{"gate": f"g{i}", "conclusion": "PASS"} for i in range(passing)]
    rows += [
        {"gate": f"g{i}", "conclusion": "UNMET"} for i in range(passing, 10)
    ]
    (logs / "gate_results_last.json").write_text(
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


def test_workspace_change_resets_the_streak(tmp_path):
    loop = _loop(
        tmp_path,
        blocked_after_stagnant_verdicts=3,
        min_turns_before_blocked=0,
    )
    loop._task_turn = 20
    verdict = _verdict("T2 AB >= 9/10 gates")
    loop._blocked_assessment(verdict)
    loop._blocked_assessment(verdict)
    loop._last_changed_files = ["src/indicator.py"]
    assert loop._blocked_assessment(verdict) == ""
    assert loop._unmet_streak == 1


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
    # The board metrics line counts blocked jobs instead of dropping them.
    assert '"BLOCKED": 0' in board
    assert "blocked" in board.split("metrics_label.set_text")[1]
    # A blocked run's report reaches the job store via blocked_report.md.
    assert "blocked_report.md" in manager
