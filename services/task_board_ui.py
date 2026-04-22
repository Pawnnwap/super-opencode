from __future__ import annotations

import time
from pathlib import Path

import streamlit as st

from services.log_ui import (
    format_status_pill,
    render_events,
    render_job_card,
    render_step_progress,
    render_token_usage_bar,
    safe_logs,
)
from services.protocol_ui import render_protocol_quality, save_protocol

ACTIVE_JOB_STATES = {"PENDING", "RUNNING"}
TERMINAL_JOB_STATES = {"SUCCESS", "FAILED", "CANCELLED"}
BOARD_STATE_OPTIONS = [
    "All states",
    "Active queue",
    "RUNNING",
    "PENDING",
    "SUCCESS",
    "FAILED",
    "CANCELLED",
]


def _job_workspace_name(job: dict) -> str:
    workspace = job.get("status", {}).get("config", {}).get("workspace", "")
    if not workspace:
        return ""
    return Path(workspace).name


def _job_label(job: dict) -> str:
    state = job.get("status", {}).get("state", "UNKNOWN")
    workspace_name = _job_workspace_name(job)
    suffix = f" - {workspace_name}" if workspace_name else ""
    return f"{job.get('id', '')} [{state}]{suffix}"


def collect_jobs(job_manager, job_type: str) -> list[dict]:
    jobs: list[dict] = []
    for job_id in job_manager.store.list_jobs():
        status = job_manager.get_job_status(job_id)
        if status and status.get("type") == job_type:
            jobs.append({"id": job_id, "status": status})
    jobs.sort(key=lambda job: job["status"].get("updated_at", 0), reverse=True)
    return jobs


def summarize_jobs(jobs: list[dict]) -> dict[str, int]:
    counts = {state: 0 for state in ACTIVE_JOB_STATES | TERMINAL_JOB_STATES}
    workspaces = set()
    for job in jobs:
        state = job.get("status", {}).get("state", "UNKNOWN")
        if state in counts:
            counts[state] += 1
        workspace_name = _job_workspace_name(job)
        if workspace_name:
            workspaces.add(workspace_name)

    active = counts["PENDING"] + counts["RUNNING"]
    finished = counts["SUCCESS"] + counts["FAILED"] + counts["CANCELLED"]
    return {
        "total": len(jobs),
        "active": active,
        "running": counts["RUNNING"],
        "pending": counts["PENDING"],
        "finished": finished,
        "success": counts["SUCCESS"],
        "needs_attention": counts["FAILED"] + counts["CANCELLED"],
        "failed": counts["FAILED"],
        "cancelled": counts["CANCELLED"],
        "workspaces": len(workspaces),
    }


def filter_jobs(
    jobs: list[dict],
    *,
    state_filter: str = "All states",
    workspace_filter: str = "All workspaces",
    text_query: str = "",
) -> list[dict]:
    query = text_query.strip().lower()
    filtered: list[dict] = []
    for job in jobs:
        status = job.get("status", {})
        state = status.get("state", "UNKNOWN")
        workspace_name = _job_workspace_name(job)

        if state_filter == "Active queue" and state not in ACTIVE_JOB_STATES:
            continue
        if state_filter not in {"All states", "Active queue"} and state != state_filter:
            continue
        if workspace_filter != "All workspaces" and workspace_name != workspace_filter:
            continue
        if query:
            haystack = " ".join(
                part.lower()
                for part in (job.get("id", ""), workspace_name, state)
                if part
            )
            if query not in haystack:
                continue
        filtered.append(job)
    return filtered


def normalize_choice(value: str, valid_options: list[str], default: str) -> str:
    return value if value in valid_options else default


def resolve_focus_job_id(requested_id: str, jobs: list[dict]) -> str:
    if not jobs:
        return ""
    job_ids = [job.get("id", "") for job in jobs]
    if requested_id in job_ids:
        return requested_id
    for job in jobs:
        if job.get("status", {}).get("state") in ACTIVE_JOB_STATES:
            return job.get("id", "")
    return jobs[0].get("id", "")


