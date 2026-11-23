"""The judge gate bounds concurrent HTTP calls without serialising the farm."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError

from supervisor.core.llm_support import judge_gate


@pytest.fixture(autouse=True)
def _isolate_gate(monkeypatch):
    monkeypatch.delenv(judge_gate.ENV_VAR, raising=False)
    judge_gate.reset_for_tests(4)
    yield
    judge_gate.reset_for_tests(4)


def test_cap_limits_concurrent_holders():
    judge_gate.reset_for_tests(2)
    active = 0
    peak = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal active, peak
        with judge_gate.judge_slot():
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert peak == 2
    assert active == 0


def test_env_limit_is_read_on_first_use(monkeypatch):
    monkeypatch.setenv(judge_gate.ENV_VAR, "1")
    judge_gate.reset_for_tests()
    entered = threading.Event()

    def worker() -> None:
        with judge_gate.judge_slot():
            entered.set()

    with judge_gate.judge_slot():
        thread = threading.Thread(target=worker)
        thread.start()
        assert not entered.wait(0.2)
    assert entered.wait(2.0)
    thread.join(timeout=2.0)


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(judge_gate.ENV_VAR, "not-a-number")
    judge_gate.reset_for_tests()
    assert judge_gate._configured_limit() == judge_gate.DEFAULT_LIMIT


def test_each_chat_attempt_holds_a_slot(monkeypatch, tmp_path):
    from supervisor.core.llm_support import chat as chat_mod

    entered: list[int] = []

    class Slot:
        def __enter__(self):
            entered.append(1)
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(chat_mod, "judge_slot", lambda: Slot())
    monkeypatch.setattr(chat_mod.time, "sleep", lambda _s: None)

    def create(**_kwargs):
        raise APITimeoutError(request=httpx.Request("POST", "http://endpoint"))

    supervisor = SimpleNamespace(
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
    with pytest.raises(APITimeoutError):
        chat_mod.chat_with_retry(supervisor, [], "record", False)

    assert len(entered) == 2