"""The supervisor test gate must fail closed on absent or invalid evidence."""

from __future__ import annotations

import pytest

from supervisor.core.goal_guard import GoalGuard
from supervisor.core.llm_support.models import CriterionResult, SupervisorVerdict
from supervisor.protocols.protocol import parse_protocol_text
from supervisor.runners.test_runner import RunTestResult
from supervisor.utils.config import SupervisorConfig

_PROTOCOL = "## INPUT\nx\n## TARGET\n1. must_pass\n## RESTRICTIONS\nnone\n"


@pytest.mark.parametrize(
    ("output", "exit_code"),
    [
        ("no tests ran in 0.01s", 5),
        ("KeyboardInterrupt", 2),
        ("", -1),
        ("", 0),
        ("not a pytest summary", 0),
    ],
)
def test_test_result_requires_real_successful_evidence(output, exit_code):
    result = RunTestResult(
        passed=0,
        failed=0,
        errors=0,
        duration_s=0.01,
        output=output,
        exit_code=exit_code,
    )

    assert result.ok is False


def test_successful_parsed_pytest_result_passes():
    result = RunTestResult(
        passed=3,
        failed=0,
        errors=0,
        duration_s=0.2,
        output="3 passed in 0.20s",
        exit_code=0,
        summary_parsed=True,
    )

    assert result.ok is True


def test_unparsed_counts_cannot_pass():
    result = RunTestResult(
        passed=3,
        failed=0,
        errors=0,
        duration_s=0.2,
        output="made-up counts",
        exit_code=0,
        summary_parsed=False,
    )

    assert result.ok is False


def test_done_is_blocked_when_pytest_collects_nothing(tmp_path, monkeypatch):
    protocol_path = tmp_path / "protocol.md"
    protocol_path.write_text(_PROTOCOL, encoding="utf-8")
    (tmp_path / "deliverable.py").write_text("x = 1\n", encoding="utf-8")
    guard = GoalGuard(
        SupervisorConfig(
            protocol_path=protocol_path,
            workspace=tmp_path,
            goal_verify_tests=True,
        ),
        parse_protocol_text(_PROTOCOL),
    )

    class _NoTestsRunner:
        def __init__(self, _workspace):
            pass

        def run(self):
            return RunTestResult(
                passed=0,
                failed=0,
                errors=0,
                duration_s=0.01,
                output="no tests ran in 0.01s",
                exit_code=5,
            )

    monkeypatch.setattr("supervisor.runners.test_runner.OcTestRunner", _NoTestsRunner)

    decision = guard.evaluate_done(
        SupervisorVerdict(
            raw="DONE: yes",
            all_targets_met=True,
            feedback="",
            criteria_results=[CriterionResult("must_pass", True, "ok")],
        ),
        changed_files=["deliverable.py"],
    )

    assert decision.accepted is False
    assert "test gate failed" in decision.reason
