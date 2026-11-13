"""Regression checks: list/board scans must never parse job logs.

Measured failure: ``/run`` stopped responding entirely (no reply in 240s)
because the board rebuilds every job card on a 1.5s timer while
``get_job_status`` attached the FULL log file to each payload - that was tens
of megabytes of .logs per refresh, with single job logs over 14 MB.

Dates and run ids deliberately stay out of this file: the checks are general.
"""

import json
import time

import pytest

from services.jobs.job_manager import JobManager
from services.jobs.state_store import JobStateStore


@pytest.fixture
def store(tmp_path):
    return JobStateStore(store_dir=str(tmp_path / ".job_store"))


def _write_logs(store, job_id, n, *, bom=False, payload_bytes=200):
    path = store._get_logs_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pad = "x" * payload_bytes
    with open(path, "w", encoding="utf-8") as f:
        if bom:
            f.write("\ufeff")
        f.writelines(json.dumps({"seq": i, "level": "info", "msg": pad}) + "\n" for i in range(n))


def test_tail_logs_returns_last_n_in_order(store):
    _write_logs(store, "run_a", 500)
    tail = store.tail_logs("run_a", 50)
    assert len(tail) == 50
    assert [e["seq"] for e in tail] == list(range(450, 500))


def test_tail_logs_reads_whole_small_file_and_bom(store):
    _write_logs(store, "run_b", 7, bom=True)
    tail = store.tail_logs("run_b", 100)
    assert [e["seq"] for e in tail] == list(range(7))


def test_tail_logs_boundaries(store):
    _write_logs(store, "run_c", 20)
    assert store.tail_logs("run_c", 0) == []
    assert store.tail_logs("run_missing", 10) == []
    assert len(store.tail_logs("run_c", 20)) == 20
    assert len(store.tail_logs("run_c", 1000)) == 20


def test_tail_logs_matches_get_logs_on_large_file(store):
    _write_logs(store, "run_d", 4000)
    tail = store.tail_logs("run_d", 300)
    assert tail == store.get_logs("run_d")[-300:]


def test_tail_logs_skips_leading_fragment_of_tail_slice(store):
    # Force many chunk expansions so the leading line is cut mid-record.
    _write_logs(store, "run_e", 3000, payload_bytes=900)
    tail = store.tail_logs("run_e", 10)
    assert [e["seq"] for e in tail] == list(range(2990, 3000))


def test_get_job_summary_carries_no_logs(tmp_path):
    manager = JobManager(store_dir=str(tmp_path / ".job_store"))
    manager.store.save_job_state("run_f", {"type": "run", "state": "RUNNING"})
    _write_logs(manager.store, "run_f", 1000)
    summary = manager.get_job_summary("run_f")
    assert summary is not None and "logs" not in summary
    assert summary["state"] == "RUNNING"


def test_get_job_status_max_logs_caps_tail(tmp_path):
    manager = JobManager(store_dir=str(tmp_path / ".job_store"))
    manager.store.save_job_state("run_g", {"type": "run", "state": "RUNNING"})
    _write_logs(manager.store, "run_g", 1000)
    assert len(manager.get_job_status("run_g")["logs"]) == 1000
    capped = manager.get_job_status("run_g", max_logs=25)["logs"]
    assert [e["seq"] for e in capped] == list(range(975, 1000))


def test_list_widgets_use_the_logs_free_path():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    board = (root / "services/webui/components/board.py").read_text(encoding="utf-8")
    screen = (root / "services/webui/components/live_screen.py").read_text(encoding="utf-8")

    def code_of(text: str, marker: str) -> str:
        body = text.split(marker, 1)[1].split("def ", 1)[0]
        # Explanatory comments deliberately name the old call; check code only.
        return "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))

    collect = code_of(board, "def _collect")
    switcher = code_of(screen, "def _render_switcher")
    assert "get_job_summary" in collect and "get_job_status" not in collect
    # The switcher now reads the shell's shared summary snapshot instead of
    # walking the store itself; both are log-free by construction.
    assert "get_job_status" not in switcher
    assert "get_job_summary" in switcher or "_store_snapshot" in switcher


def test_board_collect_cost_scales_with_state_files_only(tmp_path):
    """A board scan over many jobs with big logs must stay cheap."""
    manager = JobManager(store_dir=str(tmp_path / ".job_store"))
    store = manager.store
    for i in range(40):
        store.save_job_state(f"run_{i}", {"type": "run", "state": "SUCCESS", "updated_at": time.time()})
        _write_logs(store, f"run_{i}", 3000)
    t0 = time.perf_counter()
    collected = [manager.get_job_summary(j) for j in store.list_jobs()]
    elapsed = time.perf_counter() - t0
    assert len(collected) == 40
    # ~120k log events (~24 MB) are on disk; the old path parsed all of them
    # here, which took >1s even warm and tens of seconds on a loaded disk.
    assert elapsed < 1.0, f"40-job summary scan took {elapsed:.2f}s"
