"""Independent, deterministic evidence evaluator for TARGET progress."""

from __future__ import annotations

from dataclasses import dataclass

from supervisor.runners.test_runner import RunTestResult


@dataclass(frozen=True)
class TargetEvaluation:
    accepted: bool
    regression: bool
    observable_progress: bool
    reason: str
    evidence: str


class TargetEvidenceEvaluator:
    """Accept/reject candidate evidence without relying on worker self-report."""

    @staticmethod
    def evaluate(
        *,
        baseline: RunTestResult | None,
        candidate: RunTestResult,
        changed_files: list[str],
    ) -> TargetEvaluation:
        evidence = candidate.summary()
        if baseline is not None and candidate.is_regression_vs(baseline):
            return TargetEvaluation(
                accepted=False,
                regression=True,
                observable_progress=bool(changed_files),
                reason="candidate regressed against test baseline",
                evidence=evidence,
            )
        if changed_files:
            return TargetEvaluation(
                accepted=True,
                regression=False,
                observable_progress=True,
                reason="candidate passed non-regression test gate with changed files",
                evidence=evidence,
            )
        return TargetEvaluation(
            accepted=True,
            regression=False,
            observable_progress=False,
            reason="candidate passed test gate but produced no changed-file evidence",
            evidence=evidence,
        )
