from supervisor.core.target_state import (
    initialise_target_state,
    load_target_state,
    read_staged_improvements,
    record_target_iteration,
    similar_rejected_direction,
    stage_temporary_improvement,
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


def test_staged_improvement_preserves_candidate_metadata(tmp_path):
    initialise_target_state(tmp_path, target="Add feature and test it.")
    path = stage_temporary_improvement(
        tmp_path,
        iteration=3,
        direction="replace parser implementation",
        changed_files=["supervisor/parser.py"],
        archive_path=tmp_path / ".archive" / "candidate",
        test_evidence="passed=10 failed=1 errors=0",
        reason="regression",
        output_excerpt="temporary implementation details",
    )

    records = read_staged_improvements(tmp_path)
    state = load_target_state(tmp_path)
    assert path.exists()
    assert records[0]["status"] == "rolled_back_but_preserved"
    assert records[0]["changed_files"] == ["supervisor/parser.py"]
    assert state and state.staged_improvements == 1
