"""Regression tests for job-store JSON reads (BOM from external editors/scripts)."""

import json

from services.jobs.state_store import JobStateStore


def _state(job_id):
    return {"job_id": job_id, "state": "RUNNING", "config": {"engine": "codex"}}


def test_get_job_state_tolerates_utf8_bom(tmp_path):
    store = JobStateStore(str(tmp_path))
    (tmp_path / "run_bom.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps(_state("run_bom"), indent=2).encode("utf-8")
    )

    loaded = store.get_job_state("run_bom")

    assert loaded is not None
    assert loaded["state"] == "RUNNING"


def test_get_logs_tolerates_utf8_bom(tmp_path):
    store = JobStateStore(str(tmp_path))
    (tmp_path / "run_bom.logs").write_bytes(
        b"\xef\xbb\xbf"
        + b'{"level": "info", "msg": "first"}\n{"level": "tool", "msg": "second"}\n'
    )

    assert [event["msg"] for event in store.get_logs("run_bom")] == ["first", "second"]


def test_saved_state_is_written_without_bom(tmp_path):
    store = JobStateStore(str(tmp_path))
    store.save_job_state("run_clean", _state("run_clean"))

    assert not (tmp_path / "run_clean.json").read_bytes().startswith(b"\xef\xbb\xbf")
    assert store.get_job_state("run_clean")["state"] == "RUNNING"
