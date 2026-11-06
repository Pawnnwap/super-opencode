"""Regression tests: codex turn-aggregated usage must not read as context size.

run_6057fb62 died in a compaction spiral: codex ``turn.completed`` usage is
the SUM over every model request in the exec turn (verified live), but the
runner fed it raw into the context monitor -> "5,031,276/320,000 tokens
(1572%)" -> forced cleanup+summary+restart after nearly every judged turn
until the loop detector aborted the run.
"""

import json

from supervisor.runners.codex_support.stream import (
    classify_line,
    context_estimate_from_turn_usage,
)
from supervisor.runners.stream_driver import consume_process_stream


def test_estimate_with_tool_requests():
    # Probe-verified shape: 3 tool calls + final prose request, aggregated
    # input 44696 -> average request input 11174.
    assert context_estimate_from_turn_usage(44696, 3) == 11174


def test_estimate_single_request_is_exact():
    # No tool items: one request, the usage IS the context.
    assert context_estimate_from_turn_usage(44696, 0) == 44696


def test_estimate_handles_zero_and_negative():
    assert context_estimate_from_turn_usage(0, 5) == 0
    assert context_estimate_from_turn_usage(-1, 5) == 0


class _Proc:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.stderr = None
        self.returncode = 0

    def kill(self):
        pass

    terminate = kill

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


def _consume(lines):
    gen = consume_process_stream(_Proc(lines), classify_line=classify_line, timeout=5)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def _consume_events(lines):
    gen = consume_process_stream(_Proc(lines), classify_line=classify_line, timeout=5)
    events = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            return events, stop.value


def test_stream_outcome_counts_tools_and_keeps_aggregated_usage():
    turn = [
        json.dumps({"type": "thread.started", "thread_id": "t-1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "echo one",
            "aggregated_output": "one", "exit_code": 0}}),
        json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "echo two",
            "aggregated_output": "two", "exit_code": 0}}),
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "DONE-123"}}),
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 44696, "cached_input_tokens": 32768,
            "output_tokens": 174}}),
    ]
    outcome = _consume([line + "\n" for line in turn])
    assert outcome.tool_count == 2  # tool items only, prose not counted
    assert outcome.tokens_total == 44696 + 174
    # The runner-facing conversion divides by tool_count + 1.
    assert context_estimate_from_turn_usage(
        outcome.tokens_total, outcome.tool_count,
    ) == (44696 + 174) // 3


def test_reasoning_is_logged_as_agent_thought_but_not_judge_output():
    turn = [
        json.dumps({"type": "item.completed", "item": {
            "type": "reasoning", "text": "I should check the gate table first."}}),
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "GATE OK"}}),
    ]

    events, outcome = _consume_events([line + "\n" for line in turn])

    assert [event for event in events if event.get("level") == "agent_thought"] == [
        {"level": "agent_thought", "msg": "I should check the gate table first."},
    ]
    # The judge-facing output must contain only the agent message.
    assert outcome.stdout == "GATE OK"
    assert outcome.prose == "GATE OK"
    assert outcome.tool_count == 0
