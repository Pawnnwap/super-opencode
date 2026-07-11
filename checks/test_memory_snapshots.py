from __future__ import annotations

from supervisor.utils.experience_tracker import (
    EvolutionSummary,
    read_experience,
    read_memory_events,
    read_summaries,
    retrieve_memory,
    restore_memory_snapshot,
    snapshot_memory,
    update_experience,
    log_evolution_summary,
)


def test_memory_snapshot_restores_state_but_keeps_audit_log(tmp_path):
    update_experience(tmp_path, worked=["keep this lesson"])
    snapshot = snapshot_memory(tmp_path, label="before risky edit")
    update_experience(tmp_path, failed=["discard this regression lesson"])

    assert restore_memory_snapshot(tmp_path, snapshot)

    restored = read_experience(tmp_path)
    assert "keep this lesson" in restored
    assert "discard this regression lesson" not in restored
    event_path = tmp_path / ".opencode" / "memory_events.jsonl"
    with event_path.open("a", encoding="utf-8") as f:
        f.write("not json\n")
    assert [event["event_type"] for event in read_memory_events(tmp_path)] == [
        "experience_updated",
        "memory_snapshot_created",
        "experience_updated",
        "memory_snapshot_restored",
    ]


def test_structured_memory_summary_persists_evidence_and_provenance(tmp_path):
    log_evolution_summary(
        tmp_path,
        EvolutionSummary(
            goal="protect user configuration",
            outcome="success",
            changed_files=["services/runtime/app_bootstrap.py"],
            test_evidence=["Baseline: passed=10 failed=0 errors=0"],
            confidence=0.9,
            expires_at="2030-01-01T00:00:00+00:00",
        ),
    )

    summary = read_summaries(tmp_path)[0]
    assert summary.changed_files == ["services/runtime/app_bootstrap.py"]
    assert summary.test_evidence == ["Baseline: passed=10 failed=0 errors=0"]
    assert summary.confidence == 0.9
    assert summary.decision_id
    assert "Changed Files" in read_experience(tmp_path)


def test_memory_retrieval_ranks_goal_and_file_match_and_skips_expired(tmp_path):
    log_evolution_summary(
        tmp_path,
        EvolutionSummary(
            goal="protect Codex configuration during upgrade",
            outcome="success",
            changed_files=["services/runtime/app_bootstrap.py"],
            solutions=["restore user config after upgrade"],
            confidence=0.95,
        ),
    )
    log_evolution_summary(
        tmp_path,
        EvolutionSummary(
            goal="render task board",
            outcome="success",
            changed_files=["services/ui/task_board_ui.py"],
            confidence=1.0,
        ),
    )
    log_evolution_summary(
        tmp_path,
        EvolutionSummary(
            goal="obsolete configuration lesson",
            outcome="failure",
            expires_at="2000-01-01T00:00:00+00:00",
            confidence=1.0,
        ),
    )

    result = retrieve_memory(
        tmp_path,
        goal="preserve config during Codex upgrade",
        files=("services/runtime/app_bootstrap.py",),
        max_records=1,
    )

    assert "protect Codex configuration" in result
    assert "task board" not in result
    assert "obsolete" not in result
