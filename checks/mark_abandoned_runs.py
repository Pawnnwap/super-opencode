"""One-off: mark yesterday's service-shutdown-orphans as FAILED.

The supervisor app died ~2026-09-30 17:29 while run_93c0ecca and
run_0557bddb were RUNNING; their store records were never finalized.
Run with the service STOPPED (no other writer for .job_store).
"""
import sys

sys.path.insert(0, r"E:\pyprojects\super-opencode")

from services.jobs.state_store import JobStateStore

ABANDONED = ["run_93c0ecca", "run_0557bddb"]
MSG = (
    "Run abandoned: the supervisor app process shut down while this job was "
    "RUNNING (last heartbeat 2026-09-30 ~17:29). Marked FAILED by the "
    "2026-10-01 service-restart maintenance pass; the task is being "
    "re-launched as a fresh job on the same workspace."
)

store = JobStateStore(r"E:\pyprojects\super-opencode\.job_store")
for job_id in ABANDONED:
    state = store.get_job_state(job_id)
    if state is None:
        print(f"{job_id}: no record, skipped")
        continue
    if state.get("state") not in ("RUNNING", "PENDING"):
        print(f"{job_id}: state={state.get('state')}, skipped")
        continue
    store.append_log(job_id, {"level": "error", "msg": MSG})
    state["state"] = "FAILED"
    store.save_job_state(job_id, state)
    print(f"{job_id}: RUNNING -> FAILED")
