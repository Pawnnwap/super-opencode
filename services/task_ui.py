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
from services.settings import apply_api_config, save_settings
from services.supervisor_config_builder import build_supervisor_config
from supervisor.analyzers.codebase_analyzer import snapshot_codebase
from supervisor.protocols.meta_protocol_builder import MetaProtocolBuilder, write_meta_protocol
from supervisor.protocols.protocol import parse_protocol_text


class JobStatusScreen:
    def __init__(
        self,
        *,
        job_manager,
        job_id: str,
        title: str,
        page_key: str,
        query_param: str,
        report_filename_prefix: str,
        running_message: str,
        is_evolution: bool = False,
        pill_map: dict[str, str],
    ):
        self.job_manager = job_manager
        self.job_id = job_id
        self.title = title
        self.page_key = page_key
        self.query_param = query_param
        self.report_filename_prefix = report_filename_prefix
        self.running_message = running_message
        self.is_evolution = is_evolution
        self.pill_map = pill_map

    def render(self) -> None:
        status = self.job_manager.get_job_status(self.job_id)
        if not status:
            st.error(f"Job {self.job_id} not found.")
            if st.button("Back to Setup"):
                del st.query_params[self.query_param]
                st.rerun()
            return

        state = status["state"]
        logs = safe_logs(status)

        job_type = status.get("type")
        sibling_ids: list[str] = []
        if job_type:
            for job_id in self.job_manager.store.list_jobs():
                sibling_status = self.job_manager.get_job_status(job_id)
                if not sibling_status or sibling_status.get("type") != job_type:
                    continue
                if sibling_status.get("state") == "RUNNING" or job_id == self.job_id:
                    sibling_ids.append(job_id)

        siblings_status = {
            job_id: self.job_manager.get_job_status(job_id) or {}
            for job_id in sibling_ids
        }
        ordered = sorted(
            set(sibling_ids),
            key=lambda job_id: (
                job_id != self.job_id,
                -float(siblings_status.get(job_id, {}).get("updated_at") or 0),
            ),
        )
        if len(ordered) > 1:

            def _fmt(job_id: str) -> str:
                sibling_status = siblings_status.get(job_id, {})
                state_name = sibling_status.get("state", "?")
                workspace = sibling_status.get("config", {}).get("workspace", "")
                workspace_name = Path(workspace).name if workspace else ""
                suffix = f" — {workspace_name}" if workspace_name else ""
                return f"{job_id} [{state_name}]{suffix}"

            index = ordered.index(self.job_id) if self.job_id in ordered else 0
            picked = st.selectbox(
                "Active tasks",
                ordered,
                index=index,
                format_func=_fmt,
                key=f"switcher_{self.page_key}",
            )
            if picked != self.job_id:
                st.query_params[self.query_param] = picked
                st.rerun()

        col_h1, col_h2, col_h3 = st.columns([3, 1, 1])
        with col_h1:
            st.markdown(f"### {self.title}: `{self.job_id}`")
        with col_h2:
            if state == "RUNNING":
                if st.button("⏹ Stop", use_container_width=True, key=f"stop_{self.page_key}"):
                    self.job_manager.cancel_job(self.job_id)
                    st.rerun()
            elif st.button("🗑 Clear", use_container_width=True, key=f"clear_{self.page_key}"):
                del st.query_params[self.query_param]
                st.rerun()
        with col_h3:
            if st.button("🔄 Refresh", use_container_width=True, key=f"refresh_{self.page_key}"):
                st.rerun()

        col_main, col_side = st.columns([2, 1])
        with col_main:
            render_step_progress(logs, state, is_evolution=self.is_evolution)
            st.markdown(f"#### 🖥️ {'Evolution' if self.is_evolution else 'Live'} Log")
            render_events(logs, "— waiting for logs —", show_verbose=True, page_key=self.page_key)

        with col_side:
            st.markdown("#### ℹ️ Details")
            st.markdown(f"**State:** {state}")
            st.markdown(
                f"**Started:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(status.get('updated_at', 0)))}"
            )
            sup_model = st.session_state.get("supervisor_model", "") or "(not set)"
            oc_model = st.session_state.get("opencode_model", "") or "(not set)"
            st.markdown(f"**Supervisor model:** `{sup_model}`")
            st.markdown(f"**Opencode model:** `{oc_model}`")
            render_token_usage_bar(logs, int(st.session_state.max_tokens))

            if status.get("report"):
                report_title = "📊 Evolution Report" if self.is_evolution else "📊 Report"
                st.markdown(f"#### {report_title}")
                with st.expander("View Report", expanded=True):
                    st.markdown(status["report"])
                    st.download_button(
                        "⬇ Download",
                        data=status["report"],
                        file_name=f"{self.report_filename_prefix}_{self.job_id}.md",
                        mime="text/markdown",
                    )

        if state == "RUNNING":
            st.info(f"{'🧬' if self.is_evolution else '🏃'} {self.running_message}")
            try:
                time.sleep(2)
            finally:
                st.rerun()


