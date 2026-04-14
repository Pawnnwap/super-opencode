"""app.py  —  opencode Supervisor UI
Run with:  streamlit run app.py
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import streamlit as st
from openai import OpenAI

from services.job_manager import JobManager
from services.settings import apply_api_config, load_settings, save_settings
from supervisor.analyzers.codebase_analyzer import snapshot_codebase
from supervisor.monitoring.session_tracker import SessionTracker
from supervisor.protocols.meta_protocol_builder import (
    MetaProtocolBuilder,
    write_meta_protocol,
)
from supervisor.protocols.protocol import parse_protocol_text
from supervisor.protocols.protocol_analyzer import ProtocolAnalyzer, Severity
from supervisor.protocols.protocol_wizard import ProtocolWizard
from supervisor.utils.config import SupervisorConfig
from supervisor.utils.text_utils import sanitize_event_message

_UPGRADE_SETTINGS_FILE = Path.home() / ".opencode_supervisor_settings.json"


# ── Upgrade helpers ──────────────────────────────────────────────────────── #

def _should_skip_upgrade() -> bool:
    if os.environ.get("OPENCODE_SKIP_UPGRADE") == "1":
        return True
    try:
        if _UPGRADE_SETTINGS_FILE.exists():
            cfg = json.loads(_UPGRADE_SETTINGS_FILE.read_text(encoding="utf-8"))
            if cfg.get("skip_upgrade"):
                return True
    except Exception:
        pass
    return False


def _auto_upgrade_opencode():
    if sys.platform != "win32":
        print("[opencode-upgrade] Skipping upgrade: not on Windows", file=sys.stderr)
        return

    if _should_skip_upgrade():
        print("[opencode-upgrade] Skipping upgrade: disabled via config/env var", file=sys.stderr)
        return

    try:
        home_dir = str(Path.home())
        print("[opencode-upgrade] Running: choco upgrade opencode -y (with admin elevation)", file=sys.stderr)
        proc = subprocess.Popen(
            [
                "powershell", "-Command",
                "Start-Process choco -ArgumentList 'upgrade','opencode','-y' -Verb RunAs -Wait",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=home_dir,
        )
        stdout, stderr = proc.communicate(timeout=120)
        if stdout:
            print(f"[opencode-upgrade] stdout: {stdout.strip()}", file=sys.stderr)
        if stderr:
            print(f"[opencode-upgrade] stderr: {stderr.strip()}", file=sys.stderr)

        code_msg = "successfully" if proc.returncode == 0 else f"with code {proc.returncode}. Continuing startup."
        print(f"[opencode-upgrade] Upgrade completed {code_msg}.", file=sys.stderr)

    except subprocess.TimeoutExpired:
        print("[opencode-upgrade] Upgrade timed out after 120 seconds. Continuing startup.", file=sys.stderr)
    except FileNotFoundError:
        print("[opencode-upgrade] 'powershell' command not found. Continuing startup.", file=sys.stderr)
    except Exception as e:
        print(f"[opencode-upgrade] Unexpected error: {e}. Continuing startup.", file=sys.stderr)


def _auto_upgrade_dcp():
    if sys.platform != "win32":
        print("[dcp-upgrade] Skipping upgrade: not on Windows", file=sys.stderr)
        return

    if _should_skip_upgrade():
        print("[dcp-upgrade] Skipping upgrade: disabled via config/env var", file=sys.stderr)
        return

    try:
        print("[dcp-upgrade] Spawning background upgrade window...", file=sys.stderr)
        cmd_string = 'start "" cmd /c "opencode plugin @tarquinen/opencode-dcp@latest --global"'
        home_dir = os.path.expanduser("~")

        subprocess.Popen(
            cmd_string,
            shell=True,
            cwd=home_dir,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

        print("[dcp-upgrade] Upgrade window launched. It will close on completion.", file=sys.stderr)

    except Exception as e:
        print(f"[dcp-upgrade] Unexpected error spawning window: {e}", file=sys.stderr)


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
    """Return logs list from a job status dict, guarding against null."""
    return status.get("logs") or []


def _redirect_if_locked(page: str, warning: str) -> None:
    """Redirect to wizard with a warning if connectivity tests haven't passed."""
    st.session_state.page = "wizard"
    st.session_state["_redirect_warning"] = warning
    st.rerun()


# ── opencode config helpers ──────────────────────────────────────────────── #

