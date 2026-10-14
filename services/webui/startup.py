"""Non-blocking startup pipeline: CLI upgrades, MCP config, artifact cleanup.

Runs once per process in a background executor so first paint is immediate;
the layout banner renders :data:`statuses` while tasks are pending.
"""

from __future__ import annotations

import threading
from pathlib import Path

from services.config.codex_config import ensure_codehelp_codex_mcp
from services.config.opencode_config import (
    find_opencode_config_dir,
    get_opencode_config_file,
)
from services.runtime.app_bootstrap import (
    auto_upgrade_codex,
    auto_upgrade_opencode,
)
from services.runtime.workspace_cleanup import clean_workspace_artifacts

_statuses: list[dict] = []
_lock = threading.Lock()
_started = False

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def statuses() -> list[dict]:
    with _lock:
        return list(_statuses)


def _record(task: str, state: str, detail: str = "") -> None:
    with _lock:
        _statuses.append({"task": task, "state": state, "detail": detail})


def _run_task(task: str, fn) -> None:
    _record(task, "running")
    try:
        detail = fn() or ""
        _record(task, "done", str(detail)[:200])
    except Exception as exc:  # noqa: BLE001 — startup must never crash the app
        _record(task, "failed", str(exc)[:200])



def _launch_from_env() -> str:
    # Multi-workspace launcher: SUPERVISOR_LAUNCH_WORKSPACES="a;b;c" plus
    # SUPERVISOR_LAUNCH_PLAN_ROUNDS (default 3) enqueue one run job per
    # workspace inside this app process, mirroring the UI launch path.
    import os

    from services.config.supervisor_config_builder import build_supervisor_config
    from services.webui.jobs import get_job_manager
    from services.webui.state import app_state

    raw = os.environ.get("SUPERVISOR_LAUNCH_WORKSPACES", "").strip()
    if not raw:
        return "no launch request"
    rounds_raw = os.environ.get("SUPERVISOR_LAUNCH_PLAN_ROUNDS", "3").strip()
    try:
        rounds = int(rounds_raw)
    except ValueError:
        rounds = 3
    manager = get_job_manager()
    launched: list[str] = []
    for part in raw.split(";"):
        ws = Path(part.strip())
        if not ws.exists():
            launched.append(f"skip missing {ws}")
            continue
        config = build_supervisor_config(
            app_state.as_mapping(),
            protocol_path=ws / "protocol.md",
            workspace=ws,
            plan_mode_rounds=rounds,
            enable_python_scanner=bool(app_state.get("enable_python_scanner")),
            enable_occam_razor=bool(app_state.get("enable_occam_razor", False)),
            enable_supervisor_skills=bool(
                app_state.get("enable_supervisor_skills", True),
            ),
        )
        job_id = manager.enqueue_job("run", config)
        launched.append(f"{ws.name} -> {job_id}")
    return "; ".join(launched)


def _replay_job() -> str:
    """Re-enqueue one stored job when SUPERVISOR_REPLAY_JOB=<job_id> is set.
    Runs inside this app process so the worker thread and cancel path work;
    a fresh job id is minted and the stored config is reused verbatim.
    """
    import os

    from services.webui.jobs import get_job_manager
    from supervisor.utils.config import SupervisorConfig

    job_id = os.environ.get("SUPERVISOR_REPLAY_JOB", "").strip()
    if not job_id:
        return "no replay request"
    manager = get_job_manager()
    state = manager.store.get_job_state(job_id)
    if not state:
        return f"replay job {job_id} not found in store"
    raw = state.get("config") or {}
    fields = set(SupervisorConfig.__dataclass_fields__)
    kwargs = {k: v for k, v in dict(raw).items() if k in fields}
    kwargs["protocol_path"] = Path(kwargs["protocol_path"])
    kwargs["workspace"] = Path(kwargs["workspace"])
    config = SupervisorConfig(**kwargs)
    new_id = manager.enqueue_job(str(state.get("type", "run")), config)
    return f"replayed {job_id} -> {new_id}"
def launch_startup_tasks() -> None:
    """Kick off all startup work in a daemon thread (idempotent)."""
    global _started
    if _started:
        return
    _started = True

    from services.webui.state import app_state

    def _upgrade_opencode() -> str:
        auto_upgrade_opencode(registry=app_state.get("npm_registry"))
        return "opencode CLI"

    def _upgrade_codex() -> str:
        auto_upgrade_codex(registry=app_state.get("npm_registry"))
        return "codex CLI"

    def _install_mcp() -> str:
        infos: list[str] = []
        mcp_dir = find_opencode_config_dir()
        if mcp_dir:
            get_opencode_config_file(
                mcp_dir,
                PROJECT_ROOT,
                on_info=infos.append,
                on_warning=infos.append,
            )
        ensure_codehelp_codex_mcp(
            PROJECT_ROOT,
            on_info=infos.append,
            on_warning=infos.append,
        )
        return "; ".join(infos)[-200:]

    def _clean_artifacts() -> str:
        workspace = app_state.get("workspace") or ""
        if workspace:
            clean_workspace_artifacts(Path(workspace))
            return f"cleaned {workspace}"
        return "no workspace configured"

    def _pipeline() -> None:
        if app_state.get("engine") == "opencode":
            _run_task("upgrade opencode", _upgrade_opencode)
        _run_task("upgrade codex", _upgrade_codex)
        _run_task("install MCP config", _install_mcp)
        _run_task("clean workspace artifacts", _clean_artifacts)
        _run_task("replay job from env", _replay_job)
        _run_task("launch workspaces from env", _launch_from_env)

    thread = threading.Thread(target=_pipeline, name="webui-startup", daemon=True)
    thread.start()
