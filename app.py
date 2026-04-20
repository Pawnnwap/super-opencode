"""app.py  —  opencode Supervisor UI
Run with:  streamlit run app.py
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import streamlit as st

from services.app_bootstrap import (
    auto_upgrade_dcp as _auto_upgrade_dcp,
    auto_upgrade_opencode as _auto_upgrade_opencode,
)
from services.connectivity import (
    run_test_with_timeout as _run_test_with_timeout,
    test_opencode_connectivity,
    test_supervisor_connectivity,
)
from services.job_manager import JobManager
from services.log_ui import (
    format_status_pill as _format_status_pill,
    safe_logs as _safe_logs_impl,
)
from services.opencode_config import (
    add_custom_provider_to_config as _add_custom_provider_to_config,
    fetch_opencode_models as _fetch_opencode_models,
    find_opencode_config_dir as _find_opencode_config_dir,
    get_opencode_config_file as _get_opencode_config_file_impl,
)
from services.protocol_ui import (
    render_existing_protocol_banner as _render_existing_protocol_banner_impl,
    render_protocol_quality as _render_protocol_quality_impl,
    save_protocol as _save_protocol_impl,
)
from services.settings import apply_api_config, load_settings, save_settings
from services.supervisor_config_builder import (
    build_supervisor_config as _build_supervisor_config_impl,
)
from services.task_ui import (
    page_evolve as _page_evolve_impl,
    page_run as _page_run_impl,
)
from services.workspace_cleanup import (
    clean_workspace_artifacts as _clean_workspace_artifacts,
)
from supervisor.monitoring.session_tracker import estimate_tokens
from supervisor.protocols.protocol_wizard import ProtocolWizard
from supervisor.utils.config import SupervisorConfig


# ── Job Manager ──────────────────────────────────────────────────────────── #

@st.cache_resource
def _get_job_manager():
    return JobManager(".job_store")


job_manager = _get_job_manager()


# ── Page config & CSS ────────────────────────────────────────────────────── #

st.set_page_config(
    page_title="opencode Supervisor",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

if not st.session_state.get("_upgrade_done"):
    _auto_upgrade_opencode()
    _auto_upgrade_dcp()
    st.session_state["_upgrade_done"] = True

CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600&family=Syne:wght@400;700;800&display=swap');

html, body, [class*="css"] {
    font-family: 'Syne', sans-serif;
    background-color: #0d0f14;
    color: #c9d1d9;
}
section[data-testid="stSidebar"] { background: #0a0c10; border-right: 1px solid #21262d; }
h1 { font-family: 'Syne', sans-serif; font-weight: 800; color: #58a6ff; letter-spacing: -1px; }
h2 { font-family: 'Syne', sans-serif; font-weight: 700; color: #79c0ff; }
h3 { font-family: 'Syne', sans-serif; font-weight: 600; color: #9ecbff; }
textarea, input[type="text"], input[type="number"], input[type="password"] {
    font-family: 'JetBrains Mono', monospace !important;
    background: #161b22 !important; color: #e6edf3 !important;
    border: 1px solid #30363d !important; border-radius: 6px !important;
}
button[kind="primary"], .stButton > button {
    background: #1f6feb !important; border: none !important; color: #fff !important;
    font-family: 'JetBrains Mono', monospace !important; font-weight: 600 !important;
    border-radius: 6px !important; padding: 0.4rem 1.2rem !important; transition: background 0.15s;
}
button[kind="primary"]:hover, .stButton > button:hover { background: #388bfd !important; }
.log-box {
    background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
    padding: 1rem 1.2rem; font-family: 'JetBrains Mono', monospace; font-size: 0.78rem;
    line-height: 1.7; max-height: 520px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
}
.log-info              { color: #8b949e; }
.log-warn              { color: #e3b341; }
.log-error             { color: #f85149; }
.log-success           { color: #3fb950; font-weight: 600; }
.log-opencode_prompt   { color: #79c0ff; white-space: pre-wrap; }
.log-opencode_output   { color: #c9d1d9; white-space: pre-wrap; }
.log-supervisor_response { color: #d2a8ff; white-space: pre-wrap; }
.log-supervisor_read_files { color: #a5d6ff; white-space: pre-wrap; }
.log-step              { color: #56d364; font-weight: 600; white-space: pre-wrap; }
.log-phase_transition  { color: #f0883e; font-weight: 600; white-space: pre-wrap; }
.log-step_progress     { color: #a5d6ff; white-space: pre-wrap; }
.log-heartbeat         { color: #39d353; white-space: pre-wrap; font-style: italic; }
.log-rule { color: #21262d; display:block; }
.log-block-hdr { color: #58a6ff; font-weight: 600; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.5px; }
.log-supervisor_suggestions { color: #d2a8ff; white-space: pre-wrap; }
.log-log-plan_phase { color: #79c0ff; font-style: italic; white-space: pre-wrap; }
div[data-testid="stProgress"] > div > div { background-color: #21262d !important; }
div[data-testid="stProgress"] > div > div > div { background: linear-gradient(90deg, #1f6feb, #58a6ff) !important; }
.card { background: #161b22; border: 1px solid #21262d; border-radius: 10px; padding: 1.2rem 1.5rem; margin-bottom: 1rem; }
.pill { display: inline-block; padding: 2px 12px; border-radius: 999px; font-size: 0.75rem; font-family: 'JetBrains Mono', monospace; font-weight: 600; margin-left: 8px; }
.pill-idle    { background:#21262d; color:#8b949e; }
.pill-running { background:#1f6feb22; color:#58a6ff; border: 1px solid #1f6feb55; }
.pill-success { background:#23863633; color:#3fb950; border: 1px solid #23863655; }
.pill-failure { background:#da363333; color:#f85149; border: 1px solid #da363355; }
.proto-preview {
    background: #0d1117; border-left: 3px solid #1f6feb; padding: 0.8rem 1rem;
    border-radius: 0 6px 6px 0; font-family: 'JetBrains Mono', monospace;
    font-size: 0.78rem; white-space: pre-wrap; color: #c9d1d9;
}
.expander-content { padding: 0.5rem 0; }
div[data-testid="stExpander"] { border: 1px solid #21262d !important; background: #161b22 !important; border-radius: 8px !important; }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ── UI Utilities ─────────────────────────────────────────────────────────── #

@contextlib.contextmanager
def _card():
    st.markdown('<div class="card">', unsafe_allow_html=True)
    yield
    st.markdown("</div>", unsafe_allow_html=True)


def _run_test_with_timeout(test_fn, test_name: str, timeout_seconds: int = 30) -> tuple[bool, str]:
    try:
        return _run_with_timeout(test_fn, seconds=timeout_seconds)
    except TimeoutError:
        return False, f"{test_name} timed out."
    except Exception as exc:
        return False, f"{test_name} failed: {exc}"


def _render_test_result(test_name: str, ok: bool, msg: str) -> None:
    (st.success if ok else st.error)(f"{'\u2705' if ok else '\u274c'} Test {test_name}: {msg}")


def _do_test(test_name: str, test_fn, state_key: str) -> None:
    with st.spinner(f"Testing {test_name}\u2026"):
        ok, msg = test_fn()
    st.session_state[state_key] = ok
    _render_test_result(test_name, ok, msg)


def _render_model_selector(label: str, models: list, session_key: str, select_key: str, fallback_key: str,
                           help_text: str = None, placeholder: str = None, show_warning: bool = True) -> str:
    if models:
        current = st.session_state.get(session_key, "")
        default_idx = models.index(current) if current in models else 0
        st.session_state[session_key] = st.selectbox(
            label, options=models, index=default_idx, key=select_key, help=help_text)
    else:
        if show_warning:
            st.warning("No models returned by 'opencode models'.")
        st.session_state[session_key] = st.text_input(
            label, key=fallback_key, value=st.session_state.get(session_key, ""), placeholder=placeholder)
    return st.session_state[session_key]


def bound_text_input(label: str, session_key: str, placeholder: str = None, type: str = "default",
                     help_text: str = None) -> str:
    st.session_state[session_key] = st.text_input(
        label, key=f"cfg_{session_key}", value=st.session_state.get(session_key, ""),
        placeholder=placeholder, type=type, help=help_text)
    return st.session_state[session_key]


def _clear_page_state(target_page: str) -> None:
    """Clear UI state from other pages when navigating to a new page."""
    if target_page != "wizard":
        st.session_state.show_custom_model_form = False
    if target_page != "run":
        st.query_params.pop("run_job_id", None)
    if target_page != "evolve":
        st.query_params.pop("evo_job_id", None)


def _safe_logs(status: dict) -> list[dict]:
    return _safe_logs_impl(status)


def _redirect_if_locked(page: str, warning: str) -> None:
    """Redirect to wizard with a warning if connectivity tests haven't passed."""
    st.session_state.page = "wizard"
    st.session_state["_redirect_warning"] = warning
    st.rerun()