def _find_opencode_config_dir() -> Path:
    """Always uses ~/.config/opencode regardless of platform."""
    config_dir = Path.home() / ".config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def _atomic_write_json(path: Path, content: dict) -> None:
    """Atomically write JSON to a file with proper error handling."""
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = path.with_suffix(".tmp")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(content, tmp, indent=2)
        tmp_path.replace(path)  # Atomic on POSIX
    except (PermissionError, OSError):
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _get_opencode_config_file(config_dir: Path) -> Path:
    """Manages the config file, ensuring hashline MCP and permissions are set."""
    # Standardize on ONE filename to avoid confusion
    target_file = config_dir / "opencode.json"

    # Optional: migrate old config.json if it exists
    old_file = config_dir / "config.json"
    if old_file.exists() and not target_file.exists():
        try:
            old_file.rename(target_file)
        except Exception:
            pass  # Fallback: just use config.json

    target_file.parent.mkdir(parents=True, exist_ok=True)

    # Read existing config
    try:
        content = json.loads(target_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        content = {"$schema": "https://opencode.ai/config.json", "provider": {}}
    except json.JSONDecodeError as e:
        st.warning(f"Config JSON invalid, resetting: {e}")
        content = {"$schema": "https://opencode.ai/config.json", "provider": {}}
    except PermissionError:
        raise PermissionError(f"Cannot read config: {target_file}")

    # Validate root is a dict
    if not isinstance(content, dict):
        raise TypeError(f"Config must be a JSON object, got {type(content).__name__}")

    dirty = False

    # Ensure MCP section exists and is a dict
    if "mcp" not in content or not isinstance(content["mcp"], dict):
        content["mcp"] = {}
        dirty = True

    # Configure Hashline MCP
    hashline_path = str(Path(__file__).parent.resolve() / "mcp_server" / "hashline.py").replace("\\", "/")
    mcp_hashline_config = {
        "type": "local",
        "command": ["python", hashline_path],
        "enabled": True,
        "environment": {},
    }

    if content["mcp"].get("hashline") != mcp_hashline_config:
        content["mcp"]["hashline"] = mcp_hashline_config
        dirty = True

    # Set permissions
    desired_permissions = {"read": "deny", "edit": "deny"}
    if content.get("permission") != desired_permissions:
        content["permission"] = desired_permissions
        dirty = True

    # Atomic write if changed
    if dirty:
        _atomic_write_json(target_file, content)
        st.info(f"✓ Config updated: {target_file.name}")  # Optional UI feedback

    return target_file


def _add_custom_provider_to_config(
    config_file: Path,
    service_name: str,
    base_url: str,
    api_key: str,
    model_names: list[str],
) -> None:
    """Adds a custom provider to the specified config file."""
    if not service_name.strip():
        raise ValueError("service_name cannot be empty")
    if not base_url:
        raise ValueError("base_url cannot be empty")

    # Read
    try:
        content = json.loads(config_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        content = {"$schema": "https://opencode.ai/config.json", "provider": {}}
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}") from e
    except PermissionError:
        raise PermissionError(f"Cannot read: {config_file}")

    if not isinstance(content, dict):
        raise TypeError(f"Config root must be object, got {type(content).__name__}")

    content.setdefault("provider", {})
    if not isinstance(content["provider"], dict):
        raise TypeError("'provider' must be an object")

    valid_models = {name.strip(): {} for name in model_names if name.strip()}

    content["provider"][service_name] = {
        "npm": "@ai-sdk/openai-compatible",
        "options": {"baseURL": base_url, "apiKey": api_key},
        "models": valid_models,
    }

    # Atomic write using shared helper
    _atomic_write_json(config_file, content)


def _fetch_opencode_models(exe: str = "opencode") -> list[str]:
    """Fetches available models. Runs from Home Dir to avoid EPERM on project files.
    """
    home_dir = str(Path.home())
    try:
        proc = subprocess.run(
            [exe, "models"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=home_dir,  # Run from home to avoid local file locks
            shell=(sys.platform == "win32"),
        )

        if proc.returncode != 0:
            # If opencode models fails, it often prints the error to stdout or stderr
            error_output = proc.stderr.strip() or proc.stdout.strip()
            print(f"[opencode-models] Error: {error_output}", file=sys.stderr)
            return []

        raw_lines = proc.stdout.strip().splitlines()
        models = []

        # Filter headers, dividers, and empty lines
        for line in raw_lines:
            clean = line.strip()
            if not clean or any(clean.startswith(h) for h in ("-", "ID", "NAME", "PROMPT", "Error")):
                continue

            # Extract just the first word (Model ID) if output is a table
            model_id = clean.split()[0]
            models.append(model_id)

        return models

    except subprocess.TimeoutExpired:
        print("[opencode-models] Request timed out after 15s.", file=sys.stderr)
    except FileNotFoundError:
        print(f"[opencode-models] Executable '{exe}' not found in PATH.", file=sys.stderr)
    except Exception as e:
        print(f"[opencode-models] Unexpected error: {e}", file=sys.stderr)

    return []

# ── SupervisorConfig factory ─────────────────────────────────────────────── #


def _build_supervisor_config(protocol_path: Path, workspace: Path, **overrides) -> SupervisorConfig:
    """Build a SupervisorConfig from session state, with optional overrides."""
    defaults = dict(
        protocol_path=protocol_path,
        workspace=workspace,
        max_retries=int(st.session_state.max_retries),
        context_threshold=st.session_state.context_threshold / 100.0,
        opencode_model=st.session_state.opencode_model or None,
        opencode_model_backup=st.session_state.opencode_model_backup or None,
        opencode_executable=st.session_state.opencode_executable,
        supervisor_model=st.session_state.supervisor_model or "gpt-4o",
        supervisor_model_backup=st.session_state.supervisor_model_backup or None,
        timeout=int(st.session_state.timeout) * 60,
        protected_files=tuple(st.session_state.get("protected_files", [])),
        max_tokens=int(st.session_state.max_tokens),
        enable_python_scanner=bool(st.session_state.enable_python_scanner),
    )
    defaults.update(overrides)
    return SupervisorConfig(**defaults)


# ── Workspace isolation helpers ──────────────────────────────────────────── #


def _get_all_run_jobs() -> list[dict]:
    """Get all run jobs sorted by most recent."""
    jobs = []
    for jid in job_manager.store.list_jobs():
        status = job_manager.get_job_status(jid)
        if status and status.get("type") == "run":
            jobs.append({"id": jid, "status": status})
    jobs.sort(key=lambda j: j["status"].get("updated_at", 0), reverse=True)
    return jobs

# ── Startup initialisation ───────────────────────────────────────────────── #


if not st.session_state.get("_mcp_config_done"):
    _mcp_dir = _find_opencode_config_dir()
    if _mcp_dir:
        _get_opencode_config_file(_mcp_dir)
    st.session_state["_mcp_config_done"] = True

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
    return _PILL_MAP.get(state, f'<span class="pill pill-idle">{state}</span>')


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

def _run_with_timeout(fn, seconds: int = 30):
    import threading
    result, error = [], []

    def worker():
        try:
            result.append(fn())
        except Exception as e:
            error.append(e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=seconds)
    if t.is_alive():
        raise TimeoutError(f"Timed out after {seconds}s")
    if error:
        raise error[0]
    return result[0]


def test_opencode() -> tuple[bool, str]:
    from supervisor.runners.opencode_runner import OpencodeRunner, find_opencode

    workspace = Path(os.environ.get("TEMP", os.environ.get("TMPDIR", "/tmp"))) / "opencode_test_dummy"
    workspace.mkdir(exist_ok=True)
    try:
        exe = find_opencode(st.session_state.opencode_executable or "")
    except FileNotFoundError as e:
        return False, str(e)

    runner = OpencodeRunner(
        workspace=workspace, opencode_model=st.session_state.opencode_model,
        opencode_executable=exe, opencode_model_backup=st.session_state.opencode_model_backup,
        timeout=30,
    )

    def _inner():
        for _ in runner.start("hi"):
            pass
        output, timed_out = runner.read_output(timeout=25)
        if timed_out:
            return False, "opencode timed out reading output."
        if runner._last_result and runner._last_result.ok:
            return True, "opencode responded successfully."
        diag = runner.last_diagnostic() if runner._last_result else "(no result)"
        return False, f"opencode returned an error.\n{diag}"

    try:
        return _run_with_timeout(_inner, seconds=30)
    except TimeoutError:
        return False, "opencode test timed out."
    except Exception as exc:
        return False, f"opencode test failed: {exc}"
    finally:
        try:
            runner.stop()
        except Exception:
            pass


def test_supervisor() -> tuple[bool, str]:
    if not st.session_state.openai_key:
        return False, "API key is not set."
    model = st.session_state.supervisor_model or "gpt-4o"
    client = OpenAI(api_key=st.session_state.openai_key,
                    base_url=st.session_state.base_url or None, timeout=25.0)

    def _inner():
        resp = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "hi"}])
        text = resp.choices[0].message.content or ""
        if text.strip():
            return True, f"Supervisor responded: {text.strip()[:120]}"
        return False, "Supervisor returned an empty response."

    return _run_test_with_timeout(_inner, "Supervisor test")


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

