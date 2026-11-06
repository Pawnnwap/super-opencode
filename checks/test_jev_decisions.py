"""Structured decision service (jev): client, policy, and closing wiring."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from supervisor.core import unblock_rescue
from supervisor.core.decision_service import (
    CHOICE_MIN_CONFIDENCE,
    CLOSE_ACTIONS,
    ChoiceDecision,
    CloseDecision,
    JevDecisionClient,
    build_close_evidence,
)
from supervisor.core.llm_support.models import CriterionResult, SupervisorVerdict
from supervisor.core.loop_base import BaseLoop, LoopState
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
        "enable_blocked_rescue": False,
    })
    cfg = build_supervisor_config(
        state, protocol_path=tmp_path / "protocol.md", workspace=tmp_path,
    )
    assert cfg.enable_jev_decisions is True
    assert cfg.enable_blocked_rescue is False
    assert cfg.max_blocked_rescues == 1
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
        "enable_blocked_rescue",
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
        "enable_blocked_rescue",
    ):
        assert f'"{key}"' in settings


# --------------------------------------------------------------------------- #
# First-blocked rescue: web-researched plan + jev feasibility ruling            #
# --------------------------------------------------------------------------- #


def test_decide_unblock_plan_parses_the_feasibility_choice():
    client = _client(_choice_answer("verdict", feasible=0.85, not_feasible=0.15))
    decision = client.decide_unblock_plan(plan="1. do x", evidence="e")
    assert decision is not None
    assert decision.action == "feasible"
    assert decision.probability == pytest.approx(0.85)


def test_decide_unblock_plan_malformed_response_returns_none():
    assert _client({"answers": {"verdict": {}}}).decide_unblock_plan(
        plan="p", evidence="e",
    ) is None


def test_unblock_review_requires_an_executor_audit():
    seen: list[dict] = []

    def _transport(payload):
        seen.append(payload)
        return _choice_answer("verdict", feasible=0.9, not_feasible=0.1)

    client = JevDecisionClient(
        "http://decision.test/systemone", transport=_transport,
    )
    client.decide_unblock_plan(plan="p", evidence="e")

    system = seen[0]["state"][0]["content"]
    criteria = seen[0]["questions"]["verdict"]["criteria"]
    assert "audits the executor's record" in system
    assert "factual errors" in criteria["feasible"]
    assert "ignores audit findings" in criteria["not_feasible"]


def test_parse_search_queries_reads_json_and_dedupes():
    raw = (
        'SEARCH_QUERIES: ["order flow imbalance", "order flow imbalance", '
        '"gate calibration"]'
    )
    assert unblock_rescue.parse_search_queries(raw) == [
        "order flow imbalance",
        "gate calibration",
    ]


def test_parse_search_queries_falls_back_to_quoted_text():
    raw = 'Try "subsecond order flow method" and "gate ceiling calibration".'
    assert unblock_rescue.parse_search_queries(raw) == [
        "subsecond order flow method",
        "gate ceiling calibration",
    ]


def test_plan_is_usable_rejects_empty_and_refusal_shaped_replies():
    assert not unblock_rescue.plan_is_usable("")
    assert not unblock_rescue.plan_is_usable("I cannot produce a recovery plan.")
    assert unblock_rescue.plan_is_usable(
        "1. Diagnose the failed gate mechanism in tools/gates.py.\n"
        "2. Use a different calibration method with a rolling window.\n"
    )


def test_gather_web_evidence_formats_results_and_caps_queries():
    calls: list[str] = []

    def _search(query: str):
        calls.append(query)
        return [{
            "title": "Method paper",
            "url": "https://example.test/paper",
            "snippet": "A calibration method.",
        }]

    evidence, executed = unblock_rescue.gather_web_evidence(
        ["q1", "q2", "q3"], search=_search,
    )

    assert executed == ["q1", "q2"]
    assert calls == ["q1", "q2"]
    assert "QUERY: q1" in evidence
    assert "https://example.test/paper" in evidence


def test_fallback_queries_cleans_the_target():
    assert unblock_rescue.fallback_queries("Reach 9 AB gates: T4!!", "") == [
        "Reach 9 AB gates T4",
    ]


def _audit_verdict() -> SimpleNamespace:
    return SimpleNamespace(
        feedback="stuck",
        next_action="None",
        criteria_results=[],
        unmet_criteria=[],
    )


def test_rescue_query_prompt_targets_the_failure_mechanism():
    prompt = unblock_rescue.build_query_prompt(
        target="Reach 9 AB gates.",
        restrictions="none",
        reason="3 stagnant turns",
        verdict=_audit_verdict(),
        report="LAST OUTPUT:\nstanding down",
    )

    assert "EXECUTOR RECORD" in prompt
    assert "At least one query must test the suspected failure mechanism" in prompt
    assert "may misreport results" in prompt


def test_rescue_plan_prompt_demands_an_executor_audit():
    prompt = unblock_rescue.build_plan_prompt(
        target="Reach 9 AB gates.",
        restrictions="none",
        reason="3 stagnant turns",
        verdict=_audit_verdict(),
        report="LAST OUTPUT:\nstanding down",
        experience="",
        web_evidence="QUERY: x\n  (no results)",
    )

    assert "EXECUTOR RECORD" in prompt
    for phrase in (
        "Factual errors",
        "Arbitrary decisions",
        "Laziness / premature give-up",
        "SEARCH QUALITY CAVEAT",
        "Do not lower the target",
        "Failure audit",
        "Search caveat",
    ):
        assert phrase in prompt


def test_executor_record_includes_summary_and_journal():
    record = unblock_rescue.build_executor_record(
        output="last output", summary="summary text", journal="journal text",
    )

    assert "LAST OUTPUT" in record
    assert "SUMMARY.MD" in record
    assert "RECENT JUDGE JOURNAL" in record


def test_executor_prompt_blocks_arbitrary_scope_change():
    prompt = unblock_rescue.build_executor_prompt(
        reason="3 stagnant turns",
        plan="1. audit then retry with a different method",
        plan_path=".opencode/unblock_plan.md",
    )

    assert "verify the plan's failure audit" in prompt
    assert "Do not lower the target" in prompt
    assert "do not assert it" in prompt


def test_max_blocked_rescues_is_backend_only():
    builder = (
        _REPO / "services" / "config" / "supervisor_config_builder.py"
    ).read_text(encoding="utf-8")
    settings = (_REPO / "services" / "config" / "settings.py").read_text(
        encoding="utf-8",
    )
    wizard = (_REPO / "services" / "webui" / "pages" / "wizard.py").read_text(
        encoding="utf-8",
    )

    assert 'session_state.get("max_blocked_rescues"' not in builder
    assert "max_blocked_rescues=int(" not in builder
    assert "max_blocked_rescues" not in settings
    assert "max_blocked_rescues" not in wizard


_PLAN = (
    "1. Diagnosis: the previous approach only re-tuned the same gate threshold.\n"
    "2. New approach: use a rolling-window rank gate instead "
    "(https://example.test/method).\n"
    "3. Steps: rewrite tools/gates.py and run tools/gate_check.py --window 20.\n"
    "4. Acceptance evidence: 9/9 gates pass and the gate report exits 0.\n"
    "5. Stop conditions: two runs below 8/9 gates mean the method is unsuitable.\n"
)


class _RescueSupervisor:
    def __init__(self, plan: str = _PLAN):
        self.plan = plan
        self.prompts: list[str] = []

    def _chat(self, prompt, **_kwargs):
        self.prompts.append(prompt)
        if "WEB RESEARCH QUERY PLANNING" in prompt:
            raw = 'SEARCH_QUERIES: ["factor gate calibration", "alpha gate ceiling"]'
        elif "RECOVERY PLAN REQUIRED" in prompt:
            raw = self.plan
        else:
            raw = "Final report."
        return SimpleNamespace(raw=raw)

    def _build_experience_context(self):
        return ""

    def report_final_status(self, reason, opencode_output):
        return "Final report."


class _RescueRunner:
    def __init__(self):
        self.sent: list[str] = []

    def get_step_progress(self):
        return SimpleNamespace(
            current_step=1,
            total_steps_estimate=5,
            phase=SimpleNamespace(name="coding"),
            completed_phases=[],
        )

    def send(self, message: str):
        self.sent.append(message)
        return iter(())

    def is_session_pinned(self):
        return True


class _RescueJev:
    def __init__(self, action: str, probability: float = 0.9):
        self.action = action
        self.probability = probability
        self.calls: list[dict] = []

    def decide_unblock_plan(self, *, plan, evidence):
        self.calls.append({"plan": plan, "evidence": evidence})
        return ChoiceDecision(action=self.action, probability=self.probability)


def _blocked_verdict() -> SupervisorVerdict:
    return SupervisorVerdict(
        raw="CRITERIA:\n- [UNMET] T2 — gates 7/10\nNEXT_ACTION: None",
        all_targets_met=False,
        feedback="stuck",
        criteria_results=[CriterionResult("T2 AB >= 9/10 gates", met=False)],
        next_action="None",
    )


def _passthrough_feedback(feedback):
    """Generator matching _sanitize_feedback's ``return safe_msg`` contract."""
    yield from ()
    return feedback


