"""Job-store write/read races must never make a live job unreadable.

Production topology behind the 2026-11-02 "UI silently disconnects" incident:

- the worker daemon rewrites the RUNNING heartbeat state every 5 s via
  ``save_job_state`` (tmp file + ``os.replace``),
- the UI process's cancel path writes the SAME state file from its own
  process (``StoreJobClient.cancel_job``),
- the UI polls ``get_job_state`` every 2 s while all of that lands.

Two writers used ONE fixed ``.tmp`` name per job, so a writer could truncate
the tmp file another writer was mid-replace on and publish torn JSON; readers
racing ``os.replace`` on Windows saw ``[Errno 13] Permission denied``. The
UI log captured exactly that on a live job. Contract under test: while the
job exists, a reader never observes it missing/corrupt and no writer errors
out, no matter how hard the store is hammered.
"""

from __future__ import annotations

import threading
import time

from services.jobs.state_store import JobStateStore

HAMMER_SECONDS = 2.0
READER_GAP = 0.005  # reader duty cycle stays far below 100%, like production


def _hammer(tmp_path) -> tuple[list[str], list[str]]:
    store = JobStateStore(str(tmp_path))
    job_id = "run_race0001"
    store.save_job_state(job_id, {"state": "RUNNING", "seq": 0})

    write_errors: list[str] = []
    read_anomalies: list[str] = []
    stop = threading.Event()

    def writer(tag: str) -> None:
        seq = 0
        while not stop.is_set():
            seq += 1
            try:
                # Two distinct production writers: the worker heartbeat and
                # the UI cancel-state write, both full-dict saves.
                store.save_job_state(job_id, {"state": "RUNNING", "seq": seq, "by": tag})
            except OSError as exc:  # save gave up after its retries
                write_errors.append(f"{tag}: {exc}")
                return

    def reader() -> None:
        while not stop.is_set():
            got = store.get_job_state(job_id)
            if got is None:
                read_anomalies.append("state file unreadable/None while job exists")
            elif not isinstance(got.get("seq"), int):
                read_anomalies.append(f"torn or corrupt state observed: {got!r}")
            time.sleep(READER_GAP)

    threads = [
        threading.Thread(target=writer, args=("heartbeat",)),
        threading.Thread(target=writer, args=("cancel",)),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    time.sleep(HAMMER_SECONDS)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    return write_errors, read_anomalies


def test_concurrent_state_saves_never_break_readers(tmp_path):
    write_errors, read_anomalies = _hammer(tmp_path)
    assert not write_errors, f"save_job_state failed under concurrency: {write_errors[:5]}"
    assert not read_anomalies, f"reader observed broken state: {read_anomalies[:5]}"


def test_reader_retries_past_a_transient_open_error(tmp_path, monkeypatch):
    """A single transient OSError on open must not surface as "job unknown"."""
    import builtins

    import services.jobs.state_store as ss

    store = JobStateStore(str(tmp_path))
    job_id = "run_retry0001"
    store.save_job_state(job_id, {"state": "RUNNING", "seq": 7})

    real_open = builtins.open
    calls = {"n": 0}

    def flaky_open(path, *args, **kwargs):
        # Fail the first read attempt only, like a delete-pending window
        # during a concurrent os.replace.
        if "r" in (args[0] if args else kwargs.get("mode", "r")):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(ss, "open", flaky_open, raising=False)
    got = store.get_job_state(job_id)
    assert got is not None and got["seq"] == 7 and got["state"] == "RUNNING"
    assert calls["n"] >= 2  # the retry actually happened