def show_task_form(*, job_manager, workspace: Path | None) -> None:
    if not workspace:
        st.warning("Set a workspace path in Configuration first.")
        return
    if not workspace.exists():
        st.error(f"Workspace does not exist: `{workspace}`")
        return
    proto_path = workspace / "protocol.md"
    if not proto_path.exists():
        st.error("No protocol.md in workspace. Create one via Protocol Wizard first.")
        return

    no_existing_jobs = not any(
        (job_manager.store.get_job_state(job_id) or {}).get("type") == "run"
        for job_id in job_manager.store.list_jobs()
    )
    with st.expander("Start New Task", expanded=no_existing_jobs):
        st.markdown(f"**Primary workspace:** `{workspace}`")
        st.caption("Each task gets its own isolated workspace directory.")
        col1, col2 = st.columns([2, 1])
        with col1:
            plan_rounds = st.number_input(
                "Plan mode rounds",
                min_value=0,
                max_value=10,
                value=int(st.session_state.plan_mode_rounds),
                key="task_plan_mode_rounds",
                help="Number of planning rounds before execution",
            )
            enable_scanner = st.toggle(
                "Enable Python scanner",
                value=bool(st.session_state.enable_python_scanner),
                key="task_enable_python_scanner",
                help="Run the Python vulnerability scanner before execution",
            )
        with col2:
            if st.button("Start Task", type="primary", use_container_width=True, key="btn_start_task"):
                st.session_state.plan_mode_rounds = plan_rounds
                st.session_state.enable_python_scanner = enable_scanner
                save_settings()
                apply_api_config()
                config = build_supervisor_config(
                    st.session_state,
                    proto_path,
                    workspace,
                    plan_mode_rounds=int(plan_rounds),
                )
                try:
                    job_id = job_manager.enqueue_job("run", config)
                    st.toast(f"Task started: `{job_id}` in `{workspace.name}`")
                    st.rerun()
                except ValueError as exc:
                    st.toast(f"❌ {exc}")


def show_evo_setup_screen(*, job_manager, render_existing_protocol_banner) -> None:
    st.markdown(
        "Point the supervisor + opencode at **this codebase itself**. "
        "Describe what you want improved or debugged — the system will "
        "auto-generate a `meta_protocol.md` from the live source tree, "
        "then run the full supervisor loop."
    )

    if not st.session_state.openai_key:
        st.warning("Enter your OpenAI API key in the Protocol Wizard config panel first.")
        return

    repo_root = Path(__file__).resolve().parents[1]
    st.info(f"**Repo root (workspace):** `{repo_root}`")

    def _on_reuse_meta(text: str):
        try:
            proto = parse_protocol_text(text)
            st.session_state.evo_goal = proto.target_section
            st.session_state.evo_extra_restrictions = proto.restrictions_section
            st.rerun()
        except Exception as exc:
            st.error(f"Failed to parse meta_protocol.md: {exc}")

    render_existing_protocol_banner(
        repo_root / "meta_protocol.md",
        "evo_meta_protocol_md",
        reuse_label="♻️  Use existing meta_protocol.md",
        on_reuse=_on_reuse_meta,
    )

    if st.session_state.evo_wizard_step == 0:
        st.markdown("### 🎯 What do you want to evolve?")
        for section_key, label, height in [
            ("evo_goal", "**Evolution goal**", 130),
            ("evo_extra_restrictions", "**Extra restrictions**", 80),
        ]:
            st.markdown('<div class="card">', unsafe_allow_html=True)
            st.markdown(label)
            st.text_area(section_key, key=section_key, height=height, label_visibility="collapsed")
            st.markdown("</div>", unsafe_allow_html=True)

        col_gen, col_snap, _ = st.columns([1, 1, 3])
        with col_gen:
            if st.button("🧠 Generate meta_protocol.md", type="primary"):
                generate_meta_protocol(repo_root)
        with col_snap:
            if st.button("🔍 Preview snapshot"):
                with st.spinner("Scanning..."):
                    snap = snapshot_codebase(repo_root)
                    st.code(snap.tree())
        return

    st.markdown("### 📄 Generated `meta_protocol.md`")
    st.text_area(
        "evo_proto_edit",
        key="evo_meta_protocol_md",
        height=340,
        label_visibility="collapsed",
    )
    col_a, col_b, _ = st.columns([1, 1, 2])
    with col_a:
        if st.button("🚀 Launch Evolution", type="primary"):
            save_settings()
            apply_api_config()
            proto_path = write_meta_protocol(st.session_state.evo_meta_protocol_md, repo_root)
            config = build_supervisor_config(st.session_state, proto_path, repo_root)
            try:
                job_id = job_manager.enqueue_job("evolve", config)
                st.query_params["evo_job_id"] = job_id
                st.rerun()
            except ValueError as exc:
                st.toast(f"❌ {exc}")
    with col_b:
        if st.button("🔄 Regenerate"):
            st.session_state.evo_wizard_step = 0
            st.rerun()