def _rescue_loop(
    tmp_path,
    *,
    action: str = "feasible",
    probability: float = 0.9,
    plan: str = _PLAN,
    **overrides,
):
    protocol = tmp_path / "protocol.md"
    protocol.write_text(
        "# PROTOCOL\n\n## TARGET\n\nReach 9 AB gates.\n", encoding="utf-8",
    )
    config = SupervisorConfig(
        workspace=tmp_path,
        protocol_path=protocol,
        enable_jev_decisions=True,
        jev_decision_url="http://decision.test/systemone",
        **overrides,
    )
    loop = BaseLoop(config)
    loop.protocol = SimpleNamespace(
        target_section="Reach 9 AB gates.",
        restrictions_section="## RESTRICTIONS\nnone",
    )
    loop.runner = _RescueRunner()
    loop.supervisor = _RescueSupervisor(plan)
    loop._jev = _RescueJev(action, probability)
    loop._sanitize_feedback = _passthrough_feedback
    loop._blocked_assessment = lambda verdict, output="": (
        "3 consecutive judged turns attained nothing new: "
        "T2 AB >= 9/10 gates"
    )
    return loop


def _run_rescue_judgement(loop, monkeypatch):
    monkeypatch.setattr(
        unblock_rescue,
        "gather_web_evidence",
        lambda queries: (f"QUERY: {queries[0]}\n  1. Result", list(queries)),
    )
    loop._get_verdict = lambda output, progress: _blocked_verdict()
    return list(loop._do_judgement("agent output"))


