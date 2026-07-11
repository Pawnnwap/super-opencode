from __future__ import annotations

import json

from supervisor.memory_policy_evaluator import (
    MemoryEvaluationCase,
    MemoryPolicyEvaluator,
    MemoryTaskResult,
    load_memory_evaluation_suite,
)
from supervisor.runners.test_runner import RunTestResult


CASES = (
    MemoryEvaluationCase("config", "preserve user config", ("config.toml",)),
    MemoryEvaluationCase("archive", "restore workspace archive", ("workspace_archiver.py",)),
)


def _policy(case: MemoryEvaluationCase) -> str:
    return case.goal


def test_policy_gate_accepts_higher_success_without_regressions():
    evaluator = MemoryPolicyEvaluator(CASES)

    def run(case: MemoryEvaluationCase, memory: str) -> MemoryTaskResult:
        if memory == "baseline":
            return MemoryTaskResult(success=case.task_id == "config", tokens=20, elapsed_s=2)
        return MemoryTaskResult(success=True, tokens=30, elapsed_s=3)

    verdict = evaluator.evaluate(
        baseline_policy=lambda case: "baseline",
        candidate_policy=_policy,
        task_runner=run,
    )

    assert verdict.accepted
    assert verdict.candidate.successes == 2


def test_policy_gate_rejects_code_test_regression():
    evaluator = MemoryPolicyEvaluator(CASES)
    baseline_tests = RunTestResult(10, 0, 0, 1, "", 0)
    candidate_tests = RunTestResult(10, 1, 0, 1, "", 1)

    verdict = evaluator.evaluate(
        baseline_policy=_policy,
        candidate_policy=_policy,
        task_runner=lambda case, memory: MemoryTaskResult(success=True, tokens=10, elapsed_s=1),
        baseline_tests=baseline_tests,
        candidate_tests=candidate_tests,
    )

    assert not verdict.accepted
    assert verdict.reason == "candidate introduced a code-test regression"


def test_policy_suite_loader_requires_fixed_valid_cases(tmp_path):
    suite = tmp_path / "memory_suite.json"
    suite.write_text(
        json.dumps({"cases": [{"task_id": "config", "goal": "preserve config", "files": ["config.toml"]}]}),
        encoding="utf-8",
    )

    cases = load_memory_evaluation_suite(suite)

    assert cases == (MemoryEvaluationCase("config", "preserve config", ("config.toml",)),)
