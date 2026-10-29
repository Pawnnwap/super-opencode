"""EventRecorder checks: headline digests, sidecar refs, error fidelity.

The recorder is the write-time filter between the loop event stream and the
job log. Contract under test (mode="headline"):

- error events pass through in full, always — never truncated, never moved
  to the sidecar;
- big blocks (prompts / outputs / verdicts / suggestions) keep one key line
  inline with a ``ref`` into the ``.full`` sidecar, whose round-trip body is
  byte-identical;
- heartbeat events are dropped;
- repeated identical warnings surface only at count milestones;
- ``report`` / ``success`` survive untouched (final-state and report capture
  depend on them);
- mode="full" restores verbatim events.
"""

import pytest

from services.jobs.event_recorder import EventRecorder
from services.jobs.state_store import JobStateStore


@pytest.fixture
def store(tmp_path):
    return JobStateStore(store_dir=str(tmp_path / ".job_store"))


@pytest.fixture
def recorder(store):
    return EventRecorder(store, "run_x", mode="headline")


def test_error_events_always_full_inline(recorder):
    big = "x" * 50_000
    out = recorder.record({"level": "error", "msg": f"boom\n{big}"})
    assert out is not None
    assert out["msg"] == f"boom\n{big}"
    assert "ref" not in out  # errors never depend on the sidecar


def test_prompt_headline_carries_kind_and_ref(store, recorder):
    out = recorder.record({
        "level": "opencode_prompt",
        "msg": "# Task\n" + "protocol body " * 500,
        "prompt_kind": "restart",
    })
    assert out["msg"].startswith("→ restart prompt (")
    assert f"full text in event #{out['ref']}" in out["msg"]
    assert out["detail_len"] == len("protocol body " * 500) + len("# Task\n")
    detail = store.read_full("run_x", out["ref"])
    assert detail["level"] == "opencode_prompt"
    assert detail["body"] == "# Task\n" + "protocol body " * 500


def test_output_headline_shows_head_and_tail(store, recorder):
    lines = [f"line {i}" for i in range(50)]
    body = "\n".join(lines)
    out = recorder.record({"level": "opencode_output", "msg": body})
    assert "line 0" in out["msg"] and "line 1" in out["msg"]
    assert "line 48" in out["msg"] and "line 49" in out["msg"]
    assert "line 25" not in out["msg"]  # middle dropped from the headline
    assert store.read_full("run_x", out["ref"])["body"] == body


def test_verdict_prefers_structured_digest(store, recorder):
    out = recorder.record({
        "level": "supervisor_response",
        "msg": "VERY LONG RAW VERDICT " * 1000,
        "digest": "⚖ CONTINUE — criteria 2/5 — next: fix the parser",
    })
    assert out["msg"].startswith("⚖ CONTINUE — criteria 2/5")
    assert f"full text in event #{out['ref']}" in out["msg"]
    assert "VERY LONG RAW" not in out["msg"]
    assert store.read_full("run_x", out["ref"])["body"].startswith("VERY LONG")


def test_verdict_without_digest_falls_back_to_head_tail(recorder):
    out = recorder.record({
        "level": "supervisor_response",
        "msg": "first\nmiddle\nmiddle2\nlast",
    })
    assert "first" in out["msg"] and "last" in out["msg"]


def test_suggestions_headline_first_line_only(recorder):
    body = "Suggestion one: do X\n" + ("detail " * 2000)
    out = recorder.record({"level": "supervisor_suggestions", "msg": body})
    assert "Suggestion one: do X" in out["msg"]
    assert "detail detail" not in out["msg"]


def test_heartbeat_dropped(recorder):
    assert recorder.record({"level": "heartbeat", "msg": "…"}) is None


def test_report_and_success_pass_through(recorder):
    report = {"level": "report", "msg": "# Final report\ncontent"}
    assert recorder.record(report) == report
    success = {"level": "success", "msg": "All targets met"}
    assert recorder.record(success) == success


def test_warn_repeats_coalesce_on_first_line(store, recorder):
    first = recorder.record({
        "level": "warn", "msg": "⚠️ Token warning: slow\n\nFiles loaded: a.py",
    })
    assert first["msg"].startswith("⚠️ Token warning")
    # Same headline, churned file list → ×2 milestone marker, not a re-log.
    second = recorder.record({
        "level": "warn", "msg": "⚠️ Token warning: slow\n\nFiles loaded: a.py, b.py",
    })
    assert second is not None and "×2" in second["msg"]
    # count=3 stays below the ×5 milestone → suppressed.
    third = recorder.record({
        "level": "warn", "msg": "⚠️ Token warning: slow\n\nFiles loaded: c.py",
    })
    assert third is None


def test_warn_milestone_marker_content(recorder):
    marker_seen = []
    for i in range(6):
        out = recorder.record({
            "level": "warn", "msg": f"same warning\nFiles loaded: f{i}.py",
        })
        if out is not None:
            marker_seen.append(out["msg"])
    # First occurrence verbatim, then exactly one ×2 and one ×5 marker.
    assert marker_seen[0] == "same warning\nFiles loaded: f0.py"
    assert len(marker_seen) == 3
    assert "×2" in marker_seen[1] and "same warning" in marker_seen[1]
    assert "×5" in marker_seen[2]


def test_different_warns_do_not_coalesce(recorder):
    a = recorder.record({"level": "warn", "msg": "Blocked paths: [x]"})
    b = recorder.record({"level": "warn", "msg": "Blocked paths: [y]"})
    assert a is not None and b is not None
    assert a["msg"] != b["msg"]


def test_full_mode_passthrough(store):
    recorder = EventRecorder(store, "run_y", mode="full")
    event = {"level": "opencode_prompt", "msg": "everything " * 1000}
    out = recorder.record(event)
    assert out is event
    # Full mode writes no sidecar records.
    assert store.read_full("run_y", 1) is None


def test_sidecar_refs_unique_across_instances(store):
    r1 = EventRecorder(store, "run_z", mode="headline")
    out1 = r1.record({"level": "opencode_output", "msg": "one"})
    # A new process (fresh recorder) continues the ref sequence.
    r2 = EventRecorder(store, "run_z", mode="headline")
    out2 = r2.record({"level": "opencode_output", "msg": "two"})
    assert out2["ref"] > out1["ref"]
    assert store.read_full("run_z", out1["ref"])["body"] == "one"
    assert store.read_full("run_z", out2["ref"])["body"] == "two"


def test_verdict_headline_from_supervisor_verdict():
    from supervisor.core.llm_support.models import (
        CriterionResult,
        SupervisorVerdict,
    )

    verdict = SupervisorVerdict(
        raw="long raw text",
        all_targets_met=False,
        feedback="keep going",
        criteria_results=[
            CriterionResult(criterion="parser works", met=True),
            CriterionResult(criterion="tests pass", met=False),
            CriterionResult(criterion="docs updated", met=False),
        ],
        next_action="Fix the failing test suite",
    )
    headline = verdict.headline()
    assert "CONTINUE" in headline
    assert "criteria 1/3" in headline
    assert "tests pass" in headline and "docs updated" in headline
    assert "Fix the failing test suite" in headline

    done = SupervisorVerdict(raw="", all_targets_met=True, feedback="")
    assert "DONE" in done.headline()
