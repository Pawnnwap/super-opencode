"""Judge waits and retries must stop promptly when a run is cancelled."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from openai import RateLimitError

from supervisor.core.llm_support import chat as chat_mod
from supervisor.core.llm_support import judge_gate


def _supervisor(create, tmp_path):
    return SimpleNamespace(
        _model="primary",
        _model_backup=None,
        _client=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        ),
        _history=[],
        _reasoning_body=None,
        _max_history_turns=40,
        _compact_intermediate_steps=False,
        _extract_and_store_opencode_output=lambda *_a, **_k: None,
        _workspace=tmp_path,
    )


def test_judge_slot_wait_is_cancellable():
    cancel = threading.Event()
    entered = threading.Event()
    errors = []

    judge_gate.reset_for_tests(1)

    def worker():
        entered.set()
        try:
            with judge_gate.judge_slot(cancel_event=cancel):
                pytest.fail("cancelled waiter acquired the slot")
        except Exception as exc:  # noqa: BLE001 - assertion records the raised type
            errors.append(exc)

    with judge_gate.judge_slot():
        thread = threading.Thread(target=worker)
        thread.start()
        assert entered.wait(1)
        cancel.set()
        thread.join(2)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], judge_gate.JudgeCancelled)


def test_retry_backoff_is_cancellable(tmp_path, monkeypatch):
    cancel = threading.Event()
    calls = []

    def create(**_kwargs):
        calls.append(1)
        cancel.set()
        raise RateLimitError(
            "rate limited",
            response=httpx.Response(
                429,
                request=httpx.Request("POST", "http://endpoint"),
            ),
            body=None,
        )

    supervisor = _supervisor(create, tmp_path)
    supervisor._cancel_event = cancel
    monkeypatch.setattr(chat_mod.time, "sleep", lambda _seconds: None)

    with pytest.raises(chat_mod.JudgeCancelled):
        chat_mod.chat_with_retry(supervisor, [], "record", False)

    assert len(calls) == 1


def test_slow_completion_does_not_block_cancellation(tmp_path):
    cancel = threading.Event()
    release = threading.Event()

    def create(**_kwargs):
        release.wait(2)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="DONE: no"))],
        )

    supervisor = _supervisor(create, tmp_path)
    supervisor._cancel_event = cancel
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(chat_mod.JudgeCancelled):
            chat_mod.chat_with_retry(supervisor, [], "record", False)
        assert time.monotonic() - started < 1
    finally:
        release.set()
        timer.cancel()
