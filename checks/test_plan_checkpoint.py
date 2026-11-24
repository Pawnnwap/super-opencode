"""A failed plan judge must be replayable without re-running the plan turn."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import InternalServerError

from supervisor.core.llm_support.chat import (
    chat,
    judge_checkpoint,
    load_judge_checkpoint,
    resume_chat,
)
from supervisor.core.llm_support.judgement import judge_plan
from supervisor.core.llm_support.models import SupervisorVerdict


def _success():
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="DONE: no\nfeedback: ok"),
            ),
        ],
    )


def _chat_supervisor(workspace, create):
    return SimpleNamespace(
        _system="system",
        _history=[],
        _last_opencode_output=None,
        _reuse_identical_verdicts=False,
        _last_verdict=None,
        _last_judged_content=None,
        _max_tokens=100000,
        _token_warnings=[],
        _truncation_enabled=False,
        _history_verbatim_turns=1,
        _history_budget_fraction=0.35,
        _log_prompt=lambda *_a, **_k: None,
        _client=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        ),
        _model="primary",
        _model_backup=None,
        _reasoning_body=None,
        _max_history_turns=40,
        _compact_intermediate_steps=False,
        _extract_and_store_opencode_output=lambda *_a, **_k: None,
        _workspace=workspace,
    )


def _checkpoint_path(tmp_path):
    return tmp_path / ".opencode" / "judge_pending.json"


def _write_checkpoint(tmp_path, **overrides):
    payload = {
        "schema": 1,
        "phase": "plan",
        "round": 1,
        "total": 2,
        "output": "plan output",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "plan prompt"},
        ],
        "history": [],
        "record_content": "plan prompt",
        "user_content": "plan prompt",
        "should_record_user": True,
    }
    payload.update(overrides)
    path = _checkpoint_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def test_chat_writes_checkpoint_before_call_and_clears_on_success(tmp_path):
    observed: dict = {}

    def create(**_kwargs):
        path = _checkpoint_path(tmp_path)
        observed["exists"] = path.is_file()
        observed["payload"] = load_judge_checkpoint(tmp_path)
        observed["raw"] = path.read_text(encoding="utf-8")
        return _success()

    supervisor = _chat_supervisor(tmp_path, create)
    with judge_checkpoint(
        {
            "phase": "plan",
            "round": 1,
            "total": 2,
            "output": "plan output",
        }
    ):
        chat(supervisor, "plan prompt")

    assert observed["exists"] is True
    payload = observed["payload"]
    assert payload["phase"] == "plan"
    assert payload["round"] == 1
    assert payload["output"] == "plan output"
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][-1] == {"role": "user", "content": "plan prompt"}
    assert payload["record_content"] == "plan prompt"
    assert "plan prompt" not in observed["raw"]
    assert not _checkpoint_path(tmp_path).exists()


def test_failed_chat_leaves_checkpoint_for_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "supervisor.core.llm_support.chat.time.sleep",
        lambda _s: None,
    )
    request = httpx.Request("POST", "http://endpoint/v1/chat/completions")
    response = httpx.Response(502, request=request, text="bad")

    def create(**_kwargs):
        raise InternalServerError("Error code: 502", response=response, body=None)

    supervisor = _chat_supervisor(tmp_path, create)
    with (
        pytest.raises(InternalServerError),
        judge_checkpoint(
            {
                "phase": "plan",
                "round": 1,
                "total": 2,
                "output": "plan output",
            }
        ),
    ):
        chat(supervisor, "plan prompt")

    payload = load_judge_checkpoint(tmp_path)
    assert payload is not None
    assert payload["messages"][-1] == {"role": "user", "content": "plan prompt"}
    assert payload["output"] == "plan output"


def test_resume_chat_consumes_checkpoint_and_restores_history(tmp_path):
    _write_checkpoint(
        tmp_path,
        history=[{"role": "user", "content": "earlier plan round"}],
    )
    calls: list[int] = []

    def create(**_kwargs):
        calls.append(1)
        return _success()

    supervisor = _chat_supervisor(tmp_path, create)
    loaded = load_judge_checkpoint(tmp_path)
    assert loaded is not None
    verdict = resume_chat(supervisor, loaded)

    assert len(calls) == 1
    assert verdict is not None
    assert supervisor._last_verdict is verdict
    assert supervisor._history[0] == {
        "role": "user",
        "content": "earlier plan round",
    }
    assert supervisor._history[1] == {"role": "user", "content": "plan prompt"}
    assert not _checkpoint_path(tmp_path).exists()


def test_load_ignores_corrupt_and_unsupported_checkpoints(tmp_path):
    path = _checkpoint_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_judge_checkpoint(tmp_path) is None

    path.write_text(json.dumps({"schema": 1, "messages": []}), encoding="utf-8")
    assert load_judge_checkpoint(tmp_path) is None

    path.write_text(
        json.dumps({"schema": 1, "messages": [{"role": "user", "content": "x"}]}),
        encoding="utf-8",
    )
    assert load_judge_checkpoint(tmp_path) is not None


def test_resume_flag_reads_the_checkpoint(tmp_path):
    from services.jobs.job_manager import _resume_available

    config = SimpleNamespace(workspace=tmp_path)
    assert _resume_available(config) is False
    _write_checkpoint(tmp_path)
    assert _resume_available(config) is True


def test_judge_plan_requests_a_checkpoint_with_round_and_output():
    seen: dict = {}

    def fake_chat(msg, history_content=None):
        from supervisor.core.llm_support import chat as chat_mod

        seen["meta"] = chat_mod._CHECKPOINT_META.get()
        return SupervisorVerdict(
            raw="DONE: no\nfeedback: ok",
            all_targets_met=False,
            feedback="ok",
        )

    supervisor = SimpleNamespace(
        _get_evaluation_context=lambda: ("", ""),
        _build_experience_context=lambda: "",
        _chat=fake_chat,
    )
    verdict = judge_plan(supervisor, "plan output", 2, 2)

    assert seen["meta"] == {
        "phase": "plan",
        "round": 2,
        "total": 2,
        "output": "plan output",
    }
    assert verdict.all_targets_met is False
