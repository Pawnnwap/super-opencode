"""Contradiction checks must distinguish failure evidence from valid behavior."""

from supervisor.core.llm_support.models import (
    CriterionResult,
    flag_contradicted_criteria,
)


def test_unable_to_connect_is_valid_positive_evidence():
    criterion = CriterionResult(
        "clear connection error",
        True,
        "Returns a clear error when unable to connect",
    )

    adjusted, notes = flag_contradicted_criteria([criterion])

    assert adjusted[0].met is True
    assert notes == []


def test_unable_to_create_is_negative_evidence():
    criterion = CriterionResult(
        "create greet.py",
        True,
        "Unable to create greet.py",
    )

    adjusted, notes = flag_contradicted_criteria([criterion])

    assert adjusted[0].met is False
    assert notes
