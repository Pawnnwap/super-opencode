"""
app.py  —  opencode Supervisor UI
Run with:  streamlit run app.py
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import streamlit as st
from openai import OpenAI

from services.job_manager import JobManager
from services.settings import load_settings, save_settings, apply_api_config
from supervisor.analyzers.codebase_analyzer import snapshot_codebase
from supervisor.core.loop import SupervisorLoop
from supervisor.core.self_evolution_loop import SelfEvolutionLoop
from supervisor.monitoring.token_estimator import estimate_tokens
from supervisor.protocols.meta_protocol_builder import (
    MetaProtocolBuilder,
    write_meta_protocol,
)
from supervisor.protocols.protocol_analyzer import ProtocolAnalyzer, Severity
from supervisor.protocols.protocol_wizard import ProtocolWizard
from supervisor.utils.config import SupervisorConfig

_UPGRADE_SETTINGS_FILE = Path.home() / ".opencode_supervisor_settings.json"


def _should_skip_upgrade():
    """Check env var and config file to decide whether to skip upgrade."""
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
    """Run choco upgrade opencode -y on Windows with admin privileges."""
    if sys.platform != "win32":
        print("[opencode-upgrade] Skipping upgrade: not on Windows", file=sys.stderr)
        return
    if _should_skip_upgrade():
        print(
            "[opencode-upgrade] Skipping upgrade: disabled via config/env var",
            file=sys.stderr,
        )
        return
    try:
        print(
            "[opencode-upgrade] Running: choco upgrade opencode -y (with admin elevation)",
            file=sys.stderr,
        )
        proc = subprocess.Popen(
            [
                "powershell",
                "-Command",
                "Start-Process choco -ArgumentList 'upgrade','opencode','-y' -Verb RunAs -Wait",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(timeout=120)
        if stdout:
            print(f"[opencode-upgrade] stdout: {stdout.strip()}", file=sys.stderr)
        if stderr:
            print(f"[opencode-upgrade] stderr: {stderr.strip()}", file=sys.stderr)
        if proc.returncode == 0:
            print("[opencode-upgrade] Upgrade completed successfully.", file=sys.stderr)
        else:
            print(
                f"[opencode-upgrade] Upgrade exited with code {proc.returncode}. Continuing startup.",
                file=sys.stderr,
            )
    except subprocess.TimeoutExpired:
        print(
            "[opencode-upgrade] Upgrade timed out after 120 seconds. Continuing startup.",
            file=sys.stderr,
        )
    except FileNotFoundError:
        print(
            "[opencode-upgrade] 'powershell' command not found. Continuing startup.",
            file=sys.stderr,
        )
    except Exception as e:
        print(
            f"[opencode-upgrade] Unexpected error: {e}. Continuing startup.",
            file=sys.stderr,
        )


# ── End auto-upgrade block ──────────────────────────────────────────────── #


# ── supervisor package imports (all at top level — never lazy) ──────────── #

# ── Job Manager instance ────────────────────────────────────────────────── #
# This instance handles long-running jobs in background threads and
# persists status to disk to handle browser refreshes/disconnections.
job_manager = JobManager(".job_store")


# ── page config ──────────────────────────────────────────────────────────── #
st.set_page_config(
    page_title="opencode Supervisor",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Run upgrade exactly once per Streamlit session ───────────────────────── #
# st.session_state persists across reruns (page switches, widget interactions)
# but is reset when the browser tab is closed or the server restarts.
# Using a flag here prevents the upgrade from firing on every script rerun.
if not st.session_state.get("_upgrade_done"):
    _auto_upgrade_opencode()
    st.session_state["_upgrade_done"] = True

# ── custom CSS constants ─────────────────────────────────────────────────── #
CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600&family=Syne:wght@400;700;800&display=swap');

html, body, [class*="css"] {
    font-family: 'Syne', sans-serif;
    background-color: #0d0f14;
    color: #c9d1d9;
}

/* sidebar */
section[data-testid="stSidebar"] {
    background: #0a0c10;
    border-right: 1px solid #21262d;
}

/* headings */
h1 { font-family: 'Syne', sans-serif; font-weight: 800; color: #58a6ff; letter-spacing: -1px; }
h2 { font-family: 'Syne', sans-serif; font-weight: 700; color: #79c0ff; }
h3 { font-family: 'Syne', sans-serif; font-weight: 600; color: #9ecbff; }

/* text areas & inputs */
textarea, input[type="text"], input[type="number"], input[type="password"] {
    font-family: 'JetBrains Mono', monospace !important;
    background: #161b22 !important;
    color: #e6edf3 !important;
    border: 1px solid #30363d !important;
    border-radius: 6px !important;
}

/* buttons */
button[kind="primary"], .stButton > button {
    background: #1f6feb !important;
    border: none !important;
    color: #fff !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-weight: 600 !important;
    border-radius: 6px !important;
    padding: 0.4rem 1.2rem !important;
    transition: background 0.15s;
}
button[kind="primary"]:hover, .stButton > button:hover {
    background: #388bfd !important;
}

/* log terminal box */
.log-box {
    background: #0d1117;
    border: 1px solid #21262d;
    border-radius: 8px;
    padding: 1rem 1.2rem;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.78rem;
    line-height: 1.7;
    max-height: 520px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-word;
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

/* progress bar */
div[data-testid="stProgress"] > div > div {
    background-color: #21262d !important;
}
div[data-testid="stProgress"] > div > div > div {
    background: linear-gradient(90deg, #1f6feb, #58a6ff) !important;
}

/* section cards */
.card {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 10px;
    padding: 1.2rem 1.5rem;
    margin-bottom: 1rem;
}

/* status pill */
.pill {
    display: inline-block;
    padding: 2px 12px;
    border-radius: 999px;
    font-size: 0.75rem;
    font-family: 'JetBrains Mono', monospace;
    font-weight: 600;
    margin-left: 8px;
}
.pill-idle    { background:#21262d; color:#8b949e; }
.pill-running { background:#1f6feb22; color:#58a6ff; border: 1px solid #1f6feb55; }
.pill-success { background:#23863633; color:#3fb950; border: 1px solid #23863655; }
.pill-failure { background:#da363333; color:#f85149; border: 1px solid #da363355; }

/* protocol preview */
.proto-preview {
    background: #0d1117;
    border-left: 3px solid #1f6feb;
    padding: 0.8rem 1rem;
    border-radius: 0 6px 6px 0;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.78rem;
    white-space: pre-wrap;
    color: #c9d1d9;
}

/* expander content wrapper */
.expander-content {
    padding: 0.5rem 0;
}

div[data-testid="stExpander"] {
    border: 1px solid #21262d !important;
    background: #161b22 !important;
    border-radius: 8px !important;
}
</style>
"""

