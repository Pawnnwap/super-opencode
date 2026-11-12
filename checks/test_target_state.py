from supervisor.core.target_state import (
    initialise_target_state,
    record_target_iteration,
    similar_rejected_direction,
    target_state_context,
)


def test_target_state_tracks_evidence_and_replan_after_two_rejections(tmp_path):
    initialise_target_state(
        tmp_path,
        target="Add feature with pytest evidence.",
        success_criteria=["pytest passes"],
    )
    first = record_target_iteration(
        tmp_path,
        iteration=1,
        direction="patch cache invalidation tests",
        evidence="failed=1",
        accepted=False,
        reason="test regression",
    )
    second = record_target_iteration(
        tmp_path,
        iteration=2,
        direction="patch cache invalidation tests again",
        evidence="failed=1",
        accepted=False,
        reason="same regression",
    )

    assert first.status == "retry_with_new_direction"
    assert second.status == "replan_required"
    assert similar_rejected_direction(second, "cache invalidation patch tests")
    assert "Avoid repeated directions" in target_state_context(tmp_path)
