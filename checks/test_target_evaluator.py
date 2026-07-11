from supervisor.core.target_evaluator import TargetEvidenceEvaluator
from supervisor.runners.test_runner import RunTestResult


def _result(passed: int, failed: int = 0, errors: int = 0) -> RunTestResult:
    return RunTestResult(passed, failed, errors, 0.01, "result", 0 if not failed and not errors else 1)


def test_target_evaluator_rejects_test_regression_independently_of_worker_output():
    evaluation = TargetEvidenceEvaluator.evaluate(
        baseline=_result(10),
        candidate=_result(9, failed=1),
        changed_files=["supervisor/core/target_state.py"],
    )

    assert not evaluation.accepted
    assert evaluation.regression
    assert "regressed" in evaluation.reason


def test_target_evaluator_records_when_tests_pass_but_worker_changed_nothing():
    evaluation = TargetEvidenceEvaluator.evaluate(
        baseline=_result(10),
        candidate=_result(10),
        changed_files=[],
    )

    assert evaluation.accepted
    assert not evaluation.regression
    assert not evaluation.observable_progress
    assert "no changed-file evidence" in evaluation.reason
