"""Fixed-suite gate for accepting or rejecting durable-memory policies."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from supervisor.runners.test_runner import RunTestResult


@dataclass(frozen=True)
class MemoryEvaluationCase:
    """Immutable coding task used for every policy comparison."""

    task_id: str
    goal: str
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryTaskResult:
    """Observed outcome of one policy on one fixed task."""

    success: bool
    regressions: int = 0
    tokens: int = 0
    elapsed_s: float = 0.0
    note: str = ""


@dataclass(frozen=True)
class MemoryPolicyScore:
    successes: int
    regressions: int
    tokens: int
    elapsed_s: float
    results: tuple[MemoryTaskResult, ...]


@dataclass(frozen=True)
class MemoryPolicyVerdict:
    accepted: bool
    reason: str
    baseline: MemoryPolicyScore
    candidate: MemoryPolicyScore


MemoryPolicy = Callable[[MemoryEvaluationCase], str]
MemoryTaskRunner = Callable[[MemoryEvaluationCase, str], MemoryTaskResult]


class MemoryPolicyEvaluator:
    """Compare candidate memory policy against baseline on unchanged cases."""

    def __init__(self, cases: Iterable[MemoryEvaluationCase]):
        self._cases = tuple(cases)
        if not self._cases:
            raise ValueError("memory evaluation suite cannot be empty")
        ids = [case.task_id for case in self._cases]
        if any(not task_id for task_id in ids) or len(set(ids)) != len(ids):
            raise ValueError("memory evaluation task IDs must be non-empty and unique")

    @property
    def cases(self) -> tuple[MemoryEvaluationCase, ...]:
        return self._cases

    def evaluate(
        self,
        *,
        baseline_policy: MemoryPolicy,
        candidate_policy: MemoryPolicy,
        task_runner: MemoryTaskRunner,
        baseline_tests: RunTestResult | None = None,
        candidate_tests: RunTestResult | None = None,
    ) -> MemoryPolicyVerdict:
        baseline = self._score(baseline_policy, task_runner)
        candidate = self._score(candidate_policy, task_runner)

        if (
            baseline_tests is not None
            and candidate_tests is not None
            and candidate_tests.is_regression_vs(baseline_tests)
        ):
            return MemoryPolicyVerdict(
                accepted=False,
                reason="candidate introduced a code-test regression",
                baseline=baseline,
                candidate=candidate,
            )
        if candidate.regressions > baseline.regressions:
            return MemoryPolicyVerdict(
                accepted=False,
                reason="candidate increased task regressions",
                baseline=baseline,
                candidate=candidate,
            )
        if candidate.successes > baseline.successes:
            return MemoryPolicyVerdict(
                accepted=True,
                reason="candidate improved fixed-suite task success without regressions",
                baseline=baseline,
                candidate=candidate,
            )
        if candidate.successes < baseline.successes:
            return MemoryPolicyVerdict(
                accepted=False,
                reason="candidate reduced fixed-suite task success",
                baseline=baseline,
                candidate=candidate,
            )

        no_more_expensive = (
            candidate.tokens <= baseline.tokens
            and candidate.elapsed_s <= baseline.elapsed_s
        )
        cheaper = (
            candidate.tokens < baseline.tokens
            or candidate.elapsed_s < baseline.elapsed_s
        )
        if no_more_expensive and cheaper:
            return MemoryPolicyVerdict(
                accepted=True,
                reason="candidate matched task quality with lower fixed-suite cost",
                baseline=baseline,
                candidate=candidate,
            )
        return MemoryPolicyVerdict(
            accepted=False,
            reason="candidate did not improve quality or fixed-suite cost",
            baseline=baseline,
            candidate=candidate,
        )

    def _score(
        self,
        policy: MemoryPolicy,
        task_runner: MemoryTaskRunner,
    ) -> MemoryPolicyScore:
        results = tuple(task_runner(case, policy(case)) for case in self._cases)
        return MemoryPolicyScore(
            successes=sum(result.success for result in results),
            regressions=sum(result.regressions for result in results),
            tokens=sum(result.tokens for result in results),
            elapsed_s=sum(result.elapsed_s for result in results),
            results=results,
        )


def load_memory_evaluation_suite(path: Path) -> tuple[MemoryEvaluationCase, ...]:
    """Load version-controlled fixed cases from JSON; reject malformed suites."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases_raw = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases_raw, list):
        raise ValueError("memory evaluation suite must contain a cases list")
    cases: list[MemoryEvaluationCase] = []
    for item in cases_raw:
        if not isinstance(item, dict):
            raise ValueError("each memory evaluation case must be an object")
        task_id = item.get("task_id")
        goal = item.get("goal")
        files = item.get("files", [])
        if not isinstance(task_id, str) or not isinstance(goal, str):
            raise ValueError("each memory evaluation case requires string task_id and goal")
        if not isinstance(files, list) or not all(isinstance(file, str) for file in files):
            raise ValueError("memory evaluation case files must be a string list")
        cases.append(MemoryEvaluationCase(task_id=task_id, goal=goal, files=tuple(files)))
    MemoryPolicyEvaluator(cases)
    return tuple(cases)
