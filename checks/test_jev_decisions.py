"""Structured decision service (jev): client, policy, and closing wiring."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from supervisor.core.decision_service import (
    CLOSE_ACTIONS,
    ChoiceDecision,
    CloseDecision,
    JevDecisionClient,
    build_close_evidence,
)
from supervisor.core.llm_support.models import CriterionResult, SupervisorVerdict
from supervisor.core.loop_base import BaseLoop
from supervisor.utils.config import SupervisorConfig

_REPO = Path(__file__).resolve().parents[1]


def _answer(close: float, action: str, terminal: float | None = None) -> dict:
    probs = {name: 0.01 for name in CLOSE_ACTIONS}
    probs[action] = 0.97
    return {
        "answers": {
            "close_now": {"noul": close},
            "terminal": {"noul": close if terminal is None else terminal},
            "action": {"probabilities": probs},
        },
    }


def _client(answer: dict, *, threshold: float = 0.85, **kwargs) -> JevDecisionClient:
    return JevDecisionClient(
        "http://decision.test/systemone",
        threshold=threshold,
        transport=lambda payload: answer,
        **kwargs,
    )


def test_close_run_above_threshold_is_a_close():
    decision = _client(_answer(0.95, "close_run")).decide_close("evidence")
    assert decision is not None
    assert decision.verdict == "close"
    assert decision.close_probability == 0.95


def test_finish_closeout_is_a_grace_turn():
    decision = _client(_answer(0.95, "finish_closeout_then_close")).decide_close("e")
    assert decision is not None
    assert decision.verdict == "grace"


def test_low_confidence_close_is_not_enough():
    decision = _client(_answer(0.60, "close_run")).decide_close("e")
    assert decision is not None
    assert decision.verdict == "continue"


def test_continue_and_escalate_are_not_closes():
    assert _client(_answer(0.01, "continue_design_work")).decide_close("e").verdict == "continue"
    assert _client(_answer(0.95, "escalate_to_operator")).decide_close("e").verdict == "escalate"


def test_transport_failure_returns_none():
    def boom(payload):
        raise TimeoutError("decision service down")

    client = JevDecisionClient(
        "http://decision.test/systemone", transport=boom,
    )
    assert client.decide_close("e") is None


def test_malformed_response_returns_none():
    client = _client({"answers": {"close_now": {"noul": "NaN"}}})
    assert client.decide_close("e") is None


def test_close_evidence_carries_state_and_verdict():
    verdict = SimpleNamespace(
        criteria_results=[CriterionResult("Target 2", False)],
        next_action="None authorized within this protocol instance.",
        feedback="The study is terminally closed.",
    )
    evidence = build_close_evidence(
        mandate="Produce one indicator with >= 8 gates.",
        status="replan_required",
        iteration=12,
        stagnation=5,
        passing_gates=6,
        verdict=verdict,
        output="Standing down; no writes.",
    )
    assert "RUN EVIDENCE" in evidence
    assert "stagnant streak=5" in evidence
    assert "passing gates=6" in evidence
    assert "[UNMET] Target 2" in evidence
    assert "terminally closed" in evidence


# --------------------------------------------------------------------------- #
# The three choice arbitrations: client parsing                                #
# --------------------------------------------------------------------------- #


def _choice_answer(name: str, **probabilities) -> dict:
    return {"answers": {name: {"probabilities": probabilities}}}


def test_decide_contradictions_flags_each_pair():
    client = _client({"answers": {"c0": {"noul": 0.9}, "c1": {"noul": 0.1}}})
    flags = client.decide_contradictions(
        [("Target 1", "gate passed exit=0"), ("Target 2", "no record")],
        context="JUDGE VERDICT CONTRADICTION CHECK",
    )
    assert flags == [True, False]


def test_decide_contradictions_missing_answer_stays_unknown():
    client = _client({"answers": {}})
    assert client.decide_contradictions([("T", "e")], context="c") == [None]


def test_decide_contradictions_caps_the_pair_list():
    client = _client({"answers": {}})
    pairs = [(f"T{i}", "e") for i in range(12)]
    assert len(client.decide_contradictions(pairs, context="c")) == 10


def test_decide_stagnation_picks_the_winning_action():
    client = _client(_choice_answer(
        "action", continue_=0.0, nudge=0.2, strategy_change=0.6, restart=0.2,
    ))
    decision = client.decide_stagnation(
        stage=2, stagnant_turns=3, reason="changed no files", output="o", state="s",
    )
    assert decision is not None
    assert decision.action == "strategy_change"
    assert decision.probability == pytest.approx(0.6)


def test_decide_protocol_violation_parses_the_verdict_choice():
    client = _client(_choice_answer(
        "verdict", violation=0.8, no_violation=0.1, uncertain=0.1,
    ))
    decision = client.decide_protocol_violation(
        violations=["scanner flagged: deleted a protected file"],
        output="rm -rf deliverable",
        restrictions="## RESTRICTIONS\nnone",
    )
    assert decision is not None
    assert decision.action == "violation"
    assert decision.probability == pytest.approx(0.8)


def test_decide_done_parses_the_accept_choice():
    client = _client(_choice_answer("verdict", accept=0.7, reject=0.2, need_evidence=0.1))
    decision = client.decide_done(
        mandate="Produce one indicator with >= 8 gates.",
        criteria=[("8 gates pass", True, "exit=0")],
        changed_files=["deliverable.py"],
        output="DONE",
    )
    assert decision is not None
    assert decision.action == "accept"
    assert decision.probability == pytest.approx(0.7)


def test_malformed_choice_response_returns_none():
    assert _client({"answers": {"action": {}}}).decide_stagnation(
        stage=1, stagnant_turns=2, reason="r", output="o", state="s",
    ) is None


# --------------------------------------------------------------------------- #
# Loop integration: contradiction arbitration                                  #
# --------------------------------------------------------------------------- #


class _StubContradictionJev:
    def __init__(self, flags: list[bool | None]):
        self._flags = flags
        self.calls: list[tuple[list, str]] = []

    def decide_contradictions(self, pairs, *, context):
        self.calls.append((pairs, context))
        return self._flags


def _verdict(*results, met=True) -> SupervisorVerdict:
    return SupervisorVerdict(
        raw="",
        all_targets_met=met,
        feedback="",
        criteria_results=list(results),
    )


def test_apply_jev_contradictions_downgrades_a_met_claim(tmp_path):
    loop = _loop(tmp_path)
    loop._jev = _StubContradictionJev([True])
    verdict = _verdict(
        CriterionResult("Target 1", met=True, evidence="exit=0"),
    )

    loop._apply_jev_contradictions(verdict)

    assert verdict.criteria_results[0].met is False
    assert verdict.all_targets_met is False
    assert any("jev" in note for note in verdict.validation_notes)
    assert any(
        note.startswith("Jev contradiction check") for note in loop._jev_notes
    )


def test_apply_jev_contradictions_leaves_unflagged_met_alone(tmp_path):
    loop = _loop(tmp_path)
    loop._jev = _StubContradictionJev([False])
    verdict = _verdict(CriterionResult("Target 1", met=True, evidence="exit=0"))

    loop._apply_jev_contradictions(verdict)

    assert verdict.criteria_results[0].met is True
    assert verdict.all_targets_met is True
    assert loop._jev_notes == []


def test_apply_jev_contradictions_without_jev_is_inert(tmp_path):
    loop = _loop(tmp_path)
    verdict = _verdict(CriterionResult("Target 1", met=True, evidence="exit=0"))

    loop._apply_jev_contradictions(verdict)

    assert verdict.criteria_results[0].met is True
    assert verdict.all_targets_met is True


# --------------------------------------------------------------------------- #
# Loop integration: protocol-flag arbitration                                  #
# --------------------------------------------------------------------------- #


class _StubChoiceJev:
    """Stub for the three ChoiceDecision arbitrations (stagnation/protocol/DONE)."""

    def __init__(self, action: str, probability: float = 0.9):
        self.action = action
        self.probability = probability
        self.calls: list[dict] = []

    def decide_stagnation(self, *, stage, stagnant_turns, reason, output, state):
        self.calls.append({
            "stage": stage,
            "stagnant_turns": stagnant_turns,
            "reason": reason,
        })
        return ChoiceDecision(action=self.action, probability=self.probability)

    def decide_protocol_violation(self, *, violations, output, restrictions):
        self.calls.append({"violations": violations, "output": output})
        return ChoiceDecision(action=self.action, probability=self.probability)

    def decide_done(self, *, mandate, criteria, changed_files, output):
        self.calls.append({
            "mandate": mandate,
            "criteria": criteria,
            "changed_files": changed_files,
        })
        return ChoiceDecision(action=self.action, probability=self.probability)


def _alignment(*descriptions: str) -> SimpleNamespace:
    return SimpleNamespace(
        aligned=False,
        violations=[
            SimpleNamespace(description=description) for description in descriptions
        ],
        reinforcement_message="Re-read the restrictions.",
    )


def test_jev_suppresses_a_false_positive_protocol_flag(tmp_path):
    loop = _loop(tmp_path)
    loop._jev = _StubChoiceJev("no_violation", probability=0.9)
    loop.protocol = SimpleNamespace(restrictions_section="## RESTRICTIONS\nnone")

    suppress, note = loop._arbitrate_protocol(
        _alignment("scanner flagged: quoted the restriction"), "quoted the rule",
    )

    assert suppress is True
    assert note.startswith("Jev protocol review")
    assert loop._jev.calls[0]["violations"] == ["scanner flagged: quoted the restriction"]


def test_jev_confirmed_violation_keeps_the_warning(tmp_path):
    loop = _loop(tmp_path)
    loop._jev = _StubChoiceJev("violation", probability=0.9)
    suppress, note = loop._arbitrate_protocol(_alignment("flagged"), "deleted it")
    assert suppress is False
    assert note.startswith("Jev protocol review")


def test_low_confidence_no_violation_does_not_suppress(tmp_path):
    loop = _loop(tmp_path)
    loop._jev = _StubChoiceJev("no_violation", probability=0.4)
    assert loop._arbitrate_protocol(_alignment("flagged"), "quoted")[0] is False


def test_jev_protocol_outage_keeps_the_warning(tmp_path):
    class _Boom:
        def decide_protocol_violation(self, **kwargs):
            raise TimeoutError("decision service down")

    loop = _loop(tmp_path)
    loop._jev = _Boom()
    assert loop._arbitrate_protocol(_alignment("flagged"), "x")[0] is False


def test_without_jev_the_protocol_warning_stands(tmp_path):
    loop = _loop(tmp_path)
    assert loop._arbitrate_protocol(_alignment("flagged"), "x") == (False, "")


# --------------------------------------------------------------------------- #
# GoalGuard integration: DONE proposal arbitration                             #
# --------------------------------------------------------------------------- #


def _guard(tmp_path, jev=None, **overrides):
    from supervisor.core.goal_guard import GoalGuard
    from supervisor.protocols.protocol import parse_protocol_text

    (tmp_path / "protocol.md").write_text(_PROTOCOL_MD, encoding="utf-8")
    protocol = parse_protocol_text(_PROTOCOL_MD)
    guard = GoalGuard(
        SupervisorConfig(
            protocol_path=tmp_path / "protocol.md", workspace=tmp_path, **overrides,
        ),
        protocol,
        jev=jev,
    )
    guard.start()
    return guard


_PROTOCOL_MD = "## INPUT\nx\n## TARGET\n1. must_pass\n## RESTRICTIONS\nnone\n"

_DONE_VERDICT = _verdict(
    CriterionResult("must_pass", met=True, evidence="exit=0"),
)


def test_jev_reject_blocks_an_otherwise_passing_done(tmp_path):
    (tmp_path / "deliverable.py").write_text("x = 1\n", encoding="utf-8")
    stub = _StubChoiceJev("reject", probability=0.9)
    guard = _guard(tmp_path, jev=stub)

    decision = guard.evaluate_done(_DONE_VERDICT, ["deliverable.py"], "wrapped up")

    assert decision.accepted is False
    assert "jev rejected" in decision.reason
    assert decision.jev_note.startswith("Jev DONE review")
    assert stub.calls[0]["criteria"] == [("must_pass", True, "exit=0")]


def test_jev_need_evidence_blocks_below_acceptance(tmp_path):
    (tmp_path / "deliverable.py").write_text("x = 1\n", encoding="utf-8")
    guard = _guard(tmp_path, jev=_StubChoiceJev("need_evidence", probability=0.9))

    decision = guard.evaluate_done(_DONE_VERDICT, ["deliverable.py"], "wrapped up")

    assert decision.accepted is False
    assert "more evidence" in decision.reason


def test_jev_accept_passes_with_a_note(tmp_path):
    (tmp_path / "deliverable.py").write_text("x = 1\n", encoding="utf-8")
    guard = _guard(tmp_path, jev=_StubChoiceJev("accept", probability=0.9))

    decision = guard.evaluate_done(_DONE_VERDICT, ["deliverable.py"], "wrapped up")

    assert decision.accepted is True
    assert decision.jev_note.startswith("Jev DONE review")


def test_low_confidence_reject_does_not_block(tmp_path):
    (tmp_path / "deliverable.py").write_text("x = 1\n", encoding="utf-8")
    guard = _guard(tmp_path, jev=_StubChoiceJev("reject", probability=0.4))

    decision = guard.evaluate_done(_DONE_VERDICT, ["deliverable.py"], "wrapped up")

    assert decision.accepted is True
    assert decision.jev_note.startswith("Jev DONE review")


# --------------------------------------------------------------------------- #
# GoalGuard integration: stagnation arbitration                                #
# --------------------------------------------------------------------------- #


def _stagnant_turn(guard, output="same output"):
    return guard.evaluate_turn(
        _verdict(),
        worktree_sig="sig",
        worktree_changed=False,
        output=output,
    )


def test_jev_restart_marks_the_turn_decision(tmp_path):
    guard = _guard(tmp_path, jev=_StubChoiceJev("restart", probability=0.9))
    for _ in range(2):
        assert _stagnant_turn(guard).restart is False
    third = _stagnant_turn(guard)

    assert third.restart is True
    assert "No further progress is possible" in third.guidance
    assert third.jev_note.startswith("Jev stagnation decision")
    assert guard._jev.calls[0]["stagnant_turns"] == 2


def test_jev_strategy_change_escalates_the_guidance(tmp_path):
    guard = _guard(tmp_path, jev=_StubChoiceJev("strategy_change", probability=0.9))
    for _ in range(2):
        _stagnant_turn(guard)
    third = _stagnant_turn(guard)

    assert third.restart is False
    assert "STRATEGY CHANGE REQUIRED" in third.guidance


def test_jev_continue_suppresses_the_stagnation_signal(tmp_path):
    guard = _guard(tmp_path, jev=_StubChoiceJev("continue", probability=0.9))
    for _ in range(2):
        _stagnant_turn(guard)
    third = _stagnant_turn(guard)

    assert third.restart is False
    assert third.stagnation is None
    assert third.guidance == ""


def test_without_jev_stagnation_falls_back_to_the_deterministic_nudge(tmp_path):
    guard = _guard(tmp_path)
    for _ in range(2):
        _stagnant_turn(guard)
    third = _stagnant_turn(guard)

    assert third.restart is False
    assert third.stagnation is not None
    assert "made no file changes" in third.guidance


# --------------------------------------------------------------------------- #
# Loop integration                                                             #
# --------------------------------------------------------------------------- #


class _StubJev:
    def __init__(self, verdict: str):
        self.verdict = verdict
        self.calls: list[str] = []

    def decide_close(self, evidence: str) -> CloseDecision:
        self.calls.append(evidence)
        action = (
            "finish_closeout_then_close"
            if self.verdict == "grace"
            else "close_run"
            if self.verdict == "close"
            else "continue_design_work"
        )
        return CloseDecision(
            verdict=self.verdict,
            close_probability=0.95 if self.verdict != "continue" else 0.01,
            terminal_probability=0.95,
            action=action,
            actions={action: 0.9},
        )


def _loop(tmp_path, **overrides):
    protocol = tmp_path / "protocol.md"
    protocol.write_text("# PROTOCOL\n\n## TARGET\n\nReach 9 gates.\n", encoding="utf-8")
    config = SupervisorConfig(
        workspace=tmp_path, protocol_path=protocol, **overrides,
    )
    loop = BaseLoop(config)
    loop._feasibility_block_reason = lambda: ""
    return loop


def _standdown(action: str, *unmet):
    return SimpleNamespace(
        all_targets_met=False,
        unmet_criteria=list(unmet),
        next_action=action,
        criteria_results=[],
        feedback="",
    )


_STANDDOWN = "None authorized within this protocol instance. Targets are unreachable."


def test_jev_close_blocks_even_before_the_turn_floor(tmp_path):
    loop = _loop(tmp_path, min_turns_before_blocked=12)
    loop._task_turn = 1
    loop._jev = _StubJev("close")
    reason = loop._blocked_assessment(_standdown(_STANDDOWN, "Target 2"), "standing down")
    assert "structured closing decision is terminal" in reason
    assert any(
        note.startswith("Jev closing decision") for note in loop._jev_notes
    )
    assert loop._jev.calls and "RUN EVIDENCE" in loop._jev.calls[0]


def test_jev_continue_suppresses_the_text_triggers(tmp_path):
    loop = _loop(tmp_path, min_turns_before_blocked=12)
    loop._task_turn = 1
    loop._jev = _StubJev("continue")
    assert loop._blocked_assessment(_standdown(_STANDDOWN, "Target 2"), "working") == ""


def test_jev_grace_allows_one_closeout_turn_then_closes(tmp_path):
    loop = _loop(tmp_path, min_turns_before_blocked=12)
    loop._task_turn = 1
    loop._jev = _StubJev("grace")
    assert loop._blocked_assessment(_standdown(_STANDDOWN, "Target 2"), "closeout") == ""
    reason = loop._blocked_assessment(_standdown(_STANDDOWN, "Target 2"), "closeout done")
    assert "grace turn is spent" in reason


def test_without_jev_the_text_closure_still_works(tmp_path):
    loop = _loop(tmp_path, min_turns_before_blocked=12)
    loop._task_turn = 1
    directive = "Invoke the protocol failure condition. Close the study."
    reason = loop._blocked_assessment(_standdown(directive, "Target 2"), "")
    assert "directed the run to close the study" in reason


def test_agent_closure_is_not_delegated(tmp_path):
    loop = _loop(tmp_path, min_turns_before_blocked=12)
    loop._task_turn = 1
    loop._jev = _StubJev("continue")
    (tmp_path / "ESCALATION.md").write_text(
        "## TERMINAL RULING 2026-11-05 - CLOSED, TARGET NOT MET\n", encoding="utf-8",
    )
    reason = loop._blocked_assessment(_standdown(_STANDDOWN, "Target 2"), "")
    assert "declared this run terminally closed" in reason


# --------------------------------------------------------------------------- #
# Configuration and UI wiring                                                  #
# --------------------------------------------------------------------------- #

def test_jev_settings_reach_the_supervisor_config(tmp_path):
    from services.config.settings import DEFAULTS
    from services.config.supervisor_config_builder import build_supervisor_config

    state = dict(DEFAULTS)
    state.update({
        "enable_jev_decisions": True,
        "jev_decision_url": "http://192.168.18.65/systemone/api/v1/systemone",
        "jev_decision_api_key": "k",
        "jev_decision_timeout": 15,
        "jev_decision_threshold": 0.91,
    })
    cfg = build_supervisor_config(
        state, protocol_path=tmp_path / "protocol.md", workspace=tmp_path,
    )
    assert cfg.enable_jev_decisions is True
    assert cfg.jev_decision_url.endswith("/systemone/api/v1/systemone")
    assert cfg.jev_decision_api_key == "k"
    assert cfg.jev_decision_timeout == 15
    assert cfg.jev_decision_threshold == 0.91


def test_wizard_exposes_the_jev_controls():
    source = (_REPO / "services" / "webui" / "pages" / "wizard.py").read_text(
        encoding="utf-8",
    )
    for key in (
        "enable_jev_decisions",
        "jev_decision_url",
        "jev_decision_api_key",
        "jev_decision_timeout",
        "jev_decision_threshold",
    ):
        assert f'"{key}"' in source
    settings = (_REPO / "services" / "config" / "settings.py").read_text(
        encoding="utf-8",
    )
    for key in (
        "enable_jev_decisions",
        "jev_decision_url",
        "jev_decision_api_key",
        "jev_decision_timeout",
        "jev_decision_threshold",
    ):
        assert f'"{key}"' in settings
