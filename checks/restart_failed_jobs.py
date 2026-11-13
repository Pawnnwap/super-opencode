"""Restart terminal-stopped jobs from the job store, config reused verbatim.

Usage::

    python checks/restart_failed_jobs.py run_34396c58 run_6f4af7d5
    python checks/restart_failed_jobs.py --states FAILED,BLOCKED --plan-rounds 3 run_34396c58

Reuses each job's stored config verbatim, overriding only what the flags say.
Runs detached: enqueues the jobs, then stays alive until every job reaches a
terminal state so the worker threads are not killed with the parent process.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.jobs.job_manager import JobManager
from supervisor.utils.config import SupervisorConfig

STORE = ROOT / ".job_store"
# States worth a restart. CANCELLED and SUCCESS are deliberate outcomes; a
# BLOCKED run only reruns if the operator changed something (facts, protocol).
RESTARTABLE = {"FAILED", "ABANDONED", "BLOCKED"}
TERMINAL = JobManager.TERMINAL_STATES


def load_config(job_id: str, plan_mode_rounds: int | None) -> SupervisorConfig:
    state = json.loads((STORE / f"{job_id}.json").read_text(encoding="utf-8"))
    data = dict(state["config"])
    if plan_mode_rounds is not None:
        data["plan_mode_rounds"] = plan_mode_rounds
    names = {f.name for f in fields(SupervisorConfig)}
    kwargs = {k: v for k, v in data.items() if k in names}
    for key in ("protocol_path", "workspace"):
        kwargs[key] = Path(kwargs[key])
    kwargs["protected_files"] = tuple(kwargs.get("protected_files") or ())
    return SupervisorConfig(**kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job_ids", nargs="+", help="job ids in .job_store")
    parser.add_argument(
        "--states",
        default=",".join(sorted(RESTARTABLE)),
        help="restart only jobs in these states (default: FAILED,ABANDONED,BLOCKED)",
    )
    parser.add_argument(
        "--plan-rounds",
        type=int,
        default=None,
        help="override plan_mode_rounds (default: keep stored value)",
    )
    args = parser.parse_args()
    states = {s.strip().upper() for s in args.states.split(",") if s.strip()}

    manager = JobManager(store_dir=str(STORE))
    live: list[str] = []
    for job_id in args.job_ids:
        state = manager.store.get_job_state(job_id) or {}
        if state.get("state") not in states:
            print(f"skip {job_id}: state={state.get('state')}", flush=True)
            continue
        config = load_config(job_id, args.plan_rounds)
        new_id = manager.enqueue_job(state.get("type", "run"), config)
        live.append(new_id)
        print(
            f"enqueued {job_id} -> {new_id} "
            f"workspace={config.workspace} plan_mode_rounds={config.plan_mode_rounds}",
            flush=True,
        )

    if not live:
        print("nothing to restart", flush=True)
        return 0

    while True:
        rows = []
        for new_id in live:
            st = (manager.store.get_job_state(new_id) or {}).get("state", "UNKNOWN")
            rows.append(f"{new_id}={st}")
        print(" | ".join(rows), flush=True)
        if all(s.split("=", 1)[1] in TERMINAL for s in rows):
            print("all jobs terminal", flush=True)
            return 0
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