def test_first_blocked_rescue_sends_the_approved_plan(tmp_path, monkeypatch):
    loop = _rescue_loop(tmp_path)

    events = _run_rescue_judgement(loop, monkeypatch)

    assert loop._state is LoopState.RUNNING
    assert loop.runner.sent
    assert "UNBLOCK DIRECTIVE" in loop.runner.sent[0]
    assert loop._blocked_rescues_used == 1
    assert loop._progress.streak == 0
    assert (tmp_path / ".opencode" / "unblock_plan.md").exists()
    assert not (tmp_path / "blocked_report.md").exists()
    assert loop._jev.calls[0]["plan"] == _PLAN.strip()
    assert "BLOCKED RUN UNBLOCK EVIDENCE" in loop._jev.calls[0]["evidence"]
    assert any("Blocked exit cancelled" in event["msg"] for event in events)
    assert any("Jev unblock review" in event["msg"] for event in events)


def test_rejected_rescue_closes_as_blocked(tmp_path, monkeypatch):
    loop = _rescue_loop(tmp_path, action="not_feasible")

    events = _run_rescue_judgement(loop, monkeypatch)

    assert loop._state is LoopState.ENDED_BLOCKED
    assert not loop.runner.sent
    assert (tmp_path / "blocked_report.md").exists()
    assert any("Unblock plan rejected" in event["msg"] for event in events)


def test_low_confidence_feasible_does_not_cancel_the_block(tmp_path, monkeypatch):
    loop = _rescue_loop(
        tmp_path, action="feasible", probability=CHOICE_MIN_CONFIDENCE - 0.1,
    )

    _run_rescue_judgement(loop, monkeypatch)

    assert loop._state is LoopState.ENDED_BLOCKED
    assert not loop.runner.sent


def test_jev_outage_during_rescue_fails_safe_to_blocked(tmp_path, monkeypatch):
    class _BoomJev:
        def decide_unblock_plan(self, **_kwargs):
            raise TimeoutError("decision service down")

    loop = _rescue_loop(tmp_path)
    loop._jev = _BoomJev()

    events = _run_rescue_judgement(loop, monkeypatch)

    assert loop._state is LoopState.ENDED_BLOCKED
    assert not loop.runner.sent
    assert any("unavailable" in event["msg"] for event in events)


def test_rescue_budget_is_a_single_attempt(tmp_path, monkeypatch):
    loop = _rescue_loop(tmp_path)
    loop._blocked_rescues_used = 1

    _run_rescue_judgement(loop, monkeypatch)

    assert loop._state is LoopState.ENDED_BLOCKED
    assert not loop.runner.sent
    assert not any(
        "RECOVERY PLAN REQUIRED" in prompt for prompt in loop.supervisor.prompts
    )


def test_rescue_without_jev_is_inert(tmp_path, monkeypatch):
    loop = _rescue_loop(tmp_path)
    loop._jev = None

    _run_rescue_judgement(loop, monkeypatch)

    assert loop._state is LoopState.ENDED_BLOCKED
    assert not loop.runner.sent
    assert not any(
        "RECOVERY PLAN REQUIRED" in prompt for prompt in loop.supervisor.prompts
    )


def test_rescue_can_be_disabled_in_config(tmp_path, monkeypatch):
    loop = _rescue_loop(tmp_path, enable_blocked_rescue=False)

    _run_rescue_judgement(loop, monkeypatch)

    assert loop._state is LoopState.ENDED_BLOCKED
    assert not loop.runner.sent
