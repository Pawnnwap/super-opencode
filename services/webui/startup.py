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

    thread = threading.Thread(target=_pipeline, name="webui-startup", daemon=True)
    thread.start()