def _render_quality_metrics(analysis) -> None:
    """Render the four quality metric columns (shared between raw/refined views)."""
    col1, col2, col3, col4 = st.columns(4)
    with col1: st.metric("Overall", f"{analysis.overall_score:.0%}")
    with col2: st.metric("INPUT", f"{analysis.input_score.overall:.0%}")
    with col3: st.metric("TARGET", f"{analysis.target_score.overall:.0%}")
    with col4: st.metric("RESTRICTIONS", f"{analysis.restrictions_score.overall:.0%}")


def _render_protocol_quality(text: str, detailed: bool = False) -> None:
    """Render protocol quality metrics and issues.

    Parameters
    ----------
    text:
        Protocol markdown to analyze.
    detailed:
        When True, show per-severity breakdowns with suggestions (used for
        the refined protocol view).  When False, show a compact issue list
        (used for the draft quality preview).

    """
    analyzer = ProtocolAnalyzer()
    try:
        analysis = analyzer.analyze_text(text)
    except Exception as e:
        st.caption("Complete all three sections to see quality scores." if not detailed
                   else f"Cannot analyze protocol: {e}")
        return

    _render_quality_metrics(analysis)

    if detailed:
        rating_colors = {"excellent": "🟢", "good": "🟡", "fair": "🟠", "poor": "🔴"}
        color = rating_colors.get(analysis.quality_rating, "⚪")
        st.caption(f"{color} Quality: {analysis.quality_rating}")

    if analysis.issues:
        if not detailed:
            st.caption(f"Found {len(analysis.issues)} issue(s)")
            for issue in analysis.issues[:5]:
                icon = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}[issue.severity.value]
                st.caption(f"{icon} [{issue.section}] {issue.message}")
        else:
            errors = [i for i in analysis.issues if i.severity == Severity.ERROR]
            warnings = [i for i in analysis.issues if i.severity == Severity.WARNING]
            infos = [i for i in analysis.issues if i.severity == Severity.INFO]

            if errors:
                st.error(f"{len(errors)} error(s) found")
                for issue in errors:
                    st.caption(f"❌ [{issue.section}] {issue.message}")
                    if issue.suggestion:
                        st.caption(f"   → {issue.suggestion}")
            if warnings:
                st.warning(f"{len(warnings)} warning(s)")
                for issue in warnings:
                    st.caption(f"⚠️ [{issue.section}] {issue.message}")
            if infos:
                with st.expander(f"{len(infos)} suggestion(s)"):
                    for issue in infos:
                        st.caption(f"ℹ️ [{issue.section}] {issue.message}")
                        if issue.suggestion:
                            st.caption(f"   → {issue.suggestion}")