def generate_meta_protocol(repo_root: Path) -> None:
    apply_api_config()
    with st.spinner("Generating meta_protocol.md..."):
        snap = snapshot_codebase(repo_root)
        builder = MetaProtocolBuilder(model=st.session_state.supervisor_model)
        try:
            meta_md = builder.build(
                evolution_goal=st.session_state.evo_goal,
                snapshot=snap,
                extra_restrictions=st.session_state.evo_extra_restrictions,
            )
            st.session_state.evo_meta_protocol_md = meta_md
            st.session_state.evo_wizard_step = 1
            st.rerun()
        except Exception as exc:
            st.error(f"Generation failed: {exc}")


def page_run(*, job_manager, pill_map: dict[str, str]) -> None:
    st.markdown("# Live Run")
    job_id = st.query_params.get("run_job_id")
    if job_id:
        if st.button("Back to Task List", key="btn_back_to_list"):
            st.query_params.pop("run_job_id", None)
            st.rerun()
        JobStatusScreen(
            job_manager=job_manager,
            job_id=job_id,
            title="Task",
            page_key="run_view",
            query_param="run_job_id",
            report_filename_prefix="report",
            running_message="Task running. You can safely close this tab or refresh.",
            pill_map=pill_map,
        ).render()
        return

    workspace = Path(st.session_state.workspace) if st.session_state.workspace else None
    show_task_form(job_manager=job_manager, workspace=workspace)

    st.markdown("---")
    col_nav, col_auto = st.columns([3, 1])
    with col_nav:
        st.markdown("Manage multiple concurrent tasks with isolated workspaces.")
    with col_auto:
        auto_refresh = st.toggle(
            "Auto-refresh",
            value=st.session_state.get("_run_list_autorefresh", True),
            key="_run_list_autorefresh_toggle",
            help="Re-fetch task states every few seconds while any task is running.",
        )
        st.session_state["_run_list_autorefresh"] = auto_refresh

    all_run_jobs = []
    for job_id in job_manager.store.list_jobs():
        status = job_manager.get_job_status(job_id)
        if status and status.get("type") == "run":
            all_run_jobs.append({"id": job_id, "status": status})
    all_run_jobs.sort(key=lambda job: job["status"].get("updated_at", 0), reverse=True)

    running_jobs: list[dict] = []
    if not all_run_jobs:
        st.info("No run tasks yet. Use **Start New Task** above to launch one.")
    else:
        running_jobs = [
            job for job in all_run_jobs if job["status"].get("state") == "RUNNING"
        ]
        completed_jobs = [
            job for job in all_run_jobs if job["status"].get("state") != "RUNNING"
        ]
        if running_jobs:
            st.markdown(f"### Active Tasks ({len(running_jobs)})")
            for job in running_jobs:
                render_job_card(
                    job_manager=job_manager,
                    job=job,
                    pill_map=pill_map,
                    query_param="run_job_id",
                )
        if completed_jobs:
            with st.expander(f"Completed Tasks ({len(completed_jobs)})", expanded=False):
                for job in completed_jobs[:10]:
                    render_job_card(
                        job_manager=job_manager,
                        job=job,
                        pill_map=pill_map,
                        query_param="run_job_id",
                    )
        st.markdown("---")

    if running_jobs and auto_refresh:
        time.sleep(3)
        st.rerun()


def page_evolve(
    *,
    job_manager,
    pill_map: dict[str, str],
    render_existing_protocol_banner,
) -> None:
    st.markdown("# Self-Evolution")
    job_id = st.query_params.get("evo_job_id")
    if not job_id:
        for candidate_id in job_manager.store.list_jobs():
            status = job_manager.get_job_status(candidate_id)
            if status and status.get("type") == "evolve" and status.get("state") == "RUNNING":
                st.query_params["evo_job_id"] = candidate_id
                st.rerun()
                return
    if job_id:
        JobStatusScreen(
            job_manager=job_manager,
            job_id=job_id,
            title="Evolution Job",
            page_key="evo",
            query_param="evo_job_id",
            report_filename_prefix="evo_report",
            running_message="Evolution in progress...",
            is_evolution=True,
            pill_map=pill_map,
        ).render()
        return

    show_evo_setup_screen(
        job_manager=job_manager,
        render_existing_protocol_banner=render_existing_protocol_banner,
    )
