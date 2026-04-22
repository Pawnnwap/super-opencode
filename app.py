"""app.py - opencode Supervisor UI.

Run with: streamlit run app.py
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from services.config.opencode_config import (
    fetch_opencode_models,
    find_opencode_config_dir,
    get_opencode_config_file,
)
from services.config.settings import load_settings
from services.jobs.job_manager import JobManager
from services.runtime.app_bootstrap import (
    auto_upgrade_dcp as _auto_upgrade_dcp,
    auto_upgrade_opencode as _auto_upgrade_opencode,
)
from services.runtime.workspace_cleanup import clean_workspace_artifacts
from services.ui.app_shell import apply_page_shell
from services.ui.protocol_ui import render_existing_protocol_banner
from services.ui.sidebar_ui import PILL_MAP, render_sidebar
from services.ui.pages.task_ui import (
    page_evolve as _page_evolve_impl,
    page_run as _page_run_impl,
)
from services.ui.pages.wizard_page import page_wizard


@st.cache_resource
def _get_job_manager():
    return JobManager(".job_store")


def _redirect_if_locked(page: str, warning: str) -> None:
    st.session_state.page = "wizard"
    st.session_state["_redirect_warning"] = warning
    st.rerun()


apply_page_shell()
job_manager = _get_job_manager()

if not st.session_state.get("_upgrade_done"):
    _auto_upgrade_opencode()
    _auto_upgrade_dcp()
    st.session_state["_upgrade_done"] = True

if not st.session_state.get("_mcp_config_done"):
    mcp_dir = find_opencode_config_dir()
    if mcp_dir:
        get_opencode_config_file(
            mcp_dir,
            Path(__file__).parent.resolve(),
            on_info=st.info,
            on_warning=st.warning,
        )
    st.session_state["_mcp_config_done"] = True

if not st.session_state.get("_artifact_clean_done"):
    workspace_raw = st.session_state.get("workspace", "")
    if workspace_raw:
        clean_workspace_artifacts(Path(workspace_raw))
    st.session_state["_artifact_clean_done"] = True

persisted = load_settings()
defaults = {
    "page": "wizard",
    "protocol_md": "",
    "log_events": [],
    "run_state": "idle",
    "final_report": "",
    "wizard_step": 0,
    "raw_input": "",
    "raw_target": "",
    "raw_restrictions": "",
    "openai_key": "",
    "base_url": "",
    "workspace": "",
    "supervisor_model": "",
    "supervisor_model_backup": "",
    "opencode_model": "",
    "opencode_model_backup": "",
    "opencode_executable": "",
    "max_retries": 3,
    "context_threshold": 60,
    "max_tokens": 150000,
    "timeout": 120,
    "plan_mode_rounds": 1,
    "protected_files": [],
    "_last_workspace": "",
    "evo_goal": "",
    "evo_extra_restrictions": "",
    "evo_meta_protocol_md": "",
    "evo_log_events": [],
    "evo_run_state": "idle",
    "evo_report": "",
    "evo_wizard_step": 0,
    "self_evolution_verbose": False,
    "verbose_log": True,
    "_run_heartbeat": 0,
    "_evo_heartbeat": 0,
    "opencode_test_passed": False,
    "supervisor_test_passed": False,
    "opencode_models": [],
    "enable_python_scanner": True,
}
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = persisted.get(key, value)

if not st.session_state["opencode_models"]:
    st.session_state["opencode_models"] = fetch_opencode_models()

tests_ok = render_sidebar(job_manager)
page = st.session_state.page
if page == "report":
    st.session_state.page = "run"
    page = "run"

if page in {"run", "evolve"} and not tests_ok:
    _redirect_if_locked(
        page,
        f"{page.title()} is locked. Pass connectivity tests on Protocol Wizard page first.",
    )
elif page == "wizard":
    page_wizard()
elif page == "run":
    _page_run_impl(job_manager=job_manager, pill_map=PILL_MAP)
elif page == "evolve":
    _page_evolve_impl(
        job_manager=job_manager,
        pill_map=PILL_MAP,
        render_existing_protocol_banner=render_existing_protocol_banner,
    )