# ── Existing-protocol reuse banner ──────────────────────────────────────── #

def _render_existing_protocol_banner(
    proto_path: Path,
    state_key: str,
    reuse_label: str = "♻️  Use existing protocol.md",
    on_reuse=None,
) -> bool:
    """If ``proto_path`` exists and ``st.session_state[state_key]`` is falsy,
    show a reuse / ignore banner.  Returns True if the banner was shown.
    ``on_reuse`` is an optional callable invoked when the user clicks Reuse.
    """
    if not (proto_path.exists() and not st.session_state.get(state_key)):
        return False

    existing_text = proto_path.read_text(encoding="utf-8")
    fname = proto_path.name
    st.info(f"📄 An existing `{fname}` was found.")
    col_reuse, col_ignore, _ = st.columns([1, 1, 3])
    with col_reuse:
        if st.button(reuse_label, type="primary", key=f"btn_reuse_{state_key}"):
            if on_reuse:
                on_reuse(existing_text)
            else:
                st.session_state[state_key] = existing_text
                st.rerun()
    with col_ignore:
        st.button("✏️  Write new one", key=f"btn_ignore_{state_key}")
    with st.expander(f"Preview existing {fname}"):
        st.code(existing_text[:1500], language="markdown")
    st.markdown("---")
    return True


