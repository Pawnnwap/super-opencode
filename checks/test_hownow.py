"""How-now sweep: the record-keeper must hand the fresh session live ids +
locations, and the wake-up text must carry the hownow procedure inline.

Also locks the two cheap paths: fenced-reply unwrapping and the no-live-
sessions short circuit (no agent may be spawned for an idle farm).
"""

import time

from services.jobs.hownow import (
    build_wakeup_prompt,
    collect_live_sessions,
    empty_report,
    extract_report,
    format_record_block,
    run_hownow_sweep,
)
from services.jobs.state_store import JobStateStore

_SESSION = "ses_recordkeeper1"


def _seed_job(store: JobStateStore, job_id: str, *, state: str, session: str = "",
              batch: dict | None = None, workspace: str = "D:/ws/study_r1") -> None:
    record = {
        "type": "run",
        "state": state,
        "heartbeat_at": time.time() - 120,
        "config": {"workspace": workspace, "engine": "codex"},
        "report": "",
    }
    record.update(batch or {})
    store.save_job_state(job_id, record)
    if session:
        store.append_log(job_id, {"level": "info", "msg": f"Session ID captured: {session}"})


def test_record_keeper_reports_ids_and_locations(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex_home"
    rollout = (
        codex_home / "sessions" / "2026" / "11" / "03"
        / f"rollout-2026-11-03T10-00-00-{_SESSION}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"session"}\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    store = JobStateStore(str(tmp_path / ".job_store"))
    _seed_job(store, "run_aaaa1111", state="RUNNING", session=_SESSION)
    _seed_job(
        store, "run_bbbb2222", state="PENDING", session="ses_second",
        batch={"batch_id": "study_x", "replica_index": 2, "replica_count": 3},
    )
    _seed_job(store, "run_cccc3333", state="SUCCESS", session="ses_gone")

    sessions = collect_live_sessions(store)
    # Terminal jobs never sweep; RUNNING + PENDING both do (batch sub-runs).
    assert [s["job_id"] for s in sessions] == ["run_aaaa1111", "run_bbbb2222"]

    first = sessions[0]
    assert first["session_id"] == _SESSION
    assert first["engine"] == "codex"
    assert first["transcript"] == str(rollout)
    assert first["job_log"].endswith("run_aaaa1111.logs")
    assert first["replica_label"] == ""

    second = sessions[1]
    assert second["replica_label"] == "r2/3"
    assert second["state"] == "PENDING"

    block = format_record_block(sessions)
    assert _SESSION in block
    assert str(rollout) in block
    assert "run_bbbb2222 r2/3" in block
    assert "run_cccc3333" not in block


def test_record_keeper_unknown_session_points_at_store(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))
    store = JobStateStore(str(tmp_path / ".job_store"))
    _seed_job(store, "run_aaaa1111", state="RUNNING")  # no capture event yet

    sessions = collect_live_sessions(store)
    assert sessions[0]["session_id"] == ""
    assert "codex_home" in sessions[0]["transcript"]  # store dir as the location


def test_wakeup_text_wakes_the_agent_inline_with_the_record():
    sessions = [{
        "job_id": "run_aaaa1111",
        "type": "run",
        "state": "RUNNING",
        "batch_id": "",
        "replica_label": "",
        "engine": "codex",
        "workspace": "D:/ws/study_r1",
        "session_id": _SESSION,
        "transcript": "C:/rollout.jsonl",
        "job_log": "E:/job_store/run_aaaa1111.logs",
        "heartbeat_age_min": 2.0,
    }]
    prompt = build_wakeup_prompt(sessions)
    # The hownow procedure lives in the text itself — no skill file is read.
    assert "FRESH session woken by this text alone" in prompt
    assert "| id | run | state | verdict | you must |" in prompt
    assert "NEEDS YOU" in prompt and "WATCH" in prompt
    assert "exactly two lines" in prompt
    # Record-keeper hand-off: ids AND locations.
    assert _SESSION in prompt
    assert "C:/rollout.jsonl" in prompt
    assert "E:/job_store/run_aaaa1111.logs" in prompt


def test_extract_report_unwraps_fence_and_keeps_bare_reply():
    bare = "| id | run |\n|---|---|\nswept 1 session"
    assert extract_report(bare) == bare
    fenced = "Here is the report:\n```markdown\n%s\n```\ndone." % bare
    assert extract_report(fenced) == bare
    assert extract_report("") == ""


def test_idle_farm_short_circuits_without_an_agent(tmp_path, monkeypatch):
    store = JobStateStore(str(tmp_path / ".job_store"))

    def _explode(config):
        raise AssertionError("no agent session may be spawned for an idle farm")

    monkeypatch.setattr("supervisor.runners.factory.create_runner", _explode)
    report = run_hownow_sweep({"workspace": str(tmp_path)}, store)
    assert report == empty_report()
    assert "swept 0 sessions" in report