def _get_opencode_config_file(config_dir: Path) -> Path:
    return _get_opencode_config_file_impl(
        config_dir,
        Path(__file__).parent.resolve(),
        on_info=st.info,
        on_warning=st.warning,
    )


def _build_supervisor_config(
    protocol_path: Path,
    workspace: Path,
    **overrides,
) -> SupervisorConfig:
    return _build_supervisor_config_impl(
        st.session_state,
        protocol_path,
        workspace,
        **overrides,
    )


# ── Startup initialisation ───────────────────────────────────────────────── #


if not st.session_state.get("_mcp_config_done"):
    _mcp_dir = _find_opencode_config_dir()
    if _mcp_dir:
        _get_opencode_config_file(_mcp_dir)
    st.session_state["_mcp_config_done"] = True

if not st.session_state.get("_artifact_clean_done"):
    _ws = st.session_state.get("workspace", "")
    if _ws:
        _clean_workspace_artifacts(Path(_ws))
    st.session_state["_artifact_clean_done"] = True

_persisted = load_settings()

defaults = {
    "page": "wizard", "protocol_md": "", "log_events": [], "run_state": "idle",
    "final_report": "", "wizard_step": 0, "raw_input": "", "raw_target": "",
    "raw_restrictions": "", "openai_key": "", "base_url": "", "workspace": "",
    "supervisor_model": "", "supervisor_model_backup": "", "opencode_model": "",
    "opencode_model_backup": "", "opencode_executable": "", "max_retries": 3,
    "context_threshold": 60, "max_tokens": 150000, "timeout": 120,
    "plan_mode_rounds": 1, "protected_files": [], "_last_workspace": "",
    "evo_goal": "", "evo_extra_restrictions": "", "evo_meta_protocol_md": "",
    "evo_log_events": [], "evo_run_state": "idle", "evo_report": "",
    "evo_wizard_step": 0, "self_evolution_verbose": False, "verbose_log": True,
    "_run_heartbeat": 0, "_evo_heartbeat": 0,
    "opencode_test_passed": False, "supervisor_test_passed": False,
    "opencode_models": [], "enable_python_scanner": True,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = _persisted.get(k, v)

if not st.session_state["opencode_models"]:
    st.session_state["opencode_models"] = _fetch_opencode_models()


# ── Sidebar ──────────────────────────────────────────────────────────────── #

_PILL_MAP = {
    "PENDING": '<span class="pill pill-idle">queued…</span>',
    "RUNNING": '<span class="pill pill-running">running</span>',
    "SUCCESS": '<span class="pill pill-success">done ✓</span>',
    "FAILED": '<span class="pill pill-failure">failed ✗</span>',
    "CANCELLED": '<span class="pill pill-failure">cancelled ⏹</span>',
}


def _any_job_running() -> bool:
    return any(
        job_manager.get_job_status(jid) and job_manager.get_job_status(jid).get("state") == "RUNNING"
        for jid in job_manager.store.list_jobs()
    )


def _render_status_pill(state: str) -> str:
    return _format_status_pill(state, _PILL_MAP)


# Compute once per render cycle so sidebar and router share the result.
_jobs_running = _any_job_running()


def _evo_job_passed() -> bool:
    for jid in job_manager.store.list_jobs():
        status = job_manager.get_job_status(jid)
        if status and status.get("type") == "evolve" and status.get("state") == "SUCCESS":
            for log in reversed(_safe_logs(status)):
                msg = log.get("msg", "")
                if msg and ("Tests: All passed" in msg or log.get("level") == "success"):
                    return True
    return False


with st.sidebar:
    st.markdown("## 🤖 opencode<br>**Supervisor**", unsafe_allow_html=True)
    st.markdown("---")

    for param_key, label in (("run_job_id", "**Live Run**"), ("evo_job_id", "**Self-evo**")):
        jid = st.query_params.get(param_key)
        if jid:
            s = job_manager.get_job_status(jid)
            if s:
                st.markdown(f"{label} {_render_status_pill(s['state'])}", unsafe_allow_html=True)

    st.markdown("---")

    tests_passed = (
        (st.session_state.opencode_test_passed and st.session_state.supervisor_test_passed)
        or _jobs_running
        or _evo_job_passed()
    )

    for key, label in {"wizard": "① Protocol Wizard", "run": "② Live Run", "evolve": "③ Self-Evolution"}.items():
        locked = key != "wizard" and not tests_passed
        active = st.session_state.page == key
        if locked:
            st.button(f"🔒 {label}", key=f"nav_{key}", use_container_width=True, disabled=True)
        elif st.button(label, key=f"nav_{key}", use_container_width=True,
                       type="primary" if active else "secondary"):
            _clear_page_state(key)
            st.session_state.page = key
            st.rerun()

    if not tests_passed:
        st.caption("🔒 Run & Self-Evolution locked — pass connectivity tests first.")

    # Custom model form (wizard page only)
    if st.session_state.page == "wizard":
        st.markdown("---")
        st.markdown("### 🤖 Add Custom Model for Opencode")
        if "show_custom_model_form" not in st.session_state:
            st.session_state.show_custom_model_form = False
        if st.button("➕ Add Custom Model for Opencode", key="btn_add_custom_model"):
            st.session_state.show_custom_model_form = True

        if st.session_state.show_custom_model_form:
            st.markdown("**Custom Service Configuration**")
            service_name = st.text_input("Service name", key="custom_service_name",
                                         placeholder="my-custom-service")
            base_url = st.text_input("Base URL", key="custom_base_url",
                                     placeholder="https://api.example.com/v1")
            api_key = st.text_input("API key", key="custom_api_key",
                                    type="password", placeholder="sk-...")
            st.markdown("**Model names** *(one per line)*")
            model_names_input = st.text_area(
                "Model names", key="custom_model_names", height=100,
                placeholder="qwen3-coder-plus\nqwen3-max\nkimi-k2-0905",
                label_visibility="collapsed",
            )
            if st.button("💾 Save Service", key="btn_save_custom_service"):
                model_names = [m.strip() for m in model_names_input.splitlines() if m.strip()]
                if not service_name.strip() or not base_url.strip() or not api_key.strip():
                    st.error("Please fill in service name, base URL, and API key.")
                elif not model_names:
                    st.error("Please enter at least one model name.")
                else:
                    try:
                        config_dir = _find_opencode_config_dir()
                        if config_dir is None:
                            st.error("Could not find or create opencode config directory.")
                        else:
                            config_file = _get_opencode_config_file(config_dir)
                            _add_custom_provider_to_config(
                                config_file, service_name.strip(), base_url.strip(),
                                api_key.strip(), model_names,
                            )
                            first_model = f"{service_name.strip()}/{model_names[0]}"
                            st.session_state.opencode_model = first_model
                            save_settings()
                            st.success(f"Service saved! Model '{first_model}' selected and persisted.")
                            st.info(f"Models can now be referenced as `{service_name.strip()}/<model-name>`")
                            st.session_state.show_custom_model_form = False
                            st.rerun()
                    except Exception as e:
                        st.error(f"Failed to save service: {e}")

    st.markdown("---")
    st.caption("streamlit · opencode")


# ── Connectivity tests ───────────────────────────────────────────────────── #

def test_opencode() -> tuple[bool, str]:
    return test_opencode_connectivity(
        st.session_state.opencode_executable,
        st.session_state.opencode_model,
        st.session_state.opencode_model_backup,
    )


def test_supervisor() -> tuple[bool, str]:
    return test_supervisor_connectivity(
        st.session_state.openai_key,
        st.session_state.supervisor_model or "gpt-4o",
        base_url=st.session_state.base_url or None,
    )


def _render_connectivity_tests() -> None:
    """Render the connectivity test section (shared by wizard page)."""
    st.markdown("---")
    st.markdown("### 🔌 Connectivity Tests")
    both_passed = st.session_state.opencode_test_passed and st.session_state.supervisor_test_passed
    if both_passed:
        st.success("✅ Both opencode and supervisor connectivity tests passed.")
    else:
        st.info("Run the tests below to verify opencode and supervisor are reachable.")

    col_t1, col_t2, col_t3 = st.columns(3)

    with col_t1:
        if st.button("\u25b6  Run Tests", type="primary", key="btn_run_tests"):
            if not st.session_state.workspace:
                st.error("Set a workspace path before running tests.")
            else:
                _do_test("opencode", test_opencode, "opencode_test_passed")
                _do_test("Supervisor", test_supervisor, "supervisor_test_passed")
    with col_t2:
        if st.button("Test opencode", key="btn_test_opencode"):
            if not st.session_state.workspace:
                st.error("Set a workspace path before testing.")
            else:
                _do_test("opencode", test_opencode, "opencode_test_passed")
    with col_t3:
        if st.button("Test Supervisor", key="btn_test_supervisor"):
            if not st.session_state.openai_key:
                st.error("Set an API key before testing.")
            else:
                _do_test("Supervisor", test_supervisor, "supervisor_test_passed")


# ── Protocol quality analysis ────────────────────────────────────────────── #

def _render_protocol_quality(text: str, detailed: bool = False) -> None:
    _render_protocol_quality_impl(text, detailed=detailed)


# ── Existing-protocol reuse banner ──────────────────────────────────────── #

def _render_existing_protocol_banner(
    proto_path: Path,
    state_key: str,
    reuse_label: str = "♻️  Use existing protocol.md",
    on_reuse=None,
) -> bool:
    return _render_existing_protocol_banner_impl(
        proto_path,
        state_key,
        reuse_label=reuse_label,
        on_reuse=on_reuse,
    )


# ═══════════════════════════════════════════════════════════════════════════ #
# PAGE 1 — Protocol Wizard                                                    #
# ═══════════════════════════════════════════════════════════════════════════ #

def _save_protocol() -> None:
    proto_path = _save_protocol_impl(
        Path(st.session_state.workspace),
        st.session_state.protocol_md,
    )
    st.session_state.protocol_saved_path = str(proto_path)


def page_wizard() -> None:
    from supervisor.runners.opencode_runner import find_opencode

    if st.session_state.get("_redirect_warning"):
        st.warning(st.session_state.pop("_redirect_warning"))

    st.markdown("# Protocol Wizard")
    st.markdown(
        "Fill in each section in plain language. The supervisor LLM will refine "
        "them into a clean, unambiguous `protocol.md`.",
    )

    try:
        find_opencode()
    except FileNotFoundError as e:
        st.error(str(e))
        st.stop()

    # ── Configuration expander ─────────────────────────────────────────── #
    with st.expander("⚙️  Configuration", expanded=st.session_state.wizard_step == 0):
        col1, col2 = st.columns(2)
        with col1:
            bound_text_input("API Key", "openai_key", placeholder="sk…", type="password")
            bound_text_input("Base URL (leave blank for OpenAI)", "base_url", placeholder="e.g. http://localhost:11434/v1")
            bound_text_input("Workspace path (absolute)", "workspace", placeholder="/home/user/myproject")
            if st.session_state.workspace != st.session_state.get("_last_workspace", ""):
                st.session_state.protected_files = []
                st.session_state._last_workspace = st.session_state.workspace
                st.session_state["_artifact_clean_done"] = False  # re-run clean on next rerun
            if not st.session_state.get("_artifact_clean_done"):
                _ws = st.session_state.get("workspace", "")
                if _ws:
                    _clean_workspace_artifacts(Path(_ws))
                st.session_state["_artifact_clean_done"] = True
            bound_text_input("Supervisor / wizard model", "supervisor_model", placeholder="e.g. gpt-4o, claude-3-5-sonnet, mistral-large")
            bound_text_input("Supervisor model backup", "supervisor_model_backup", placeholder="e.g. gpt-4o-mini (used when primary fails)")

        with col2:
            models = st.session_state.get("opencode_models", [])
            _render_model_selector(
                "Model", models, "opencode_model", "cfg_opencode_model_select", "cfg_opencode_model_fallback",
                help_text="Models returned by 'opencode models'")

            backup_models = [m for m in models if m != st.session_state.opencode_model] if models else []
            _render_model_selector(
                "opencode model backup", backup_models, "opencode_model_backup", "cfg_opencode_model_backup_select",
                "cfg_opencode_model_backup", help_text="Fallback model used when the primary model fails",
                placeholder="e.g. /my-provider/backup-model (used when primary fails)", show_warning=False)

            def _refresh_models():
                st.session_state["opencode_models"] = _fetch_opencode_models()
            st.button("🔄 Refresh models", key="btn_refresh_models", on_click=_refresh_models)

            st.session_state.max_retries = st.number_input(
                "Max retries", key="cfg_max_retries", min_value=1, max_value=20,
                value=int(st.session_state.max_retries))
            st.session_state.context_threshold = st.slider(
                "Context compaction threshold (%)", 20, 95, key="cfg_ctx_threshold",
                value=int(st.session_state.context_threshold))
            st.session_state.max_tokens = st.number_input(
                "Max tokens (model context window)", key="cfg_max_tokens",
                min_value=1000, max_value=1000000,
                value=int(st.session_state.max_tokens), step=1000)
            st.session_state.timeout = st.number_input(
                "Timeout (min)", key="cfg_timeout", min_value=1, max_value=999,
                value=min(max(int(st.session_state.timeout), 1), 999))
            st.session_state.enable_python_scanner = st.toggle(
                "Enable Python scanner", key="cfg_enable_python_scanner",
                value=bool(st.session_state.enable_python_scanner),
                help="Run the Python vulnerability scanner before each live run")

        # Protected files sub-expander
        with st.expander("🛡️  Protected Files", expanded=False):
            st.caption("Files that opencode cannot modify or delete")
            protected = st.session_state.get("protected_files", [])
            if not isinstance(protected, list):
                protected = []
            workspace_path = Path(st.session_state.workspace) if st.session_state.workspace else None
            all_files = []
            if workspace_path and workspace_path.exists():
                try:
                    from supervisor.utils.path_filters import should_skip_path
                    all_files = sorted([
                        str(f.relative_to(workspace_path)).replace("\\", "/")
                        for f in workspace_path.rglob("*")
                        if f.is_file() and not should_skip_path(f, extra_dirs=["debug"])
                    ])
                except Exception:
                    pass

            available_files = [f for f in all_files if f not in set(protected)]
            st.markdown("**Add protected files:**")
            selected_to_add = st.multiselect(
                "Select files to protect", options=available_files,
                key="protected_files_multiselect", label_visibility="collapsed",
                placeholder="Choose files from workspace...")
            if selected_to_add:
                st.session_state.protected_files = list(set(protected) | set(selected_to_add))
                st.rerun()
            if protected:
                st.success(f"{len(protected)} file(s) protected")
                for pf in protected:
                    col_pf1, col_pf2 = st.columns([4, 1])
                    with col_pf1: st.code(pf, language=None)
                    with col_pf2:
                        if st.button("✕", key=f"remove_pf_{pf}"):
                            st.session_state.protected_files = [x for x in protected if x != pf]
                            st.rerun()

        # Ignore patterns sub-expander
        with st.expander("🚫 Ignore Patterns (.opencodeignore)", expanded=False):
            from supervisor.workspace.ignore_patterns import (
                IGNORE_FILE,
                write_ignore_file,
            )
            st.caption("Files matching these patterns will be excluded from context retrieval")
            ws_path = Path(st.session_state.workspace) if st.session_state.workspace else None
            if not ws_path or not ws_path.exists():
                st.warning("Set a valid workspace path to edit .opencodeignore")
            else:
                ignore_file_path = ws_path / IGNORE_FILE
                current_ignore_content = ""
                if ignore_file_path.exists():
                    try:
                        current_ignore_content = ignore_file_path.read_text(encoding="utf-8")
                    except Exception:
                        pass
                new_ignore_content = st.text_area(
                    "Ignore patterns", value=current_ignore_content, height=200,
                    key="ignore_patterns_editor",
                    placeholder=(
                        "# Patterns to ignore (one per line)\n# Examples:\n"
                        "# *.pyc\n# debug*\n# *test.py\n# build/\n# **/*.log\n"
                    ), label_visibility="collapsed")
                if new_ignore_content != current_ignore_content:
                    if st.button("Save Ignore Patterns", key="save_ignore_patterns"):
                        try:
                            write_ignore_file(ws_path, new_ignore_content)
                            st.success(f"Saved {IGNORE_FILE}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to save: {e}")
                if ignore_file_path.exists():
                    st.caption(f"Found existing {IGNORE_FILE} with {len(current_ignore_content.splitlines())} patterns")

                st.markdown("---")
                suggest_disabled = not (st.session_state.workspace and Path(st.session_state.workspace).exists())
                if st.button("Suggest and Apply Ignore Patterns", key="btn_suggest_ignore",
                             disabled=suggest_disabled):
                    try:
                        ws = Path(st.session_state.workspace)
                        all_entries = sorted([
                            str(p.relative_to(ws)).replace("\\", "/")
                            for p in ws.rglob("*")
                            if str(p.relative_to(ws)) != ".opencodeignore"
                        ])
                        file_list_str = "\n".join(all_entries)
                        truncation_note = ""
                        if estimate_tokens(file_list_str) > 100000:
                            all_entries = all_entries[:1000]
                            file_list_str = "\n".join(all_entries)
                            truncation_note = (
                                "Note: The file list was truncated to the first 1,000 entries "
                                "due to token limits."
                            )
                        client = OpenAI(api_key=st.session_state.openai_key,
                                        base_url=st.session_state.base_url or None)
                        model = st.session_state.supervisor_model or "gpt-4o"
                        system_msg = (
                            "Given a list of files and directories in a workspace, "
                            "generate a .opencodeignore file that ignores common build "
                            "artifacts, dependency directories, cache files, and other "
                            "files that should not be modified by an autonomous coding "
                            "agent. The patterns should be in gitignore format. Only "
                            "output the patterns, one per line. Do not include any explanations."
                        )
                        user_msg = (
                            f"The workspace contains the following files and directories:\n\n"
                            f"{file_list_str}"
                            + (f"\n\n{truncation_note}" if truncation_note else "")
                        )
                        response = client.chat.completions.create(
                            model=model,
                            messages=[{"role": "system", "content": system_msg},
                                      {"role": "user", "content": user_msg}],
                        )
                        generated_patterns = normalize_model_response(
                            response.choices[0].message.content,
                            "generated ignore patterns response",
                        )
                        st.text_area("Generated .opencodeignore patterns", value=generated_patterns,
                                     height=300, key="generated_ignore_patterns", disabled=True)
                        ignore_file_path.write_text(generated_patterns, encoding="utf-8")
                        st.toast("Ignore patterns generated and saved to .opencodeignore.")
                    except Exception as e:
                        st.error(f"Failed to generate ignore patterns: {e}")

    save_settings()
    _render_connectivity_tests()

    # Existing protocol.md banner
    workspace_path = Path(st.session_state.workspace) if st.session_state.workspace else None
    if workspace_path:
        def _on_reuse_protocol(text: str):
            st.session_state.protocol_md = text
            st.session_state.wizard_step = 1
            st.rerun()
        _render_existing_protocol_banner(
            workspace_path / "protocol.md", "protocol_md",
            on_reuse=_on_reuse_protocol)

    # ── Three-section form ─────────────────────────────────────────────── #
    st.markdown("### ✍️ Draft your protocol")

    for section_key, label, height, placeholder in [
        ("raw_input", "**INPUT** — what already exists / what the agent starts with", 120,
         "e.g. A Python repo is at ./src. The main entry point is main.py."),
        ("raw_target", "**TARGET** — concrete, testable deliverables", 140,
         "e.g.\n1. Build a FastAPI server in src/main.py with GET /health and POST /echo\n"
         "2. Add requirements.txt\n3. All tests in ./tests/ must pass"),
        ("raw_restrictions", "**RESTRICTIONS** — hard rules the agent must not break", 100,
         "e.g.\n- Don't touch files outside ./src\n- No system package installs\n- Keep code under 300 lines"),
    ]:
        with _card():
            st.markdown(label)
            st.text_area(section_key, key=section_key, height=height,
                         placeholder=placeholder, label_visibility="collapsed")

    if any(st.session_state.get(k, "").strip() for k in ("raw_input", "raw_target", "raw_restrictions")):
        with st.expander("📊 Protocol Quality Preview"):
            _render_protocol_quality(
                f"## INPUT\n\n{st.session_state.raw_input}\n\n"
                f"## TARGET\n\n{st.session_state.raw_target}\n\n"
                f"## RESTRICTIONS\n\n{st.session_state.raw_restrictions}\n",
            )

    if st.button("✨  Refine with AI", type="primary"):
        missing = [label for key, label in [
            ("openai_key", "OpenAI API Key"), ("workspace", "Workspace path"),
            ("raw_input", "INPUT section"), ("raw_target", "TARGET section"),
            ("raw_restrictions", "RESTRICTIONS section"),
        ] if not st.session_state.get(key, "").strip()]
        if missing:
            st.error(f"Please fill in: {', '.join(missing)}")
        else:
            apply_api_config()
            with st.spinner("Asking supervisor to refine your protocol…"):
                wizard = ProtocolWizard(model=st.session_state.supervisor_model)
                try:
                    refined_md, _ = wizard.refine(
                        st.session_state.raw_input,
                        st.session_state.raw_target,
                        st.session_state.raw_restrictions,
                    )
                    st.session_state.protocol_md = refined_md
                    st.session_state.wizard_step = 1
                    st.rerun()
                except Exception as exc:
                    st.error(f"Refinement failed: {exc}")

    if st.session_state.wizard_step == 1 and st.session_state.protocol_md:
        st.markdown("---")
        st.markdown("### 📄 Refined `protocol.md`")
        st.markdown("*Review and edit below, then accept.*")
        st.text_area("proto_edit", key="protocol_md", height=300, label_visibility="collapsed")
        with st.expander("📊 Protocol Quality Analysis"):
            _render_protocol_quality(st.session_state.protocol_md, detailed=True)
        col_a, col_b, _ = st.columns([1, 1, 3])
        with col_a:
            if st.button("✅  Accept & Save", type="primary"):
                _save_protocol()
                st.success("protocol.md saved to workspace.")
        with col_b:
            if st.button("🔄  Re-refine"):
                st.session_state.wizard_step = 0
                st.rerun()


def page_run() -> None:
    _page_run_impl(job_manager=job_manager, pill_map=_PILL_MAP)

def page_evolve() -> None:
    _page_evolve_impl(
        job_manager=job_manager,
        pill_map=_PILL_MAP,
        render_existing_protocol_banner=_render_existing_protocol_banner,
    )


# ═══════════════════════════════════════════════════════════════════════════ #
# Router                                                                      #
# ═══════════════════════════════════════════════════════════════════════════ #

if st.session_state.page == "report":
    st.session_state.page = "run"

_tests_ok = (
    (st.session_state.opencode_test_passed and st.session_state.supervisor_test_passed)
    or _jobs_running
)

_LOCKED_PAGES = {"run", "evolve"}
page = st.session_state.page

if page in _LOCKED_PAGES and not _tests_ok:
    _redirect_if_locked(
        page,
        f"🔒 {page.title()} is locked. Pass connectivity tests on the Protocol Wizard page first.",
    )
elif page == "wizard":
    page_wizard()
elif page == "run":
    page_run()
elif page == "evolve":
    page_evolve()