def _query_param_value(key: str, default: str = "") -> str:
    value = st.query_params.get(key, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value or default


def _set_query_param(key: str, value: str, *, default: str = "") -> None:
    current = _query_param_value(key, default)
    if value == default:
        if key in st.query_params:
            st.query_params.pop(key, None)
        return
    if current != value:
        st.query_params[key] = value


def _ensure_board_filter_state(job_type: str, workspace_options: list[str]) -> None:
    state_key = f"{job_type}_job_state_filter"
    workspace_key = f"{job_type}_job_workspace_filter"
    search_key = f"{job_type}_job_text_filter"

    state_default = normalize_choice(
        _query_param_value(f"{job_type}_state", "All states"),
        BOARD_STATE_OPTIONS,
        "All states",
    )
    workspace_default = normalize_choice(
        _query_param_value(f"{job_type}_workspace", "All workspaces"),
        workspace_options,
        "All workspaces",
    )

    current_state = st.session_state.get(state_key, state_default)
    st.session_state[state_key] = normalize_choice(
        current_state,
        BOARD_STATE_OPTIONS,
        state_default,
    )

    current_workspace = st.session_state.get(workspace_key, workspace_default)
    st.session_state[workspace_key] = normalize_choice(
        current_workspace,
        workspace_options,
        workspace_default,
    )

    if search_key not in st.session_state:
        st.session_state[search_key] = _query_param_value(f"{job_type}_search", "")


def _persist_board_filters(
    job_type: str,
    *,
    state_filter: str,
    workspace_filter: str,
    text_filter: str,
) -> None:
    _set_query_param(f"{job_type}_state", state_filter, default="All states")
    _set_query_param(f"{job_type}_workspace", workspace_filter, default="All workspaces")
    _set_query_param(f"{job_type}_search", text_filter.strip(), default="")


def _ensure_focus_state(job_type: str, jobs: list[dict]) -> str:
    key = f"{job_type}_job_focus_id"
    if key not in st.session_state:
        st.session_state[key] = _query_param_value(f"{job_type}_focus_id", "")
    resolved = resolve_focus_job_id(st.session_state.get(key, ""), jobs)
    if st.session_state.get(key, "") != resolved:
        st.session_state[key] = resolved
    return resolved


def _job_last_event_message(job: dict) -> str:
    logs = safe_logs(job.get("status", {}))
    for event in reversed(logs):
        if not isinstance(event, dict):
            continue
        if event.get("level") == "heartbeat":
            continue
        message = (event.get("msg") or "").strip()
        if message:
            return message.splitlines()[0][:180]
    return ""


def render_job_snapshot(
    *,
    job_manager,
    job: dict,
    pill_map: dict[str, str],
    query_param: str,
    page_key: str,
) -> None:
    status = job.get("status", {})
    state = status.get("state", "UNKNOWN")
    logs = safe_logs(status)
    workspace = status.get("config", {}).get("workspace", "")
    last_event = _job_last_event_message(job)

    st.markdown("#### Task Snapshot")
    st.markdown(
        f"`{job['id']}` {format_status_pill(state, pill_map)}",
        unsafe_allow_html=True,
    )
    if workspace:
        st.caption(f"Workspace: `{workspace}`")
    updated_at = float(status.get("updated_at") or 0)
    if updated_at:
        st.caption(
            f"Updated: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(updated_at))}",
        )
    if last_event:
        st.caption(f"Last: {last_event}")

    col_open, col_stop = st.columns(2)
    with col_open:
        if st.button("Open full view", use_container_width=True, key=f"open_snapshot_{page_key}"):
            st.query_params[query_param] = job["id"]
            st.rerun()
    with col_stop:
        if state == "RUNNING":
            if st.button("Stop", use_container_width=True, key=f"stop_snapshot_{page_key}"):
                job_manager.cancel_job(job["id"])
                st.rerun()

    render_step_progress(logs, state, is_evolution=query_param == "evo_job_id")
    render_token_usage_bar(logs, int(st.session_state.max_tokens))

    with st.expander("Recent log", expanded=state in ACTIVE_JOB_STATES):
        render_events(
            logs[-20:],
            "No logs yet.",
            show_verbose=False,
            page_key=f"{page_key}_snapshot",
        )

    if status.get("report"):
        with st.expander("Report preview", expanded=False):
            st.markdown(status["report"][:2000])


def _reset_job_filters(prefix: str) -> None:
    st.session_state[f"{prefix}_job_state_filter"] = "All states"
    st.session_state[f"{prefix}_job_workspace_filter"] = "All workspaces"
    st.session_state[f"{prefix}_job_text_filter"] = ""
    st.session_state[f"{prefix}_job_focus_id"] = ""
    for suffix in ("state", "workspace", "search", "focus_id"):
        st.query_params.pop(f"{prefix}_{suffix}", None)


def _open_page(page: str) -> None:
    st.session_state.page = page
    st.rerun()


def render_live_protocol_readiness(workspace: Path) -> bool:
    proto_path = workspace / "protocol.md"
    draft_text = (st.session_state.get("protocol_md") or "").strip()
    saved_text = proto_path.read_text(encoding="utf-8") if proto_path.exists() else ""

    st.markdown("### Protocol Readiness")
    st.caption("Live Run always uses `protocol.md` from selected workspace.")

    if proto_path.exists():
        st.success(f"`protocol.md` ready in `{workspace.name}`.")
        col_primary, col_secondary = st.columns([1, 1])
        if draft_text and draft_text != saved_text.strip():
            st.warning("Current Protocol Wizard draft differs from saved `protocol.md`.")
            with col_primary:
                if st.button(
                    "Save current draft",
                    use_container_width=True,
                    key="btn_save_current_protocol_draft",
                ):
                    save_protocol(workspace, st.session_state.protocol_md)
                    st.toast("Updated `protocol.md` from current draft.")
                    st.rerun()
            with col_secondary:
                if st.button(
                    "Open Protocol Wizard",
                    use_container_width=True,
                    key="btn_open_protocol_wizard_ready",
                ):
                    _open_page("wizard")
        else:
            with col_primary:
                if st.button(
                    "Review in Protocol Wizard",
                    use_container_width=True,
                    key="btn_review_protocol_wizard_ready",
                ):
                    _open_page("wizard")

        with st.expander("Preview `protocol.md`", expanded=False):
            st.code(saved_text[:1800], language="markdown")
            render_protocol_quality(saved_text)
        return True

    if draft_text:
        st.warning("No saved `protocol.md` yet. Current Protocol Wizard draft can be reused here.")
        col_save, col_wizard = st.columns([1, 1])
        with col_save:
            if st.button(
                "Save current draft as protocol.md",
                type="primary",
                use_container_width=True,
                key="btn_save_protocol_from_draft",
            ):
                save_protocol(workspace, st.session_state.protocol_md)
                st.toast(f"Saved `protocol.md` to `{workspace.name}`.")
                st.rerun()
        with col_wizard:
            if st.button(
                "Open Protocol Wizard",
                use_container_width=True,
                key="btn_open_protocol_wizard_missing",
            ):
                _open_page("wizard")
        with st.expander("Preview current draft", expanded=False):
            st.code(draft_text[:1800], language="markdown")
            render_protocol_quality(draft_text)
        return False

    st.error("No `protocol.md` found for this workspace.")
    col_create, col_refresh = st.columns([1, 1])
    with col_create:
        if st.button(
            "Create in Protocol Wizard",
            type="primary",
            use_container_width=True,
            key="btn_create_protocol_wizard",
        ):
            _open_page("wizard")
    with col_refresh:
        if st.button(
            "Refresh status",
            use_container_width=True,
            key="btn_refresh_protocol_status",
        ):
            st.rerun()
    return False


def render_job_board(
    *,
    job_manager,
    job_type: str,
    pill_map: dict[str, str],
    query_param: str,
    board_title: str,
    empty_title: str,
    empty_description: str,
    auto_refresh_key: str,
) -> None:
    all_jobs = collect_jobs(job_manager, job_type)
    st.markdown(f"### {board_title}")
    st.caption("Status counts first. Then filters. Then active queue and history.")

    if not all_jobs:
        st.info(f"{empty_title} {empty_description}")
        return

    summary = summarize_jobs(all_jobs)
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Total", summary["total"])
    with m2:
        st.metric(
            "Active queue",
            summary["active"],
            delta=f"{summary['running']} running / {summary['pending']} queued",
            delta_color="off",
        )
    with m3:
        st.metric(
            "Finished",
            summary["finished"],
            delta=f"{summary['success']} success",
            delta_color="off",
        )
    with m4:
        st.metric(
            "Needs attention",
            summary["needs_attention"],
            delta=f"{summary['workspaces']} workspace(s)",
            delta_color="off",
        )

    workspace_options = ["All workspaces"] + sorted(
        {
            workspace_name
            for workspace_name in (_job_workspace_name(job) for job in all_jobs)
            if workspace_name
        },
    )
    _ensure_board_filter_state(job_type, workspace_options)
    filter_col1, filter_col2, filter_col3, filter_col4 = st.columns([1.2, 1.2, 1.8, 0.8])
    with filter_col1:
        state_filter = st.selectbox("State", BOARD_STATE_OPTIONS, key=f"{job_type}_job_state_filter")
    with filter_col2:
        workspace_filter = st.selectbox(
            "Workspace",
            workspace_options,
            key=f"{job_type}_job_workspace_filter",
        )
    with filter_col3:
        text_filter = st.text_input(
            "Search",
            key=f"{job_type}_job_text_filter",
            placeholder="Filter by task id, workspace, or state",
        )
    with filter_col4:
        auto_refresh = st.toggle(
            "Auto-refresh",
            value=st.session_state.get(auto_refresh_key, True),
            key=f"{job_type}_job_autorefresh",
            help="Refresh task list while queue still active.",
        )
        st.session_state[auto_refresh_key] = auto_refresh
    _persist_board_filters(
        job_type,
        state_filter=state_filter,
        workspace_filter=workspace_filter,
        text_filter=text_filter,
    )

    filtered_jobs = filter_jobs(
        all_jobs,
        state_filter=state_filter,
        workspace_filter=workspace_filter,
        text_query=text_filter,
    )
    filters_active = (
        state_filter != "All states"
        or workspace_filter != "All workspaces"
        or bool(text_filter.strip())
    )
    st.caption(f"{len(filtered_jobs)} of {summary['total']} tasks shown")

    if not filtered_jobs:
        st.warning("No matching tasks.")
        st.caption("Clear one or more filters to get back to full queue.")
        _set_query_param(f"{job_type}_focus_id", "", default="")
        if st.button("Clear filters", key=f"btn_clear_{job_type}_filters"):
            _reset_job_filters(job_type)
            st.rerun()
        return

    active_jobs = [
        job for job in filtered_jobs
        if job.get("status", {}).get("state") in ACTIVE_JOB_STATES
    ]
    history_jobs = [
        job for job in filtered_jobs
        if job.get("status", {}).get("state") not in ACTIVE_JOB_STATES
    ]

    focus_id = _ensure_focus_state(job_type, filtered_jobs)
    _set_query_param(f"{job_type}_focus_id", focus_id, default="")
    selected_job = next(
        (job for job in filtered_jobs if job.get("id") == focus_id),
        filtered_jobs[0],
    )

    list_col, detail_col = st.columns([1.7, 1])
    with list_col:
        if active_jobs:
            st.markdown(f"#### Active Queue ({len(active_jobs)})")
            for job in active_jobs:
                render_job_card(
                    job_manager=job_manager,
                    job=job,
                    pill_map=pill_map,
                    query_param=query_param,
                )
        if history_jobs:
            expanded = not active_jobs and not filters_active
            with st.expander(f"History ({len(history_jobs)})", expanded=expanded):
                for job in history_jobs[:20]:
                    render_job_card(
                        job_manager=job_manager,
                        job=job,
                        pill_map=pill_map,
                        query_param=query_param,
                    )

    with detail_col:
        st.selectbox(
            "Peek task",
            options=[job["id"] for job in filtered_jobs],
            key=f"{job_type}_job_focus_id",
            format_func=lambda job_id: _job_label(
                next(job for job in filtered_jobs if job["id"] == job_id),
            ),
        )
        _set_query_param(
            f"{job_type}_focus_id",
            st.session_state.get(f"{job_type}_job_focus_id", ""),
            default="",
        )
        selected_job = next(
            job for job in filtered_jobs
            if job["id"] == st.session_state.get(f"{job_type}_job_focus_id", selected_job["id"])
        )
        render_job_snapshot(
            job_manager=job_manager,
            job=selected_job,
            pill_map=pill_map,
            query_param=query_param,
            page_key=f"{job_type}_board",
        )

    if summary["active"] and auto_refresh:
        time.sleep(3)
        st.rerun()
