"""Wizard gate loop: ai-refine must self-test its draft and edit until the
target audit passes.

Regression: "Refine with AI" returned whatever the model wrote, and the
deterministic TARGET audit only fired later at Accept time ("TARGET not
ready: … needs acceptance evidence … needs a completion state"), leaving
the user holding a failed protocol. The wizard must now audit every draft,
feed the exact findings back as an edit round, and only ever return a
draft that passes the gate (or raise ProtocolGateError).
"""

from supervisor.protocols import protocol_wizard as pw_module
from supervisor.protocols.protocol_wizard import (
    GATE_FIX_ROUNDS,
    REQUIRED_TARGET,
    ProtocolGateError,
    ProtocolWizard,
    _append_required_target,
)
from supervisor.protocols.target_audit import audit_target

BAD_DRAFT = (
    "## INPUT\n\nLegacy service.\n\n"
    "## TARGET\n\n1. Improve the code quality.\n\n"
    "## RESTRICTIONS\n\n- Do not edit files outside services/.\n"
)

GOOD_DRAFT = (
    "## INPUT\n\nLegacy service.\n\n"
    "## TARGET\n\n1. Refactor the duplicate parsers into services/common.py.\n"
    "2. Done when the full pytest suite passes with no regression.\n\n"
    "## RESTRICTIONS\n\n- Do not edit files outside services/.\n"
)


class ScriptedWizard(ProtocolWizard):
    """Refine against a scripted reply list; records every prompt sent."""

    def __init__(self, replies):
        # Skip ProtocolWizard.__init__: it builds a real OpenAI client.
        self._model = "test-model"
        self.replies = list(replies)
        self.prompts: list[str] = []

    def _chat(self, user_msg, on_progress=None, stop_event=None):
        self.prompts.append(user_msg)
        return self.replies.pop(0)


def test_appended_required_target_keeps_a_good_draft_actionable():
    from supervisor.protocols.protocol import parse_protocol_text

    md = _append_required_target(GOOD_DRAFT)
    assert md.count(REQUIRED_TARGET) == 1
    protocol = parse_protocol_text(md)
    assert audit_target(
        protocol.target_section, protocol.restrictions_section,
    ).is_actionable


def test_append_required_target_is_idempotent():
    once = _append_required_target(GOOD_DRAFT)
    twice = _append_required_target(once)
    assert twice == once


def test_refine_returns_first_passing_draft_without_edit_rounds():
    wizard = ScriptedWizard([GOOD_DRAFT])
    rounds = []

    md, protocol = wizard.refine("in", "tgt", "res", on_round=lambda *a: rounds.append(a))

    assert audit_target(
        protocol.target_section, protocol.restrictions_section,
    ).is_actionable
    assert len(wizard.prompts) == 1
    assert rounds == []
    assert REQUIRED_TARGET in md


def test_refine_feeds_audit_findings_back_and_returns_passing_draft():
    wizard = ScriptedWizard([BAD_DRAFT, GOOD_DRAFT])
    rounds = []

    _md, protocol = wizard.refine("in", "tgt", "res", on_round=lambda *a: rounds.append(a))

    assert audit_target(
        protocol.target_section, protocol.restrictions_section,
    ).is_actionable
    assert len(wizard.prompts) == 2
    # The edit round carries the exact gate findings and the rejected draft.
    assert "needs acceptance evidence" in wizard.prompts[1]
    assert "needs a completion state" in wizard.prompts[1]
    assert "Improve the code quality" in wizard.prompts[1]
    assert len(rounds) == 1
    fix_round, max_rounds, issues = rounds[0]
    assert fix_round == 1
    assert max_rounds == GATE_FIX_ROUNDS
    assert any("acceptance evidence" in issue for issue in issues)


def test_refine_feeds_parse_errors_back_too():
    wizard = ScriptedWizard(["Sure! Here is your protocol. It is great.", GOOD_DRAFT])

    _md, protocol = wizard.refine("in", "tgt", "res")

    assert audit_target(
        protocol.target_section, protocol.restrictions_section,
    ).is_actionable
    assert "protocol.md is missing" in wizard.prompts[1]


def test_refine_raises_gate_error_after_all_fix_rounds_are_spent():
    wizard = ScriptedWizard([BAD_DRAFT] * (GATE_FIX_ROUNDS + 1))

    try:
        wizard.refine("in", "tgt", "res")
    except ProtocolGateError as exc:
        assert "TARGET not ready" in str(exc)
        assert any("acceptance evidence" in issue for issue in exc.issues)
    else:
        raise AssertionError("ProtocolGateError not raised")
    # Initial draft + one edit per round, no calls beyond the budget.
    assert len(wizard.prompts) == GATE_FIX_ROUNDS + 1


def _bare_wizard() -> ProtocolWizard:
    # Real _chat without __init__ (which builds a real OpenAI client).
    wizard = ProtocolWizard.__new__(ProtocolWizard)
    wizard._client = None  # fakes never use it, but the attribute is read
    wizard._model = "test-model"
    return wizard


def test_chat_requests_low_reasoning_effort(monkeypatch):
    calls = []

    def fake_stream(client, model, messages, **kwargs):
        calls.append(kwargs)
        return "## INPUT\n\n## TARGET\n\n## RESTRICTIONS\n"

    monkeypatch.setattr(pw_module, "stream_chat_text", fake_stream)
    _bare_wizard()._chat("hello")

    assert calls[0]["extra_body"] == {"reasoning_effort": "low"}


def test_chat_falls_back_to_plain_call_when_effort_rejected(monkeypatch):
    calls = []

    def fake_stream(client, model, messages, **kwargs):
        calls.append(kwargs)
        if kwargs.get("extra_body"):
            raise RuntimeError("reasoning_effort is not supported")
        return "## INPUT\n\n## TARGET\n\n## RESTRICTIONS\n"

    monkeypatch.setattr(pw_module, "stream_chat_text", fake_stream)
    out = _bare_wizard()._chat("hello")

    assert "RESTRICTIONS" in out
    assert len(calls) == 2
    assert calls[1].get("extra_body") is None


def test_chat_reraises_cancel_and_timeout_without_fallback(monkeypatch):
    from supervisor.utils.llm_stream import GenerationCancelled

    def fake_stream(client, model, messages, **kwargs):
        raise GenerationCancelled("cancelled")

    monkeypatch.setattr(pw_module, "stream_chat_text", fake_stream)
    try:
        _bare_wizard()._chat("hello")
    except GenerationCancelled:
        pass
    else:
        raise AssertionError("GenerationCancelled should propagate")