# ── custom CSS  (dark terminal aesthetic) ────────────────────────────────── #
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ── UI helper functions ──────────────────────────────────────────────────── #
def render_expander_section(title, content_func):
    """Render a standardized expander section with consistent styling."""
    with st.expander(title, expanded=False):
        st.markdown("<div class='expander-content'>", unsafe_allow_html=True)
        content_func()
        st.markdown("</div>", unsafe_allow_html=True)


def render_file_block(title, body, language="python"):
    """Render a file block with title and code content."""
    st.markdown(f"### {title}")
    st.code(body, language=language)


# ── session state defaults ────────────────────────────────────────────────── #


# ── Helper functions for custom model configuration ───────────────────────── #
def _find_opencode_config_dir() -> Path | None:
    """Find a directory containing .config/opencode under user's home."""
    home = Path.home()
    # Fix 3: use os.path.join instead of / operator for string path concatenation
    config_dir = Path(os.path.join(str(home), ".config", "opencode"))
    if config_dir.exists() and config_dir.is_dir():
        return config_dir

    # On Windows, also check AppData/Local equivalent
    if sys.platform == "win32":
        config_dir_win = Path(os.path.join(str(home), "AppData", "Local", "opencode"))
        if config_dir_win.exists() and config_dir_win.is_dir():
            return config_dir_win

    # Try to create the standard location
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def _get_opencode_config_file(config_dir: Path) -> Path:
    """Get the opencode.json file, creating it if it doesn't exist."""
    opencode_json = Path(os.path.join(str(config_dir), "opencode.json"))
    config_json = Path(os.path.join(str(config_dir), "config.json"))

    # Prefer opencode.json if it exists
    if opencode_json.exists():
        target_file = opencode_json
    elif config_json.exists():
        target_file = config_json
    else:
        # Create empty opencode.json with correct structure
        default_content = {"$schema": "https://opencode.ai/config.json", "provider": {}}
        opencode_json.write_text(json.dumps(default_content, indent=2), encoding="utf-8")
        target_file = opencode_json

    # Inject MCP session
    try:
        content = json.loads(target_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        content = {"$schema": "https://opencode.ai/config.json", "provider": {}}

    if "mcp" not in content:
        content["mcp"] = {}

    hashline_path = str(Path(__file__).parent.resolve() / "mcp_server" / "hashline.py")
    hashline_path = hashline_path.replace("\\", "/")

    mcp_hashline_config = {
        "type": "local",
        "command": ["python", hashline_path],
        "enabled": True,
        "environment": {}
    }

    # Only write to file if the configuration is not already present
    if content["mcp"].get("hashline") != mcp_hashline_config:
        content["mcp"]["hashline"] = mcp_hashline_config
        target_file.write_text(json.dumps(content, indent=2), encoding="utf-8")

    return target_file


def _add_custom_provider_to_config(
    config_file: Path,
    service_name: str,
    base_url: str,
    api_key: str,
    model_names: list[str],
):
    """Add a new provider entry to the opencode config file."""
    try:
        content = json.loads(config_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        content = {"$schema": "https://opencode.ai/config.json", "provider": {}}

    if "provider" not in content:
        content["provider"] = {}

    # Build models dict from provided model names
    models_dict = {name.strip(): {} for name in model_names if name.strip()}

    # Create the new provider entry using the correct structure
    new_provider = {
        "npm": "@ai-sdk/openai-compatible",
        "options": {"baseURL": base_url, "apiKey": api_key},
        "models": models_dict,
    }

    content["provider"][service_name] = new_provider

    config_file.write_text(json.dumps(content, indent=2), encoding="utf-8")


def _fetch_opencode_models(exe: str = "opencode") -> list[str]:
    """Run 'opencode models' and return the list of model identifiers."""
    try:
        proc = subprocess.run(
            [exe, "models"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0:
            return [
                line.strip()
                for line in proc.stdout.strip().splitlines()
                if line.strip()
            ]
    except Exception:
        pass
    return []


# ── Ensure MCP Config is injected on startup ──────────────────────────────── #
if not st.session_state.get("_mcp_config_done"):
    _mcp_dir = _find_opencode_config_dir()
    if _mcp_dir:
        _get_opencode_config_file(_mcp_dir)
    st.session_state["_mcp_config_done"] = True


# Load persisted settings first — used as defaults below
_persisted = load_settings()

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
    "opencode_model": "",
    "opencode_executable": "",
    "max_retries": 3,
    "context_threshold": 60,
    "max_tokens": 150000,
    "timeout": 120,
    "plan_mode_rounds": 1,
    "protected_files": [],
    "_last_workspace": "",
    # self-evolution page
    "evo_goal": "",
    "evo_extra_restrictions": "",
    "evo_meta_protocol_md": "",
    "evo_log_events": [],
    "evo_run_state": "idle",
    "evo_report": "",
    "evo_wizard_step": 0,
    "self_evolution_verbose": False,
    "verbose_log": True,
    # internal state for live run
    "_run_heartbeat": 0,
    # internal state for self-evolution
    "_evo_heartbeat": 0,
    # connectivity test flags
    "opencode_test_passed": False,
    "supervisor_test_passed": False,
    "opencode_models": [],
}
for k, v in defaults.items():
    if k not in st.session_state:
        # Use persisted value if available, else default
        st.session_state[k] = _persisted.get(k, v)

# ── Fetch opencode models once per session ──────────────────────────────── #
if not st.session_state["opencode_models"]:
    st.session_state["opencode_models"] = _fetch_opencode_models()

# ── sidebar navigation ────────────────────────────────────────────────────── #
with st.sidebar:
    st.markdown("## 🤖 opencode<br>**Supervisor**", unsafe_allow_html=True)
    st.markdown("---")

    pill_map = {
        "PENDING": '<span class="pill pill-idle">queued…</span>',
        "RUNNING": '<span class="pill pill-running">running</span>',
        "SUCCESS": '<span class="pill pill-success">done ✓</span>',
        "FAILED": '<span class="pill pill-failure">failed ✗</span>',
        "CANCELLED": '<span class="pill pill-failure">cancelled ⏹</span>',
    }

    # Retrieve current job IDs from query params
    run_job_id = st.query_params.get("run_job_id")
    evo_job_id = st.query_params.get("evo_job_id")

    if run_job_id:
        status = job_manager.get_job_status(run_job_id)
        if status:
            st.markdown(
                f"**Live Run** {pill_map.get(status['state'], '')}",
                unsafe_allow_html=True,
            )

    if evo_job_id:
        status = job_manager.get_job_status(evo_job_id)
        if status:
            st.markdown(
                f"**Self-evo** {pill_map.get(status['state'], '')}",
                unsafe_allow_html=True,
            )

    st.markdown("---")

    pages = {
        "wizard": "① Protocol Wizard",
        "run": "② Live Run",
        "evolve": "③ Self-Evolution",
    }
    tests_passed = (
        st.session_state.opencode_test_passed
        and st.session_state.supervisor_test_passed
    )
    for key, label in pages.items():
        locked = key != "wizard" and not tests_passed
        active = st.session_state.page == key
        if locked:
            if st.button(
                f"🔒 {label}",
                key=f"nav_{key}",
                use_container_width=True,
                disabled=True,
            ):
                pass
        else:
            if st.button(
                label,
                key=f"nav_{key}",
                use_container_width=True,
                type="primary" if active else "secondary",
            ):
                st.session_state.page = key
                st.rerun()

    if not tests_passed:
        st.caption("🔒 Run & Self-Evolution locked — pass connectivity tests first.")

    # ── Add Custom Model for Opencode (sidebar, wizard page only) ───────── #
    if st.session_state.page == "wizard":
        st.markdown("---")
        st.markdown("### 🤖 Add Custom Model for Opencode")

        # Initialize session state for custom model form
        if "show_custom_model_form" not in st.session_state:
            st.session_state.show_custom_model_form = False

        if st.button("➕ Add Custom Model for Opencode", key="btn_add_custom_model"):
            st.session_state.show_custom_model_form = True

        if st.session_state.show_custom_model_form:
            st.markdown("**Custom Service Configuration**")

            service_name = st.text_input(
                "Service name",
                key="custom_service_name",
                placeholder="my-custom-service",
                help="Prefix with slash when using, e.g. /my-service",
            )

            base_url = st.text_input(
                "Base URL",
                key="custom_base_url",
                placeholder="https://api.example.com/v1",
            )

            api_key = st.text_input(
                "API key", key="custom_api_key", type="password", placeholder="sk-..."
            )

            # Fix 2: Add model names input
            st.markdown("**Model names** *(one per line)*")
            model_names_input = st.text_area(
                "Model names",
                key="custom_model_names",
                height=100,
                placeholder="qwen3-coder-plus\nqwen3-max\nkimi-k2-0905",
                label_visibility="collapsed",
                help="Enter one model name per line. These will be added under the provider's models key.",
            )

            if st.button("💾 Save Service", key="btn_save_custom_service"):
                model_names = [
                    m.strip() for m in model_names_input.splitlines() if m.strip()
                ]
                if (
                    not service_name.strip()
                    or not base_url.strip()
                    or not api_key.strip()
                ):
                    st.error("Please fill in service name, base URL, and API key.")
                elif not model_names:
                    st.error("Please enter at least one model name.")
                else:
                    try:
                        config_dir = _find_opencode_config_dir()
                        if config_dir is None:
                            st.error(
                                "Could not find or create opencode config directory."
                            )
                        else:
                            config_file = _get_opencode_config_file(config_dir)
                            _add_custom_provider_to_config(
                                config_file,
                                service_name.strip(),
                                base_url.strip(),
                                api_key.strip(),
                                model_names,
                            )
                            st.success("✅ Service saved successfully!")
                            st.info(
                                f"Models can now be referenced as `{service_name.strip()}/<model-name>`"
                            )
                            st.session_state.show_custom_model_form = False
                            # Clear the form inputs
                            st.session_state.custom_service_name = ""
                            st.session_state.custom_base_url = ""
                            st.session_state.custom_api_key = ""
                            st.session_state.custom_model_names = ""
                    except Exception as e:
                        st.error(f"Failed to save service: {e}")

    st.markdown("---")
    st.caption("streamlit · opencode")


# ═══════════════════════════════════════════════════════════════════════════ #
# PAGE 1 — Protocol Wizard                                                    #
# ═══════════════════════════════════════════════════════════════════════════ #


# ═══════════════════════════════════════════════════════════════════════════ #
# Connectivity tests                                                          #
# ═══════════════════════════════════════════════════════════════════════════ #


def _run_with_timeout(fn, seconds=30):
    """Run fn() in a daemon thread; raise TimeoutError if it exceeds `seconds`."""
    import threading
    
    result = []
    error = []

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


def test_opencode():
    import tempfile
    from supervisor.runners.opencode_runner import OpencodeRunner, find_opencode

    workspace = Path(tempfile.gettempdir()) / "opencode_test_dummy"
    workspace.mkdir(exist_ok=True) # Ensure dummy dir exists

    try:
        exe = find_opencode(st.session_state.opencode_executable or "")
    except FileNotFoundError as e:
        return False, str(e)

    runner = OpencodeRunner(
        workspace=workspace,
        opencode_model=st.session_state.opencode_model or None,
        opencode_executable=exe,
        timeout=30,
    )

    def _inner():
        runner.start("hi")
        # Ensure we don't hang indefinitely reading output
        output, timed_out = runner.read_output(timeout=25)
        if timed_out:
            return False, "opencode timed out reading output."
        if runner._last_result and runner._last_result.ok:
            return True, "opencode responded successfully."
        diag = runner.last_diagnostic() if runner._last_result else "(no result)"
        return False, f"opencode returned an error.\n{diag}"

    try:
        # Give the wrapper a slightly longer timeout than the internal read timeout
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


def test_supervisor():
    if not st.session_state.openai_key:
        return False, "API key is not set."

    model = st.session_state.supervisor_model or "gpt-4o"
    client = OpenAI(
        api_key=st.session_state.openai_key,
        base_url=st.session_state.base_url or None,
        timeout=25.0,  # connection + read timeout on the socket
    )

    def _inner():
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
        )
        text = resp.choices[0].message.content or ""
        if text.strip():
            return True, f"Supervisor responded: {text.strip()[:120]}"
        return False, "Supervisor returned an empty response."

    try:
        return _run_with_timeout(_inner, seconds=30)
    except TimeoutError:
        return False, "Supervisor test timed out."
    except Exception as exc:
        return False, f"Supervisor test failed: {exc}"


def page_wizard():
    from supervisor.runners.opencode_runner import find_opencode

    st.markdown("# Protocol Wizard")
    st.markdown(
        "Fill in each section in plain language. The supervisor LLM will refine "
        "them into a clean, unambiguous `protocol.md`."
    )

    try:
        find_opencode()
    except FileNotFoundError as e:
        st.error(str(e))
        st.stop()

    # ── config panel ──────────────────────────────────────────────────── #
    with st.expander("⚙️  Configuration", expanded=st.session_state.wizard_step == 0):
        col1, col2 = st.columns(2)
        with col1:
            st.session_state.openai_key = st.text_input(
                "API Key",
                key="cfg_openai_key",
                value=st.session_state.openai_key,
                type="password",
                placeholder="sk-…",
            )
            st.session_state.base_url = st.text_input(
                "Base URL (leave blank for OpenAI)",
                key="cfg_base_url",
                value=st.session_state.base_url,
                placeholder="e.g. http://localhost:11434/v1",
            )
            st.session_state.workspace = st.text_input(
                "Workspace path (absolute)",
                key="cfg_workspace",
                value=st.session_state.workspace,
                placeholder="/home/user/myproject",
            )
            if st.session_state.workspace != st.session_state.get(
                "_last_workspace", ""
            ):
                st.session_state.protected_files = []
                st.session_state._last_workspace = st.session_state.workspace
            st.session_state.supervisor_model = st.text_input(
                "Supervisor / wizard model",
                key="cfg_supervisor_model",
                value=st.session_state.supervisor_model,
                placeholder="e.g. gpt-4o, claude-3-5-sonnet, mistral-large",
            )
        with col2:
            # Dynamic model list from 'opencode models' command
            models = st.session_state.get("opencode_models", [])
            if models:
                # Determine default index
                current = st.session_state.get("opencode_model", "")
                default_idx = models.index(current) if current in models else 0
                selected = st.selectbox(
                    "Model",
                    options=models,
                    index=default_idx,
                    key="cfg_opencode_model_select",
                    help="Models returned by 'opencode models'",
                )
                st.session_state.opencode_model = selected
            else:
                st.warning("No models returned by 'opencode models'.")
                st.session_state.opencode_model = st.text_input(
                    "opencode model",
                    key="cfg_opencode_model_fallback",
                    value=st.session_state.opencode_model,
                )

            def _refresh_models():
                st.session_state["opencode_models"] = _fetch_opencode_models()

            st.button(
                "🔄 Refresh models",
                key="btn_refresh_models",
                on_click=_refresh_models,
            )

            st.session_state.max_retries = st.number_input(
                "Max retries",
                key="cfg_max_retries",
                min_value=1,
                max_value=20,
                value=int(st.session_state.max_retries),
            )
            st.session_state.context_threshold = st.slider(
                "Context compaction threshold (%)",
                20,
                95,
                key="cfg_ctx_threshold",
                value=int(st.session_state.context_threshold),
            )
            st.session_state.max_tokens = st.number_input(
                "Max tokens (model context window)",
                key="cfg_max_tokens",
                min_value=1000,
                max_value=1000000,
                value=int(st.session_state.max_tokens),
                step=1000,
            )
            st.session_state.timeout = st.number_input(
                "Timeout (min)",
                key="cfg_timeout",
                min_value=1,
                max_value=999,
                value=min(max(int(st.session_state.timeout), 1), 999),
            )
        with st.expander("🛡️  Protected Files", expanded=False):
            st.caption("Files that opencode cannot modify or delete")
            protected = st.session_state.get("protected_files", [])
            if not isinstance(protected, list):
                protected = []

            workspace_path = (
                Path(st.session_state.workspace) if st.session_state.workspace else None
            )
            all_files = []
            if workspace_path and workspace_path.exists():
                try:

                    def is_in_dot_dir(path: Path, workspace: Path) -> bool:
                        rel = path.relative_to(workspace)
                        for part in rel.parts[:-1]:
                            if part.startswith("."):
                                return True
                        return False

                    def contains_debug_dir(path: Path, workspace: Path) -> bool:
                        rel = path.relative_to(workspace)
                        for part in rel.parts[:-1]:
                            if "debug" in part.lower():
                                return True
                        return False

                    all_files = sorted(
                        [
                            str(f.relative_to(workspace_path)).replace("\\", "/")
                            for f in workspace_path.rglob("*")
                            if f.is_file()
                            and not is_in_dot_dir(f, workspace_path)
                            and not contains_debug_dir(f, workspace_path)
                        ]
                    )
                except Exception:
                    pass

            current_protected_set = set(protected)
            available_files = [f for f in all_files if f not in current_protected_set]

            st.markdown("**Add protected files:**")
            selected_to_add = st.multiselect(
                "Select files to protect",
                options=available_files,
                key="protected_files_multiselect",
                label_visibility="collapsed",
                placeholder="Choose files from workspace...",
            )
            if selected_to_add:
                new_protected = list(set(protected) | set(selected_to_add))
                st.session_state.protected_files = new_protected
                st.rerun()

            if protected:
                st.success(f"{len(protected)} file(s) protected")
                for pf in protected:
                    col1, col2 = st.columns([4, 1])
                    with col1:
                        st.code(pf, language=None)
                    with col2:
                        if st.button("✕", key=f"remove_pf_{pf}"):
                            st.session_state.protected_files = [
                                x for x in protected if x != pf
                            ]
                            st.rerun()

        with st.expander("🚫 Ignore Patterns (.opencodeignore)", expanded=False):
            from supervisor.workspace.ignore_patterns import (
                IGNORE_FILE,
                write_ignore_file,
            )

            st.caption(
                f"Files matching these patterns will be excluded from context retrieval"
            )

            ws_path = (
                Path(st.session_state.workspace) if st.session_state.workspace else None
            )
            if not ws_path or not ws_path.exists():
                st.warning("Set a valid workspace path to edit .opencodeignore")
            else:
                current_ignore_content = ""
                ignore_file_path = ws_path / IGNORE_FILE
                if ignore_file_path.exists():
                    try:
                        current_ignore_content = ignore_file_path.read_text(
                            encoding="utf-8"
                        )
                    except Exception:
                        pass

                new_ignore_content = st.text_area(
                    "Ignore patterns",
                    value=current_ignore_content,
                    height=200,
                    key="ignore_patterns_editor",
                    placeholder=(
                        "# Patterns to ignore (one per line)\n"
                        "# Examples:\n"
                        "# *.pyc           # ignore all .pyc files\n"
                        "# debug*          # ignore files starting with debug\n"
                        "# *test.py        # ignore files ending with test.py\n"
                        "# build/          # ignore entire build directory\n"
                        "# **/*.log        # ignore all .log files\n"
                    ),
                    label_visibility="collapsed",
                )

                if new_ignore_content != current_ignore_content:
                    if st.button("Save Ignore Patterns", key="save_ignore_patterns"):
                        try:
                            write_ignore_file(ws_path, new_ignore_content)
                            st.success(f"Saved {IGNORE_FILE}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Failed to save: {e}")

                if ignore_file_path.exists():
                    st.caption(
                        f"Found existing {IGNORE_FILE} with {len(current_ignore_content.splitlines())} patterns"
                    )

                st.markdown("---")
                suggest_disabled = not (
                    st.session_state.workspace
                    and Path(st.session_state.workspace).exists()
                )
                if st.button(
                    "Suggest and Apply Ignore Patterns",
                    key="btn_suggest_ignore",
                    disabled=suggest_disabled,
                ):
                    try:
                        ws = Path(st.session_state.workspace)
                        all_entries = sorted(
                            [
                                str(p.relative_to(ws)).replace("\\", "/")
                                for p in ws.rglob("*")
                                if str(p.relative_to(ws)) != ".opencodeignore"
                            ]
                        )
                        file_list_str = "\n".join(all_entries)
                        truncation_note = ""
                        token_count = estimate_tokens(file_list_str)
                        if token_count > 100000:
                            all_entries = all_entries[:1000]
                            file_list_str = "\n".join(all_entries)
                            truncation_note = (
                                "Note: The file list was truncated to the first 1,000 entries "
                                "due to token limits."
                            )

                        client = OpenAI(
                            api_key=st.session_state.openai_key,
                            base_url=st.session_state.base_url or None,
                        )
                        model = st.session_state.supervisor_model or "gpt-4o"
                        system_msg = (
                            "You are an expert in writing .gitignore files. "
                            "Given a list of files and directories in a workspace, "
                            "generate a .opencodeignore file that ignores common build "
                            "artifacts, dependency directories, cache files, and other "
                            "files that should not be modified by an autonomous coding "
                            "agent. The patterns should be in gitignore format. Only "
                            "output the patterns, one per line. Do not include any "
                            "explanations."
                        )
                        user_msg = (
                            f"The workspace contains the following files and directories:\n\n"
                            f"{file_list_str}\n\n"
                            f"{truncation_note}\n\n"
                            f"Generate a .opencodeignore file that ignores common build "
                            f"artifacts, dependency directories, cache files, and other "
                            f"files that should not be modified by an autonomous coding "
                            f"agent. The patterns should be in gitignore format. Only "
                            f"output the patterns, one per line. Do not include any "
                            f"explanations."
                        )
                        response = client.chat.completions.create(
                            model=model,
                            messages=[
                                {"role": "system", "content": system_msg},
                                {"role": "user", "content": user_msg},
                            ],
                        )
                        generated_patterns = response.choices[0].message.content.strip()

                        st.text_area(
                            "Generated .opencodeignore patterns",
                            value=generated_patterns,
                            height=300,
                            key="generated_ignore_patterns",
                            disabled=True,
                        )
                        ignore_file_path.write_text(
                            generated_patterns, encoding="utf-8"
                        )
                        st.toast(
                            "Ignore patterns generated and saved to .opencodeignore."
                        )
                    except Exception as e:
                        st.error(f"Failed to generate ignore patterns: {e}")

    # Auto-save settings to disk whenever the config panel is shown
    save_settings()

    # ── connectivity tests ────────────────────────────────────────────────── #
    st.markdown("---")
    st.markdown("### 🔌 Connectivity Tests")

    both_passed = (
        st.session_state.opencode_test_passed
        and st.session_state.supervisor_test_passed
    )
    if both_passed:
        st.success("✅ Both opencode and supervisor connectivity tests passed.")
    else:
        st.info("Run the tests below to verify opencode and supervisor are reachable.")

    col_t1, col_t2, col_t3 = st.columns(3)

    with col_t1:
        if st.button("▶  Run Tests", type="primary", key="btn_run_tests"):
            if not st.session_state.workspace:
                st.error("Set a workspace path before running tests.")
            else:
                with st.spinner("Testing opencode…"):
                    ok, msg = test_opencode()
                if ok:
                    st.session_state.opencode_test_passed = True
                    st.success(f"✅ Test opencode: {msg}")
                else:
                    st.session_state.opencode_test_passed = False
                    st.error(f"❌ Test opencode: {msg}")

                with st.spinner("Testing supervisor…"):
                    ok2, msg2 = test_supervisor()
                if ok2:
                    st.session_state.supervisor_test_passed = True
                    st.success(f"✅ Test Supervisor: {msg2}")
                else:
                    st.session_state.supervisor_test_passed = False
                    st.error(f"❌ Test Supervisor: {msg2}")

    with col_t2:
        if st.button("🧪 Test opencode", key="btn_test_opencode"):
            if not st.session_state.workspace:
                st.error("Set a workspace path before testing.")
            else:
                with st.spinner("Testing opencode…"):
                    ok, msg = test_opencode()
                if ok:
                    st.session_state.opencode_test_passed = True
                    st.success(f"✅ {msg}")
                else:
                    st.session_state.opencode_test_passed = False
                    st.error(f"❌ {msg}")

    with col_t3:
        if st.button("🧪 Test Supervisor", key="btn_test_supervisor"):
            if not st.session_state.openai_key:
                st.error("Set an API key before testing.")
            else:
                with st.spinner("Testing supervisor…"):
                    ok, msg = test_supervisor()
                if ok:
                    st.session_state.supervisor_test_passed = True
                    st.success(f"✅ {msg}")
                else:
                    st.session_state.supervisor_test_passed = False
                    st.error(f"❌ {msg}")

    # ── existing protocol.md detection ───────────────────────────────────── #
    workspace_path = (
        Path(st.session_state.workspace) if st.session_state.workspace else None
    )
    if workspace_path:
        existing_proto = workspace_path / "protocol.md"
        if existing_proto.exists() and not st.session_state.protocol_md:
            existing_text = existing_proto.read_text(encoding="utf-8")
            st.info(f"📄 An existing `protocol.md` was found in your workspace.")
            col_reuse, col_ignore, _ = st.columns([1, 1, 3])
            with col_reuse:
                if st.button(
                    "♻️  Use existing protocol.md", type="primary", key="btn_reuse_proto"
                ):
                    st.session_state.protocol_md = existing_text
                    st.session_state.wizard_step = 1
                    st.rerun()
            with col_ignore:
                if st.button("✏️  Write new one", key="btn_ignore_proto"):
                    pass  # just fall through to the form
            with st.expander("Preview existing protocol.md"):
                st.code(existing_text[:1500], language="markdown")
            st.markdown("---")

    # ── three-section form ─────────────────────────────────────────────── #
    st.markdown("### ✍️ Draft your protocol")

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("**INPUT** — what already exists / what the agent starts with")
    st.text_area(
        "input_area",
        key="raw_input",
        height=120,
        placeholder="e.g. A Python repo is at ./src. The main entry point is main.py.",
        label_visibility="collapsed",
    )
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("**TARGET** — concrete, testable deliverables")
    st.text_area(
        "target_area",
        key="raw_target",
        height=140,
        placeholder=(
            "e.g.\n"
            "1. Build a FastAPI server in src/main.py with GET /health and POST /echo\n"
            "2. Add requirements.txt\n"
            "3. All tests in ./tests/ must pass"
        ),
        label_visibility="collapsed",
    )
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("**RESTRICTIONS** — hard rules the agent must not break")
    st.text_area(
        "restrictions_area",
        key="raw_restrictions",
        height=100,
        placeholder=(
            "e.g.\n"
            "- Don't touch files outside ./src\n"
            "- No system package installs\n"
            "- Keep code under 300 lines"
        ),
        label_visibility="collapsed",
    )
    st.markdown("</div>", unsafe_allow_html=True)

    # ── live quality analysis ─────────────────────────────────────────── #
    if (
        st.session_state.raw_input.strip()
        or st.session_state.raw_target.strip()
        or st.session_state.raw_restrictions.strip()
    ):

        def _quality_preview_content():
            _render_quality_analysis(
                st.session_state.raw_input,
                st.session_state.raw_target,
                st.session_state.raw_restrictions,
            )

        render_expander_section("📊 Protocol Quality Preview", _quality_preview_content)

    # ── refine button ──────────────────────────────────────────────────── #
    refine_clicked = st.button("✨  Refine with AI", type="primary")

    if refine_clicked:
        missing = []
        if not st.session_state.openai_key:
            missing.append("OpenAI API Key")
        if not st.session_state.workspace:
            missing.append("Workspace path")
        if not st.session_state.raw_input.strip():
            missing.append("INPUT section")
        if not st.session_state.raw_target.strip():
            missing.append("TARGET section")
        if not st.session_state.raw_restrictions.strip():
            missing.append("RESTRICTIONS section")

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

    # ── preview & accept ──────────────────────────────────────────────── #
    if st.session_state.wizard_step == 1 and st.session_state.protocol_md:
        st.markdown("---")
        st.markdown("### 📄 Refined `protocol.md`")
        st.markdown("*Review and edit below, then accept.*")

        edited = st.text_area(
            "proto_edit",
            key="protocol_md",
            height=300,
            label_visibility="collapsed",
        )

        def _quality_analysis_content():
            _render_refined_quality_analysis(st.session_state.protocol_md)

        render_expander_section(
            "📊 Protocol Quality Analysis", _quality_analysis_content
        )

        col_a, col_b, _ = st.columns([1, 1, 3])
        with col_a:
            if st.button("✅  Accept & Save", type="primary"):
                _save_protocol()
                st.success("protocol.md saved to workspace.")
        with col_b:
            if st.button("🔄  Re-refine"):
                st.session_state.wizard_step = 0
                st.rerun()


def _render_quality_analysis(raw_input: str, raw_target: str, raw_restrictions: str):
    """Render real-time quality analysis of raw protocol sections."""
    analyzer = ProtocolAnalyzer()
    temp_text = (
        f"## INPUT\n\n{raw_input}\n\n"
        f"## TARGET\n\n{raw_target}\n\n"
        f"## RESTRICTIONS\n\n{raw_restrictions}\n"
    )
    try:
        analysis = analyzer.analyze_text(temp_text)
    except Exception:
        st.caption("Complete all three sections to see quality scores.")
        return

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Overall", f"{analysis.overall_score:.0%}")
    with col2:
        st.metric("INPUT", f"{analysis.input_score.overall:.0%}")
    with col3:
        st.metric("TARGET", f"{analysis.target_score.overall:.0%}")
    with col4:
        st.metric("RESTRICTIONS", f"{analysis.restrictions_score.overall:.0%}")

    if analysis.issues:
        st.caption(f"Found {len(analysis.issues)} issue(s)")
        for issue in analysis.issues[:5]:
            icon = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}[issue.severity.value]
            st.caption(f"{icon} [{issue.section}] {issue.message}")


def _render_refined_quality_analysis(refined_md: str):
    """Render quality analysis for a refined protocol markdown."""
    analyzer = ProtocolAnalyzer()
    try:
        analysis = analyzer.analyze_text(refined_md)
    except Exception as e:
        st.warning(f"Cannot analyze protocol: {e}")
        return

    rating_colors = {
        "excellent": "🟢",
        "good": "🟡",
        "fair": "🟠",
        "poor": "🔴",
    }
    color = rating_colors.get(analysis.quality_rating, "⚪")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Overall", f"{analysis.overall_score:.0%}")
    with col2:
        st.metric("INPUT", f"{analysis.input_score.overall:.0%}")
    with col3:
        st.metric("TARGET", f"{analysis.target_score.overall:.0%}")
    with col4:
        st.metric("RESTRICTIONS", f"{analysis.restrictions_score.overall:.0%}")

    st.caption(f"{color} Quality: {analysis.quality_rating}")

    if analysis.issues:
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

            def _suggestions_content():
                for issue in infos:
                    st.caption(f"ℹ️ [{issue.section}] {issue.message}")
                    if issue.suggestion:
                        st.caption(f"   → {issue.suggestion}")

            render_expander_section(f"{len(infos)} suggestion(s)", _suggestions_content)


def _save_protocol():
    workspace = Path(st.session_state.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    proto_path = workspace / "protocol.md"
    proto_path.write_text(st.session_state.protocol_md, encoding="utf-8")
    st.session_state.protocol_saved_path = str(proto_path)


# ═══════════════════════════════════════════════════════════════════════════ #
# PAGE 2 — Live Run                                                           #
# ═══════════════════════════════════════════════════════════════════════════ #

def page_run():
    st.markdown("# Live Run")

    # Check for existing job in query params
    job_id = st.query_params.get("run_job_id")
    if job_id:
        _show_run_status_screen(job_id)
    else:
        _show_run_setup_screen()

def _show_run_setup_screen():
    # Pre-flight check
    workspace = Path(st.session_state.workspace) if st.session_state.workspace else None
    if not workspace:
        st.warning("Please set a workspace path in the Protocol Wizard configuration.")
        return

    if not workspace.exists():
        st.error(f"Workspace directory does not exist: {workspace}")
        return

    proto_path = workspace / "protocol.md"
    if not proto_path.exists():
        st.error(f"**protocol.md not found** in workspace: `{workspace}`")
        st.info("Please complete the Protocol Wizard to generate a protocol.md file.")
        return

    st.markdown(
        f"**Workspace:** `{workspace}`  \n"
        f"**Protocol:** `{proto_path}`  \n"
        f"**Supervisor model:** `{st.session_state.supervisor_model}`"
    )

    col1, col2 = st.columns([2, 1])
    with col1:
        plan_rounds = st.number_input(
            "Plan mode rounds",
            min_value=0,
            max_value=10,
            value=int(st.session_state.plan_mode_rounds),
            key="run_plan_mode_rounds_setup",
            help="Number of planning rounds before execution",
        )
    with col2:
        if st.button("▶  Start Live Run", type="primary", use_container_width=True):
            st.session_state.plan_mode_rounds = plan_rounds
            job_id = _enqueue_run_job()
            st.query_params["run_job_id"] = job_id
            st.rerun()

def _enqueue_run_job() -> str:
    save_settings()
    apply_api_config()
    
    workspace = Path(st.session_state.workspace)
    proto_path = workspace / "protocol.md"
    
    config = SupervisorConfig(
        protocol_path=proto_path,
        workspace=workspace,
        max_retries=int(st.session_state.max_retries),
        context_threshold=st.session_state.context_threshold / 100.0,
        opencode_model=st.session_state.opencode_model or None,
        opencode_executable=st.session_state.opencode_executable,
        supervisor_model=st.session_state.supervisor_model,
        timeout=int(st.session_state.timeout) * 60,
        protected_files=tuple(st.session_state.get("protected_files", [])),
        max_tokens=int(st.session_state.max_tokens),
        plan_mode_rounds=int(st.session_state.plan_mode_rounds),
    )
    
    return job_manager.enqueue_job("run", config)

def _show_run_status_screen(job_id: str):
    status = job_manager.get_job_status(job_id)
    if not status:
        st.error(f"Job {job_id} not found.")
        if st.button("Back to Setup"):
            del st.query_params["run_job_id"]
            st.rerun()
        return

    state = status["state"]
    
    # Header with status and control buttons
    col_h1, col_h2, col_h3 = st.columns([3, 1, 1])
    with col_h1:
        st.markdown(f"### Job: `{job_id}`")
    with col_h2:
        if state == "RUNNING":
            if st.button("⏹ Stop", use_container_width=True):
                job_manager.cancel_job(job_id)
                st.rerun()
        else:
            if st.button("🗑 Clear", use_container_width=True):
                del st.query_params["run_job_id"]
                st.rerun()
    with col_h3:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()

    # Progress and details
    if state == "RUNNING":
        st.info("🏃 Job is running in background. You can safely close this tab or refresh.")
        # Auto-refresh loop
        time.sleep(2)
        st.rerun()

    # Layout for logs and info
    col_main, col_side = st.columns([2, 1])
    
    with col_main:
        _render_step_progress(status.get("logs", []), state)
        st.markdown("#### 🖥️ Live Log")
        _render_events(status.get("logs", []), "— waiting for logs —", show_verbose=True)
        
    with col_side:
        st.markdown("#### ℹ️ Details")
        st.markdown(f"**State:** {state}")
        st.markdown(f"**Started:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(status.get('updated_at', 0)))}")
        
        # Token usage if available
        _render_token_usage_bar(status.get("logs", []), int(st.session_state.max_tokens))

        if status.get("report"):
            st.markdown("#### 📊 Report")
            with st.expander("View Report", expanded=True):
                st.markdown(status["report"])
                st.download_button(
                    "⬇ Download",
                    data=status["report"],
                    file_name=f"report_{job_id}.md",
                    mime="text/markdown"
                )

def _render_token_usage_bar(logs: list[dict], max_tokens: int):
    """Simplified token usage bar for the status screen."""
    import re
    latest_current = 0
    latest_fraction = 0.0
    found = False
    
    for ev in logs:
        msg = ev.get("msg", "")
        if "context usage" in msg.lower():
            match = re.search(r"(\d[\d,]*)\s*/\s*(\d[\d,]*)\s*tokens", msg)
            if match:
                current = int(match.group(1).replace(",", ""))
                max_t = int(match.group(2).replace(",", ""))
                fraction = current / max_t if max_t > 0 else 0
                if fraction >= latest_fraction:
                    latest_fraction = fraction
                    latest_current = current
                    found = True
                    
    if found:
        color = "🔴" if latest_fraction > 0.9 else "🟡" if latest_fraction > 0.7 else "🟢"
        st.progress(min(latest_fraction, 1.0), text=f"{color} {latest_current:,} / {max_tokens:,} tokens")

def _render_step_progress(logs: list[dict], run_state: str, is_evolution: bool = False):
    """Render progress bar, step history, and heartbeats."""
    step_events = [
        e for e in logs if e.get("level") in ("step", "phase_transition")
    ]
    progress_events = [
        e for e in logs if e.get("level") == "step_progress"
    ]
    heartbeat_events = [
        e for e in logs if e.get("level") == "heartbeat"
    ]

    process_label = "Evolution process active" if is_evolution else "Background process active"

    if run_state == "RUNNING":
        heartbeat_count = len(heartbeat_events)
        status_col1, status_col2, status_col3 = st.columns([3, 1, 1])
        with status_col1:
            st.markdown(f"🟢 **{process_label}**")
        with status_col2:
            st.caption(f"💓 {heartbeat_count} heartbeat(s)")
        with status_col3:
            st.caption(f"🧭 {len(step_events)} step(s)")
        if progress_events:
            last_progress = progress_events[-1]
            msg = last_progress.get("msg", "")

            def _progress_content():
                st.caption(msg)

            render_expander_section("📊 Progress", _progress_content)
    elif progress_events:
        last_progress = progress_events[-1]
        msg = last_progress.get("msg", "")

        progress_col1, progress_col2, progress_col3 = st.columns([3, 1, 1])
        with progress_col1:
            st.caption(f"📊 {msg}")
        with progress_col2:
            step_count = len(step_events)
            st.caption(f"🧭 {step_count} step(s)")
        with progress_col3:
            last_heartbeat = heartbeat_events[-1] if heartbeat_events else None
            if last_heartbeat:
                st.caption("🟢 active")

        progress_val = 0.0
        if progress_events:
            ev = progress_events[-1]
            if "percentage" not in ev:
                parts = ev.get("msg", "").split()
                for i, p in enumerate(parts):
                    if p.replace("%", "").replace(".", "").isdigit():
                        try:
                            progress_val = float(p.replace("%", ""))
                            break
                        except ValueError:
                            pass
            else:
                progress_val = ev.get("percentage", 0.0)

        if progress_val > 0:
            progress_col1, progress_col2 = st.columns([4, 1])
            with progress_col1:
                st.progress(progress_val / 100.0, text=f"{progress_val:.0f}% complete")
            with progress_col2:
                pass

        if step_events:
            with st.expander("📍 Step History", expanded=False):
                for ev in step_events[-5:]:
                    lvl = ev.get("level", "")
                    if lvl == "step":
                        st.caption(f"• {ev.get('msg', '')[:80]}")
                    elif lvl == "phase_transition":
                        st.caption(f"⚡ {ev.get('msg', '')}")



def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_BLOCK_META = {
    "opencode_prompt": ("hdr-oc-prompt", "▶ PROMPT → opencode"),
    "opencode_output": ("hdr-oc-output", "◀ OUTPUT ← opencode"),
    "supervisor_response": ("hdr-sv-response", "🧠 SUPERVISOR"),
    "supervisor_read_files": ("hdr-sv-read", "📂 SUPERVISOR READ FILES"),
}


def _render_events(
    events: list[dict],
    empty_msg: str,
    skip: set | None = None,
    show_verbose: bool = True,
) -> None:
    skip = skip or set()
    verbose = st.session_state.get("verbose_log", True)

    if show_verbose:
        # Verbose toggle
        st.session_state.verbose_log = st.toggle(
            "Verbose log",
            value=verbose,
            key=f"vtoggle_{empty_msg[:8].replace(' ', '_')}",
        )
        verbose = st.session_state.verbose_log

    if not events:
        st.markdown(
            f'<div class="log-box"><span class="log-info">{_esc(empty_msg)}</span></div>',
            unsafe_allow_html=True,
        )
        return

    lines_html: list[str] = []

    for ev in events[-600:]:
        lvl = ev.get("level", "info")
        if lvl in skip:
            continue
        msg = ev.get("msg", "")

        if lvl in _BLOCK_META:
            if not verbose:
                # Compact summary line instead of full content
                preview = _esc(msg[:120].replace("\n", " "))
                hdr_cls, hdr_label = _BLOCK_META[lvl]
                lines_html.append(
                    f'<span class="{hdr_cls}">{hdr_label}</span>'
                    + f'<span class="log-info" style="opacity:0.6"> {preview}…</span>\n'
                )
                continue

            # Verbose: full block with header
            hdr_cls, hdr_label = _BLOCK_META[lvl]
            lines_html.append(
                f'<span class="log-rule">{"─" * 60}</span>\n'
                + f'<span class="{hdr_cls}">{hdr_label}</span>\n'
                + f'<span class="log-{lvl}">{_esc(msg)}</span>\n'
            )
        else:
            lines_html.append(f'<span class="log-{lvl}">{_esc(msg)}</span>\n')

    body = "".join(lines_html)
    st.markdown(f'<div class="log-box">{body}</div>', unsafe_allow_html=True)




# ═══════════════════════════════════════════════════════════════════════════ #
# Self-Evolution                                                              #
# ═══════════════════════════════════════════════════════════════════════════ #

def page_evolve():
    st.markdown("# Self-Evolution")
    
    # Check for existing job in query params
    job_id = st.query_params.get("evo_job_id")
    if job_id:
        _show_evo_status_screen(job_id)
    else:
        _show_evo_setup_screen()

def _show_evo_setup_screen():
    st.markdown(
        "Point the supervisor + opencode at **this codebase itself**. "
        "Describe what you want improved or debugged — the system will "
        "auto-generate a `meta_protocol.md` from the live source tree, "
        "then run the full supervisor loop."
    )

    if not st.session_state.openai_key:
        st.warning("Enter your OpenAI API key in the Protocol Wizard config panel first.")
        return

    repo_root = Path(__file__).parent.resolve()
    st.info(f"**Repo root (workspace):** `{repo_root}`")

    # Step 0 — define the evolution goal
    if st.session_state.evo_wizard_step == 0:
        st.markdown("### 🎯 What do you want to evolve?")
        
        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown("**Evolution goal**")
        st.text_area("evo_goal_input", key="evo_goal", height=130, label_visibility="collapsed")
        st.markdown("</div>", unsafe_allow_html=True)

        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown("**Extra restrictions**")
        st.text_area("evo_restrictions_input", key="evo_extra_restrictions", height=80, label_visibility="collapsed")
        st.markdown("</div>", unsafe_allow_html=True)

        col_gen, col_snap, _ = st.columns([1, 1, 3])
        with col_gen:
            if st.button("🧠 Generate meta_protocol.md", type="primary"):
                _generate_meta_protocol(repo_root)
        with col_snap:
            if st.button("🔍 Preview snapshot"):
                with st.spinner("Scanning..."):
                    snap = snapshot_codebase(repo_root)
                    st.code(snap.tree())

    # Step 1 — review and launch
    elif st.session_state.evo_wizard_step == 1:
        st.markdown("### 📄 Generated `meta_protocol.md`")
        st.text_area("evo_proto_edit", key="evo_meta_protocol_md", height=340, label_visibility="collapsed")
        
        col_a, col_b, _ = st.columns([1, 1, 2])
        with col_a:
            if st.button("🚀 Launch Evolution", type="primary"):
                job_id = _enqueue_evo_job(repo_root)
                st.query_params["evo_job_id"] = job_id
                st.rerun()
        with col_b:
            if st.button("🔄 Regenerate"):
                st.session_state.evo_wizard_step = 0
                st.rerun()

def _generate_meta_protocol(repo_root: Path):
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

def _enqueue_evo_job(repo_root: Path) -> str:
    save_settings()
    apply_api_config()
    proto_path = write_meta_protocol(st.session_state.evo_meta_protocol_md, repo_root)
    
    config = SupervisorConfig(
        protocol_path=proto_path,
        workspace=repo_root,
        max_retries=int(st.session_state.max_retries),
        context_threshold=st.session_state.context_threshold / 100.0,
        opencode_model=st.session_state.opencode_model or None,
        opencode_executable=st.session_state.opencode_executable,
        supervisor_model=st.session_state.supervisor_model,
        timeout=int(st.session_state.timeout) * 60,
        protected_files=tuple(st.session_state.get("protected_files", [])),
        max_tokens=int(st.session_state.max_tokens),
    )
    return job_manager.enqueue_job("evolve", config)

def _show_evo_status_screen(job_id: str):
    status = job_manager.get_job_status(job_id)
    if not status:
        st.error(f"Job {job_id} not found.")
        if st.button("Back to Setup"):
            del st.query_params["evo_job_id"]
            st.rerun()
        return

    state = status["state"]
    
    col_h1, col_h2, col_h3 = st.columns([3, 1, 1])
    with col_h1:
        st.markdown(f"### Evolution Job: `{job_id}`")
    with col_h2:
        if state == "RUNNING":
            if st.button("⏹ Stop", use_container_width=True):
                job_manager.cancel_job(job_id)
                st.rerun()
        else:
            if st.button("🗑 Clear", use_container_width=True):
                del st.query_params["evo_job_id"]
                st.rerun()
    with col_h3:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()

    if state == "RUNNING":
        st.info("🧬 Evolution in progress...")
        time.sleep(2)
        st.rerun()

    col_main, col_side = st.columns([2, 1])
    with col_main:
        _render_step_progress(status.get("logs", []), state, is_evolution=True)
        st.markdown("#### 🖥️ Evolution Log")
        _render_events(status.get("logs", []), "— waiting for logs —", show_verbose=True)
        
    with col_side:
        st.markdown("#### ℹ️ Details")
        st.markdown(f"**State:** {state}")
        st.markdown(f"**Started:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(status.get('updated_at', 0)))}")
        
        _render_token_usage_bar(status.get("logs", []), int(st.session_state.max_tokens))

        if status.get("report"):
            st.markdown("#### 📊 Evolution Report")
            with st.expander("View Report", expanded=True):
                st.markdown(status["report"])
                st.download_button("⬇ Download", data=status["report"], file_name=f"evo_report_{job_id}.md")

# Router
# ═══════════════════════════════════════════════════════════════════════════ #

page = st.session_state.page
if page == "report":
    page = "run"
    st.session_state.page = "run"
_tests_ok = (
    st.session_state.opencode_test_passed and st.session_state.supervisor_test_passed
)
if page == "wizard":
    page_wizard()
elif page == "run":
    if not _tests_ok:
        st.warning(
            "🔒 Live Run is locked. Pass connectivity tests on the Protocol Wizard page first."
        )
        page_wizard()
    else:
        page_run()
elif page == "evolve":
    if not _tests_ok:
        st.warning(
            "🔒 Self-Evolution is locked. Pass connectivity tests on the Protocol Wizard page first."
        )
        page_wizard()
    else:
        page_evolve()