# ═══════════════════════════════════════════════════════════════════════════ #
# PAGE 1 — Protocol Wizard                                                    #
# ═══════════════════════════════════════════════════════════════════════════ #

def _save_protocol() -> None:
    workspace = Path(st.session_state.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    proto_path = workspace / "protocol.md"
    proto_path.write_text(st.session_state.protocol_md, encoding="utf-8")
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
                        if SessionTracker.estimate_tokens(file_list_str) > 100000:
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
                        generated_patterns = response.choices[0].message.content.strip()
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


# ═══════════════════════════════════════════════════════════════════════════ #
# Shared log rendering                                                        #
# ═══════════════════════════════════════════════════════════════════════════ #

def _esc(t) -> str:
    """HTML-escape, safely coercing None/non-str."""
    if t is None:
        return ""
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_BLOCK_LABELS = {
    "opencode_prompt": "▶ PROMPT → opencode",
    "opencode_output": "◀ OUTPUT ← opencode",
    "supervisor_response": "🧠 SUPERVISOR",
    "supervisor_read_files": "📂 SUPERVISOR READ FILES",
}


def _render_events(
    events: list[dict],
    empty_msg: str,
    skip: set | None = None,
    show_verbose: bool = True,
    page_key: str = "default",
) -> None:
    events = events or []
    skip = skip or set()
    verbose = st.session_state.get("verbose_log", True)

    if show_verbose:
        st.session_state.verbose_log = st.toggle(
            "Verbose log", value=verbose, key=f"vtoggle_{page_key}")
        verbose = st.session_state.verbose_log

    if not events:
        st.markdown(
            f'<div class="log-box"><span class="log-info">{_esc(empty_msg)}</span></div>',
            unsafe_allow_html=True)
        return

    lines_html: list[str] = []

    for ev in events[-600:]:
        if not isinstance(ev, dict):
            continue
        lvl = ev.get("level") or "info"
        if lvl in skip:
            continue
        msg = sanitize_event_message(ev.get("msg") or "")

        if lvl in _BLOCK_LABELS:
            hdr_label = _BLOCK_LABELS[lvl]
            if not verbose:
                preview = _esc(str(msg)[:120].replace("\n", " "))
                lines_html.append(
                    f'<span class="log-block-hdr">{hdr_label}</span>'
                    f'<span class="log-info" style="opacity:0.6"> {preview}…</span>\n')
            else:
                lines_html.append(
                    f'<span class="log-rule">{"─" * 60}</span>\n'
                    f'<span class="log-block-hdr">{hdr_label}</span>\n'
                    f'<span class="log-{_esc(lvl)}">{_esc(msg)}</span>\n')
        else:
            lines_html.append(f'<span class="log-{_esc(lvl)}">{_esc(msg)}</span>\n')

    st.markdown(f'<div class="log-box">{"".join(lines_html)}</div>', unsafe_allow_html=True)


def _render_token_usage_bar(logs: list[dict], max_tokens: int) -> None:
    import re
    latest_current, latest_fraction, found = 0, 0.0, False
    for ev in logs:
        if not isinstance(ev, dict):
            continue
        msg = ev.get("msg") or ""
        if "context usage" in msg.lower():
            match = re.search(r"(\d[\d,]*)\s*/\s*(\d[\d,]*)\s*tokens", msg)
            if match:
                current = int(match.group(1).replace(",", ""))
                max_t = int(match.group(2).replace(",", ""))
                fraction = current / max_t if max_t > 0 else 0
                if fraction >= latest_fraction:
                    latest_fraction, latest_current, found = fraction, current, True
    if found:
        color = "🔴" if latest_fraction > 0.9 else "🟡" if latest_fraction > 0.7 else "🟢"
        st.progress(min(latest_fraction, 1.0),
                    text=f"{color} {latest_current:,} / {max_tokens:,} tokens")


def _render_step_progress(logs: list[dict], run_state: str, is_evolution: bool = False) -> None:
    logs = logs or []
    step_events = [e for e in logs if isinstance(e, dict) and e.get("level") in ("step", "phase_transition")]
    progress_events = [e for e in logs if isinstance(e, dict) and e.get("level") == "step_progress"]
    heartbeat_events = [e for e in logs if isinstance(e, dict) and e.get("level") == "heartbeat"]
    process_label = "Evolution process active" if is_evolution else "Background process active"

    if run_state == "RUNNING":
        c1, c2, c3 = st.columns([3, 1, 1])
        with c1: st.markdown(f"🟢 **{process_label}**")
        with c2: st.caption(f"💓 {len(heartbeat_events)} heartbeat(s)")
        with c3: st.caption(f"🧭 {len(step_events)} step(s)")
        if progress_events:
            with st.expander("📊 Progress"):
                st.caption(progress_events[-1].get("msg") or "")
    elif progress_events:
        last_progress = progress_events[-1]
        c1, c2, c3 = st.columns([3, 1, 1])
        with c1: st.caption(f"📊 {last_progress.get('msg') or ''}")
        with c2: st.caption(f"🧭 {len(step_events)} step(s)")
        with c3:
            if heartbeat_events: st.caption("🟢 active")

        progress_val = 0.0
        if "percentage" in last_progress and last_progress["percentage"] is not None:
            try:
                progress_val = float(last_progress["percentage"])
            except (TypeError, ValueError):
                pass
        else:
            for p in (last_progress.get("msg") or "").split():
                candidate = p.replace("%", "").replace(".", "")
                if candidate.isdigit():
                    try:
                        progress_val = float(p.replace("%", ""))
                        break
                    except ValueError:
                        pass

        if progress_val > 0:
            c1, _ = st.columns([4, 1])
            with c1:
                st.progress(progress_val / 100.0, text=f"{progress_val:.0f}% complete")

        if step_events:
            with st.expander("📍 Step History", expanded=False):
                for ev in step_events[-5:]:
                    if ev.get("level") == "step":
                        st.caption(f"• {(ev.get('msg') or '')[:80]}")
                    elif ev.get("level") == "phase_transition":
                        st.caption(f"⚡ {ev.get('msg') or ''}")


# ── Shared job status screen ─────────────────────────────────────────────── #

class JobStatusScreen:
    """Renders the status screen for a running or completed job.
    Encapsulates the duplicated pattern between run and evo pages.
    """

    def __init__(
        self,
        job_id: str,
        title: str,
        page_key: str,
        query_param: str,
        report_filename_prefix: str,
        running_message: str,
        is_evolution: bool = False,
    ):
        self.job_id = job_id
        self.title = title
        self.page_key = page_key
        self.query_param = query_param
        self.report_filename_prefix = report_filename_prefix
        self.running_message = running_message
        self.is_evolution = is_evolution

    def render(self) -> None:
        status = job_manager.get_job_status(self.job_id)
        if not status:
            st.error(f"Job {self.job_id} not found.")
            if st.button("Back to Setup"):
                del st.query_params[self.query_param]
                st.rerun()
            return

        state = status["state"]
        logs = _safe_logs(status)

        # Header row
        col_h1, col_h2, col_h3 = st.columns([3, 1, 1])
        with col_h1:
            st.markdown(f"### {self.title}: `{self.job_id}`")
        with col_h2:
            if state == "RUNNING":
                if st.button("⏹ Stop", use_container_width=True, key=f"stop_{self.page_key}"):
                    job_manager.cancel_job(self.job_id)
                    st.rerun()
            elif st.button("🗑 Clear", use_container_width=True, key=f"clear_{self.page_key}"):
                del st.query_params[self.query_param]
                st.rerun()
        with col_h3:
            if st.button("🔄 Refresh", use_container_width=True, key=f"refresh_{self.page_key}"):
                st.rerun()

        col_main, col_side = st.columns([2, 1])
        with col_main:
            _render_step_progress(logs, state, is_evolution=self.is_evolution)
            st.markdown(f"#### 🖥️ {'Evolution' if self.is_evolution else 'Live'} Log")
            _render_events(logs, "— waiting for logs —", show_verbose=True, page_key=self.page_key)

        with col_side:
            st.markdown("#### ℹ️ Details")
            st.markdown(f"**State:** {state}")
            st.markdown(
                f"**Started:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(status.get('updated_at', 0)))}")
            sup_model = st.session_state.get("supervisor_model", "") or "(not set)"
            oc_model = st.session_state.get("opencode_model", "") or "(not set)"
            st.markdown(f"**Supervisor model:** `{sup_model}`")
            st.markdown(f"**Opencode model:** `{oc_model}`")
            _render_token_usage_bar(logs, int(st.session_state.max_tokens))

            if status.get("report"):
                report_title = "📊 Evolution Report" if self.is_evolution else "📊 Report"
                st.markdown(f"#### {report_title}")
                with st.expander("View Report", expanded=True):
                    st.markdown(status["report"])
                    st.download_button(
                        "⬇ Download", data=status["report"],
                        file_name=f"{self.report_filename_prefix}_{self.job_id}.md",
                        mime="text/markdown")

        if state == "RUNNING":
            st.info(f"{'🧬' if self.is_evolution else '🏃'} {self.running_message}")
            try:
                time.sleep(2)
            finally:
                st.rerun()


def _show_task_form(workspace: Path | None) -> None:
    """Render the new-task form with workspace isolation."""
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

    with st.expander("Start New Task", expanded=st.session_state.get("_show_task_form", False)):
        st.markdown(f"**Primary workspace:** `{workspace}`")
        st.caption("Each task gets its own isolated workspace directory.")
        col1, col2 = st.columns([2, 1])
        with col1:
            plan_rounds = st.number_input(
                "Plan mode rounds", min_value=0, max_value=10,
                value=int(st.session_state.plan_mode_rounds),
                key="task_plan_mode_rounds",
                help="Number of planning rounds before execution")
            enable_scanner = st.toggle(
                "Enable Python scanner", value=bool(st.session_state.enable_python_scanner),
                key="task_enable_python_scanner",
                help="Run the Python vulnerability scanner before execution")
        with col2:
            if st.button("Start Task", type="primary", use_container_width=True, key="btn_start_task"):
                st.session_state.plan_mode_rounds = plan_rounds
                st.session_state.enable_python_scanner = enable_scanner
                st.session_state._show_task_form = False
                save_settings()
                apply_api_config()
                config = _build_supervisor_config(proto_path, workspace, plan_mode_rounds=int(plan_rounds))
                try:
                    job_id = job_manager.enqueue_job("run", config)
                    st.toast(f"Task started: `{job_id}` in `{workspace.name}`")
                    st.rerun()
                except ValueError as e:
                    st.toast(f"❌ {e}")


def _render_job_card(job: dict, is_running: bool) -> None:
    job_id = job["id"]
    status = job["status"]
    state = status.get("state", "UNKNOWN")
    config = status.get("config", {})
    workspace = config.get("workspace", "")
    col1, col2, col3 = st.columns([3, 2, 1])
    with col1:
        pill = _render_status_pill(state)
        st.markdown(f"`{job_id}` {pill} ", unsafe_allow_html=True)
        if workspace:
            ws_name = Path(workspace).name
            st.caption(f"Workspace: `{ws_name}`")
    with col2:
        if state == "RUNNING":
            if st.button("Stop", key=f"stop_card_{job_id}"):
                job_manager.cancel_job(job_id)
                st.rerun()
        elif state in ("SUCCESS", "FAILED", "CANCELLED"):
            if st.button("Delete", key=f"delete_card_{job_id}"):
                job_manager.store.delete_job(job_id)
                st.rerun()
    with col3:
        if st.button("View", key=f"view_card_{job_id}"):
            st.query_params["run_job_id"] = job_id
            st.rerun()


# ═══════════════════════════════════════════════════════════════════════════ #
# PAGE 2 — Live Run                                                           #
# ═══════════════════════════════════════════════════════════════════════════ #


def page_run() -> None:
    st.markdown("# Live Run")
    job_id = st.query_params.get("run_job_id")
    if job_id:
        if st.button("Back to Task List", key="btn_back_to_list"):
            st.query_params.pop("run_job_id", None)
            st.rerun()
        JobStatusScreen(
            job_id=job_id, title="Task", page_key="run_view",
            query_param="run_job_id", report_filename_prefix="report",
            running_message="Task running. You can safely close this tab or refresh.",
        ).render()
        return

    workspace = Path(st.session_state.workspace) if st.session_state.workspace else None

    st.markdown("Manage multiple concurrent tasks with isolated workspaces.")
    col_nav, col_new = st.columns([3, 1])
    with col_new:
        if st.button("New Task", type="primary", key="btn_new_task"):
            st.session_state._show_task_form = True
            st.rerun()
    st.markdown("---")
    all_run_jobs = _get_all_run_jobs()
    if not all_run_jobs:
        st.info("No run tasks yet. Click **New Task** to start one.")
    else:
        running_jobs = [j for j in all_run_jobs if j["status"].get("state") == "RUNNING"]
        completed_jobs = [j for j in all_run_jobs if j["status"].get("state") != "RUNNING"]
        if running_jobs:
            st.markdown("### Active Tasks")
            for job in running_jobs:
                _render_job_card(job, is_running=True)
        if completed_jobs:
            with st.expander("### Completed Tasks", expanded=False):
                for job in completed_jobs[:10]:
                    _render_job_card(job, is_running=False)
        st.markdown("---")

    _show_task_form(workspace)


# ═══════════════════════════════════════════════════════════════════════════ #
# PAGE 3 — Self-Evolution                                                     #
# ═══════════════════════════════════════════════════════════════════════════ #

def page_evolve() -> None:
    st.markdown("# Self-Evolution")
    job_id = st.query_params.get("evo_job_id")
    if not job_id:
        for jid in job_manager.store.list_jobs():
            s = job_manager.get_job_status(jid)
            if s and s.get("type") == "evolve" and s.get("state") == "RUNNING":
                st.query_params["evo_job_id"] = jid
                st.rerun()
                return
    if job_id:
        JobStatusScreen(
            job_id=job_id, title="Evolution Job", page_key="evo",
            query_param="evo_job_id", report_filename_prefix="evo_report",
            running_message="Evolution in progress...", is_evolution=True,
        ).render()
    else:
        _show_evo_setup_screen()


def _show_evo_setup_screen() -> None:
    st.markdown(
        "Point the supervisor + opencode at **this codebase itself**. "
        "Describe what you want improved or debugged — the system will "
        "auto-generate a `meta_protocol.md` from the live source tree, "
        "then run the full supervisor loop.")

    if not st.session_state.openai_key:
        st.warning("Enter your OpenAI API key in the Protocol Wizard config panel first.")
        return

    repo_root = Path(__file__).parent.resolve()
    st.info(f"**Repo root (workspace):** `{repo_root}`")

    # Existing meta_protocol.md banner
    def _on_reuse_meta(text: str):
        try:
            proto = parse_protocol_text(text)
            st.session_state.evo_goal = proto.target_section
            st.session_state.evo_extra_restrictions = proto.restrictions_section
            st.rerun()
        except Exception as e:
            st.error(f"Failed to parse meta_protocol.md: {e}")

    _render_existing_protocol_banner(
        repo_root / "meta_protocol.md", "evo_meta_protocol_md",
        reuse_label="♻️  Use existing meta_protocol.md",
        on_reuse=_on_reuse_meta)

    if st.session_state.evo_wizard_step == 0:
        st.markdown("### 🎯 What do you want to evolve?")
        for section_key, label, height in [
            ("evo_goal", "**Evolution goal**", 130),
            ("evo_extra_restrictions", "**Extra restrictions**", 80),
        ]:
            with _card():
                st.markdown(label)
                st.text_area(section_key, key=section_key, height=height, label_visibility="collapsed")

        col_gen, col_snap, _ = st.columns([1, 1, 3])
        with col_gen:
            if st.button("🧠 Generate meta_protocol.md", type="primary"):
                _generate_meta_protocol(repo_root)
        with col_snap:
            if st.button("🔍 Preview snapshot"):
                with st.spinner("Scanning..."):
                    snap = snapshot_codebase(repo_root)
                    st.code(snap.tree())

    elif st.session_state.evo_wizard_step == 1:
        st.markdown("### 📄 Generated `meta_protocol.md`")
        st.text_area("evo_proto_edit", key="evo_meta_protocol_md",
                     height=340, label_visibility="collapsed")
        col_a, col_b, _ = st.columns([1, 1, 2])
        with col_a:
            if st.button("🚀 Launch Evolution", type="primary"):
                save_settings()
                apply_api_config()
                proto_path = write_meta_protocol(st.session_state.evo_meta_protocol_md, repo_root)
                config = _build_supervisor_config(proto_path, repo_root)
                try:
                    job_id = job_manager.enqueue_job("evolve", config)
                    st.query_params["evo_job_id"] = job_id
                    st.rerun()
                except ValueError as e:
                    st.toast(f"❌ {e}")
        with col_b:
            if st.button("🔄 Regenerate"):
                st.session_state.evo_wizard_step = 0
                st.rerun()


def _generate_meta_protocol(repo_root: Path) -> None:
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
