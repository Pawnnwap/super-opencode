"""Judge failures must leave a replayable sidecar and a classified cause."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, InternalServerError

from supervisor.core.llm_support.chat import chat_with_retry, judge_phase


def _supervisor(create, workspace):
    return SimpleNamespace(
        _model="primary",
        _model_backup=None,
        _client=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        ),
        _history=[],
        _reasoning_body={"reasoning_effort": "xhigh"},
        _max_history_turns=40,
        _compact_intermediate_steps=False,
        _extract_and_store_opencode_output=lambda *_a, **_k: None,
        _workspace=workspace,
    )


def _request() -> httpx.Request:
    return httpx.Request("POST", "http://endpoint/v1/chat/completions")


def _502() -> InternalServerError:
    response = httpx.Response(
        502,
        request=_request(),
        headers={
            "server": "front-proxy",
            "content-type": "text/html",
            "x-request-id": "req-123",
        },
        text="<html>bad gateway</html>",
    )
    return InternalServerError("Error code: 502", response=response, body=None)


def _sidecar(tmp_path):
    files = list((tmp_path / ".opencode" / "judge_failures").glob("*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_502_captures_status_headers_body_and_request(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(
        "supervisor.core.llm_support.chat.time.sleep", lambda _s: None,
    )

    def create(**_kwargs):
        raise _502()

    supervisor = _supervisor(create, tmp_path)
    with caplog.at_level("ERROR"), pytest.raises(InternalServerError) as info:
        chat_with_retry(
            supervisor,
            [{"role": "user", "content": "hello"}],
            "record",
            False,
        )

    assert info.value.failure_context["failure_class"] == "provider_5xx"
    assert "req-123" in caplog.text
    assert "front-proxy" in caplog.text
    assert "bad gateway" in caplog.text

    payload = _sidecar(tmp_path)
    assert payload["failure_class"] == "provider_5xx"
    assert payload["status_code"] == 502
    assert payload["request_id"] == "req-123"
    assert payload["headers"]["server"] == "front-proxy"
    assert "bad gateway" in payload["body_excerpt"]
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert payload["prompt_chars"] == 5
    assert payload["attempt"] == 4  # initial + three 5xx retries
    assert payload["elapsed_s"] >= 0


def test_timeout_sidecar_records_attempt_and_class(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "supervisor.core.llm_support.chat.time.sleep", lambda _s: None,
    )
    calls: list[int] = []

    def create(**_kwargs):
        calls.append(1)
        raise APITimeoutError(request=_request())

    supervisor = _supervisor(create, tmp_path)
    with pytest.raises(APITimeoutError):
        chat_with_retry(supervisor, [], "record", False)

    assert len(calls) == 2  # one quick retry, then fail
    payload = _sidecar(tmp_path)
    assert payload["failure_class"] == "provider_timeout"
    assert payload["attempt"] == 2
    assert payload["status_code"] is None
    assert payload["elapsed_s"] >= 0


def test_sidecar_records_plan_phase(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(
        "supervisor.core.llm_support.chat.time.sleep", lambda _s: None,
    )

    def create(**_kwargs):
        raise _502()

    supervisor = _supervisor(create, tmp_path)
    with judge_phase("plan", 2, 2), pytest.raises(InternalServerError):
        chat_with_retry(supervisor, [], "record", False)

    payload = _sidecar(tmp_path)
    assert payload["phase"] == "plan"
    assert payload["round"] == 2
    assert payload["total_rounds"] == 2


def test_success_writes_no_failure_sidecar(tmp_path):
    def create(**_kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="DONE: no\nfeedback: ok"),
                ),
            ],
        )

    supervisor = _supervisor(create, tmp_path)
    chat_with_retry(supervisor, [], "record", False)
    assert not (tmp_path / ".opencode" / "judge_failures").exists()