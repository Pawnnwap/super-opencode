from __future__ import annotations

from pathlib import Path

import streamlit as st

from services.log_ui import format_status_pill, safe_logs
from services.opencode_config import (
    add_custom_provider_to_config,
    find_opencode_config_dir,
    get_opencode_config_file,
)
from services.settings import save_settings

PILL_MAP = {
    "PENDING": '<span class="pill pill-idle">queued</span>',
    "RUNNING": '<span class="pill pill-running">running</span>',
    "SUCCESS": '<span class="pill pill-success">done</span>',
    "FAILED": '<span class="pill pill-failure">failed</span>',
    "CANCELLED": '<span class="pill pill-failure">cancelled</span>',
}


def _clear_page_state(target_page: str) -> None:
    if target_page != "wizard":
        st.session_state.show_custom_model_form = False
    if target_page != "run":
        st.query_params.pop("run_job_id", None)
    if target_page != "evolve":
        st.query_params.pop("evo_job_id", None)


def _get_opencode_config_file(config_dir: Path) -> Path:
    project_root = Path(__file__).resolve().parents[1]
    return get_opencode_config_file(
        config_dir,
        project_root,
        on_info=st.info,
        on_warning=st.warning,
    )


def _any_job_running(job_manager) -> bool:
    return any(
        job_manager.get_job_status(job_id)
        and job_manager.get_job_status(job_id).get("state") == "RUNNING"
        for job_id in job_manager.store.list_jobs()
    )


def _job_queue_stats(job_manager, job_type: str) -> dict[str, int]:
    stats = {"active": 0, "running": 0, "pending": 0}
    for job_id in job_manager.store.list_jobs():
        status = job_manager.get_job_status(job_id)
        if not status or status.get("type") != job_type:
            continue
        state = status.get("state")
        if state == "RUNNING":
            stats["running"] += 1
            stats["active"] += 1
        elif state == "PENDING":
            stats["pending"] += 1
            stats["active"] += 1
    return stats


def _evo_job_passed(job_manager) -> bool:
    for job_id in job_manager.store.list_jobs():
        status = job_manager.get_job_status(job_id)
        if status and status.get("type") == "evolve" and status.get("state") == "SUCCESS":
            for log in reversed(safe_logs(status)):
                msg = log.get("msg", "")
                if msg and ("Tests: All passed" in msg or log.get("level") == "success"):
                    return True
    return False


def _render_status_pill(state: str) -> str:
    return format_status_pill(state, PILL_MAP)


def _render_custom_model_form() -> None:
    st.markdown("---")
    st.markdown("### Add Custom Model for Opencode")
    if "show_custom_model_form" not in st.session_state:
        st.session_state.show_custom_model_form = False
    if st.button("Add Custom Model for Opencode", key="btn_add_custom_model"):
        st.session_state.show_custom_model_form = True

    if not st.session_state.show_custom_model_form:
        return

    st.markdown("**Custom Service Configuration**")
    service_name = st.text_input(
        "Service name",
        key="custom_service_name",
        placeholder="my-custom-service",
    )
    base_url = st.text_input(
        "Base URL",
        key="custom_base_url",
        placeholder="https://api.example.com/v1",
    )
    api_key = st.text_input(
        "API key",
        key="custom_api_key",
        type="password",
        placeholder="sk-...",
    )
    st.markdown("**Model names** *(one per line)*")
    model_names_input = st.text_area(
        "Model names",
        key="custom_model_names",
        height=100,
        placeholder="qwen3-coder-plus\nqwen3-max\nkimi-k2-0905",
        label_visibility="collapsed",
    )
    if not st.button("Save Service", key="btn_save_custom_service"):
        return

    model_names = [name.strip() for name in model_names_input.splitlines() if name.strip()]
    if not service_name.strip() or not base_url.strip() or not api_key.strip():
        st.error("Please fill in service name, base URL, and API key.")
        return
    if not model_names:
        st.error("Please enter at least one model name.")
        return

    try:
        config_dir = find_opencode_config_dir()
        if config_dir is None:
            st.error("Could not find or create opencode config directory.")
            return
        config_file = _get_opencode_config_file(config_dir)
        add_custom_provider_to_config(
            config_file,
            service_name.strip(),
            base_url.strip(),
            api_key.strip(),
            model_names,
        )
        first_model = f"{service_name.strip()}/{model_names[0]}"
        st.session_state.opencode_model = first_model
        save_settings()
        st.success(f"Service saved. Model '{first_model}' selected and persisted.")
        st.info(f"Models can now be referenced as `{service_name.strip()}/<model-name>`")
        st.session_state.show_custom_model_form = False
        st.rerun()
    except Exception as exc:
        st.error(f"Failed to save service: {exc}")


def render_sidebar(job_manager) -> bool:
    jobs_running = _any_job_running(job_manager)
    tests_ok = (
        (st.session_state.opencode_test_passed and st.session_state.supervisor_test_passed)
        or jobs_running
        or _evo_job_passed(job_manager)
    )

    with st.sidebar:
        st.markdown("## opencode<br>**Supervisor**", unsafe_allow_html=True)
        st.markdown("---")
        run_stats = _job_queue_stats(job_manager, "run")
        evo_stats = _job_queue_stats(job_manager, "evolve")
        queue_bits = []
        if run_stats["active"]:
            queue_bits.append(f"Live {run_stats['running']} run / {run_stats['pending']} queued")
        if evo_stats["active"]:
            queue_bits.append(f"Evo {evo_stats['running']} run / {evo_stats['pending']} queued")
        if queue_bits:
            st.caption(" | ".join(queue_bits))

        for param_key, label in (("run_job_id", "**Live Run**"), ("evo_job_id", "**Self-evo**")):
            job_id = st.query_params.get(param_key)
            if job_id:
                status = job_manager.get_job_status(job_id)
                if status:
                    st.markdown(f"{label} {_render_status_pill(status['state'])}", unsafe_allow_html=True)

        st.markdown("---")
        for key, label in {"wizard": "1. Protocol Wizard", "run": "2. Live Run", "evolve": "3. Self-Evolution"}.items():
            locked = key != "wizard" and not tests_ok
            active = st.session_state.page == key
            if locked:
                st.button(f"Locked {label}", key=f"nav_{key}", use_container_width=True, disabled=True)
            elif st.button(
                label,
                key=f"nav_{key}",
                use_container_width=True,
                type="primary" if active else "secondary",
            ):
                _clear_page_state(key)
                st.session_state.page = key
                st.rerun()

        if not tests_ok:
            st.caption("Run and Self-Evolution locked - pass connectivity tests first.")

        if st.session_state.page == "wizard":
            _render_custom_model_form()

        st.markdown("---")
        st.caption("streamlit | opencode")

    return tests_ok
