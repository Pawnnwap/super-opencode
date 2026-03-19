"""
app.py  —  opencode Supervisor UI
Run with:  streamlit run app.py
"""

from __future__ import annotations

import threading
import time
from pathlib import Path


# ── supervisor package imports (all at top level — never lazy) ──────────── #
from supervisor.config import SupervisorConfig
from supervisor.protocol_wizard import ProtocolWizard
from supervisor.loop import SupervisorLoop
from supervisor.codebase_analyzer import snapshot_codebase
from supervisor.meta_protocol_builder import MetaProtocolBuilder, write_meta_protocol
from supervisor.self_evolution_loop import SelfEvolutionLoop

import streamlit as st

# ── page config ──────────────────────────────────────────────────────────── #
st.set_page_config(
    page_title="opencode Supervisor",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── custom CSS  (dark terminal aesthetic) ────────────────────────────────── #
st.markdown(
    """
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

div[data-testid="stExpander"] {
    border: 1px solid #21262d !important;
    background: #161b22 !important;
    border-radius: 8px !important;
}
</style>
""",
    unsafe_allow_html=True,
)

# ── session state defaults ────────────────────────────────────────────────── #

import json
import os

# ── Settings persistence ──────────────────────────────────────────────────── #
_SETTINGS_FILE = Path.home() / ".opencode_supervisor_settings.json"

# Keys that are persisted to disk (excludes runtime state and secrets in plaintext
# — API key is stored because the user explicitly enters it here;
#   they can clear it by deleting the settings file)
_PERSIST_KEYS = [
    "openai_key",
    "base_url",
    "workspace",
    "supervisor_model",
    "opencode_model",
    "opencode_executable",
    "max_retries",
    "context_threshold",
    "timeout",
    "raw_input",
    "raw_target",
    "raw_restrictions",
    "evo_goal",
    "evo_extra_restrictions",
]


def _load_settings() -> dict:
    """Load persisted settings from disk. Returns {} if file missing or corrupt."""
    try:
        if _SETTINGS_FILE.exists():
            return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_settings() -> None:
    """Write current session_state values for persisted keys to disk."""
    data = {k: st.session_state.get(k, "") for k in _PERSIST_KEYS}
    try:
        _SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_opencode_path() -> str:
    """Auto-load the opencode executable path written by diagnose_opencode.py."""
    p = Path(__file__).parent / ".opencode_path"
    if p.exists():
        val = p.read_text(encoding="utf-8").strip()
        if val:
            return val
    return ""


# Load persisted settings first — used as defaults below
_persisted = _load_settings()

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
    "opencode_executable": _load_opencode_path(),
    "max_retries": 3,
    "context_threshold": 60,
    "timeout": 5,
    # self-evolution page
    "evo_goal": "",
    "evo_extra_restrictions": "",
    "evo_meta_protocol_md": "",
    "evo_log_events": [],
    "evo_run_state": "idle",
    "evo_report": "",
    "evo_wizard_step": 0,
    "verbose_log": True,
}
for k, v in defaults.items():
    if k not in st.session_state:
        # Use persisted value if available, else default
        st.session_state[k] = _persisted.get(k, v)

# ── sidebar navigation ────────────────────────────────────────────────────── #
with st.sidebar:
    st.markdown("## 🤖 opencode<br>**Supervisor**", unsafe_allow_html=True)
    st.markdown("---")

    pill_map = {
        "idle": '<span class="pill pill-idle">idle</span>',
        "running": '<span class="pill pill-running">running</span>',
        "success": '<span class="pill pill-success">done ✓</span>',
        "failure": '<span class="pill pill-failure">failed ✗</span>',
    }
    st.markdown(
        f"**Status** {pill_map.get(st.session_state.run_state, '')}",
        unsafe_allow_html=True,
    )
    st.markdown("---")

    pages = {
        "wizard": "① Protocol Wizard",
        "run": "② Live Run",
        "report": "③ Report",
        "evolve": "④ Self-Evolution",
    }
    for key, label in pages.items():
        active = st.session_state.page == key
        if st.button(
            label,
            key=f"nav_{key}",
            use_container_width=True,
            type="primary" if active else "secondary",
        ):
            st.session_state.page = key
            st.rerun()

    evo_state = st.session_state.evo_run_state
    if evo_state != "idle":
        evo_pill = pill_map.get(evo_state, "")
        st.markdown(f"**Self-evo** {evo_pill}", unsafe_allow_html=True)

    st.markdown("---")
    st.caption("openai · streamlit · opencode")


# ═══════════════════════════════════════════════════════════════════════════ #
# PAGE 1 — Protocol Wizard                                                    #
# ═══════════════════════════════════════════════════════════════════════════ #


def _apply_api_config():
    """Push API key and optional base URL into the environment for the SDK."""
    import os

    os.environ["OPENAI_API_KEY"] = st.session_state.openai_key or "none"
    if st.session_state.base_url.strip():
        os.environ["OPENAI_BASE_URL"] = st.session_state.base_url.strip()
    elif "OPENAI_BASE_URL" in os.environ:
        del os.environ["OPENAI_BASE_URL"]


def page_wizard():
    st.markdown("# Protocol Wizard")
    st.markdown(
        "Fill in each section in plain language. The supervisor LLM will refine "
        "them into a clean, unambiguous `protocol.md`."
    )

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
            st.session_state.supervisor_model = st.text_input(
                "Supervisor / wizard model",
                key="cfg_supervisor_model",
                value=st.session_state.supervisor_model,
                placeholder="e.g. gpt-4o, claude-3-5-sonnet, mistral-large",
            )
        with col2:
            st.session_state.opencode_model = st.text_input(
                "opencode model (leave blank = opencode default)",
                key="cfg_opencode_model",
                value=st.session_state.opencode_model,
            )
            st.session_state.opencode_executable = st.text_input(
                "opencode executable (leave blank to auto-detect)",
                key="cfg_opencode_exe",
                value=str(st.session_state.opencode_executable),
                placeholder=r"e.g. C:\Users\you\AppData\Roaming\npm\opencode.cmd",
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
            st.session_state.timeout = st.number_input(
                "Timeout (min)",
                key="cfg_timeout",
                min_value=1,
                max_value=999,
                value=min(max(int(st.session_state.timeout) // 60, 1), 999),
            )
    # Auto-save settings to disk whenever the config panel is shown
    _save_settings()

    st.markdown("---")

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
    st.session_state.raw_input = st.text_area(
        "input_area",
        value=st.session_state.raw_input,
        height=120,
        placeholder="e.g. A Python repo is at ./src. The main entry point is main.py.",
        label_visibility="collapsed",
    )
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown("**TARGET** — concrete, testable deliverables")
    st.session_state.raw_target = st.text_area(
        "target_area",
        value=st.session_state.raw_target,
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
    st.session_state.raw_restrictions = st.text_area(
        "restrictions_area",
        value=st.session_state.raw_restrictions,
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
            import os

            _apply_api_config()

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
            value=st.session_state.protocol_md,
            height=300,
            label_visibility="collapsed",
        )
        st.session_state.protocol_md = edited

        col_a, col_b, _ = st.columns([1, 1, 3])
        with col_a:
            if st.button("✅  Accept & Save", type="primary"):
                _save_protocol()
                st.success("protocol.md saved to workspace.")
        with col_b:
            if st.button("🔄  Re-refine"):
                st.session_state.wizard_step = 0
                st.rerun()


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

    # Pre-flight check
    ready = bool(st.session_state.protocol_md and st.session_state.workspace)
    if not ready:
        st.warning("Complete the Protocol Wizard first.")
        return

    # Sync thread-safe shared dict → session_state BEFORE rendering buttons
    if "_run_shared" in st.session_state:
        sh = st.session_state._run_shared
        st.session_state.log_events = sh["events"]
        if sh["state"] != "running":
            st.session_state.run_state = sh["state"]
            st.session_state.final_report = sh["report"]

    workspace = Path(st.session_state.workspace)
    proto_path = workspace / "protocol.md"

    col1, col2 = st.columns([2, 1])
    with col1:
        st.markdown(
            f"**Workspace:** `{workspace}`  \n"
            f"**Protocol:** `{proto_path}`  \n"
            f"**Supervisor model:** `{st.session_state.supervisor_model}`  \n"
            f"**Max retries:** {st.session_state.max_retries} · "
            f"**Timeout:** {st.session_state.timeout} min · "
            f"**Compaction at:** {st.session_state.context_threshold}%"
        )
    with col2:
        state = st.session_state.run_state
        can_start = state in ("idle", "success", "failure")
        can_stop = state == "running"

        if st.button("▶  Start Run", type="primary", disabled=not can_start):
            _start_run()

        if st.button("⏹  Stop", disabled=not can_stop):
            if "_run_stop" in st.session_state:
                st.session_state._run_stop.set()
            st.session_state.run_state = "idle"
            st.warning(
                "Stop requested — the background thread will finish its current step."
            )

    st.markdown("---")
    st.markdown("### 🖥️  Live Log")
    _render_log()

    # Auto-refresh while running
    if st.session_state.run_state == "running":
        time.sleep(0.8)
        st.rerun()


def _start_run():
    _save_settings()
    _apply_api_config()

    workspace = Path(st.session_state.workspace)
    proto_path = workspace / "protocol.md"

    if not proto_path.exists():
        _save_protocol()

    config = SupervisorConfig(
        protocol_path=proto_path,
        workspace=workspace,
        max_retries=int(st.session_state.max_retries),
        context_threshold=st.session_state.context_threshold / 100.0,
        opencode_model=st.session_state.opencode_model or None,
        opencode_executable=st.session_state.opencode_executable,
        supervisor_model=st.session_state.supervisor_model,
        timeout=int(st.session_state.timeout) * 60,
    )

    # Thread-safe shared state — never touch st.session_state from the thread
    shared = {"events": [], "state": "running", "report": ""}
    stop_event = threading.Event()
    st.session_state.log_events = shared["events"]
    st.session_state.run_state = "running"
    st.session_state.final_report = ""
    st.session_state._run_shared = shared
    st.session_state._run_stop = stop_event

    def _worker():
        loop = SupervisorLoop(config)
        for event in loop.run_streaming():
            if stop_event.is_set():
                break
            shared["events"].append(event)

        if any(e["level"] == "success" for e in shared["events"]):
            shared["state"] = "success"
        else:
            shared["state"] = "failure"

        for p in (workspace / "failure_report.md", workspace / "summary.md"):
            if p.exists():
                shared["report"] = p.read_text(encoding="utf-8")
                break

    threading.Thread(target=_worker, daemon=True).start()


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_BLOCK_META = {
    "opencode_prompt": ("hdr-oc-prompt", "▶ PROMPT → opencode"),
    "opencode_output": ("hdr-oc-output", "◀ OUTPUT ← opencode"),
    "supervisor_response": ("hdr-sv-response", "🧠 SUPERVISOR"),
}


def _render_events(events: list[dict], empty_msg: str, skip: set | None = None) -> None:
    skip = skip or set()
    verbose = st.session_state.get("verbose_log", True)

    # Verbose toggle
    st.session_state.verbose_log = st.toggle(
        "Verbose log", value=verbose, key=f"vtoggle_{empty_msg[:8].replace(' ', '_')}"
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


def _render_log():
    _render_events(st.session_state.log_events, "— waiting for run to start —")


def _render_step_progress():
    step_events = [
        e
        for e in st.session_state.log_events
        if e.get("level") in ("step", "phase_transition")
    ]
    progress_events = [
        e for e in st.session_state.log_events if e.get("level") == "step_progress"
    ]
    heartbeat_events = [
        e for e in st.session_state.log_events if e.get("level") == "heartbeat"
    ]

    if progress_events:
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


# ═══════════════════════════════════════════════════════════════════════════ #
# PAGE 3 — Report                                                             #
# ═══════════════════════════════════════════════════════════════════════════ #


def page_report():
    st.markdown("# Final Report")

    state = st.session_state.run_state
    pill_map = {
        "idle": ("🔵", "No run has completed yet."),
        "running": ("🟡", "Run is still in progress."),
        "success": ("🟢", "All targets met — run completed successfully."),
        "failure": ("🔴", "Run ended with failures."),
    }
    icon, label = pill_map.get(state, ("⚪", ""))
    st.markdown(f"### {icon} {label}")

    if state in ("success", "failure") and st.session_state.final_report:
        st.markdown("---")
        st.markdown("#### Supervisor Report")
        st.markdown(
            f'<div class="proto-preview">{st.session_state.final_report}</div>',
            unsafe_allow_html=True,
        )
        st.download_button(
            "⬇  Download report",
            data=st.session_state.final_report,
            file_name="supervisor_report.md",
            mime="text/markdown",
        )

    if st.session_state.protocol_md:
        st.markdown("---")
        st.markdown("#### Protocol used")
        st.markdown(
            f'<div class="proto-preview">{st.session_state.protocol_md}</div>',
            unsafe_allow_html=True,
        )


# ═══════════════════════════════════════════════════════════════════════════ #
# PAGE 4 — Self-Evolution                                                     #
# ═══════════════════════════════════════════════════════════════════════════ #


def page_evolve():
    st.markdown("# ④ Self-Evolution")
    st.markdown(
        "Point the supervisor + opencode at **this codebase itself**. "
        "Describe what you want improved or debugged — the system will "
        "auto-generate a `meta_protocol.md` from the live source tree, "
        "then run the full supervisor loop with checkpointing and rollback."
    )

    if not st.session_state.openai_key:
        st.warning(
            "Enter your OpenAI API key in the Protocol Wizard config panel first."
        )
        return

    # Sync thread-safe shared dict → session_state BEFORE rendering buttons
    if "_evo_shared" in st.session_state:
        sh = st.session_state._evo_shared
        st.session_state.evo_log_events = sh["events"]
        if sh["state"] != "running":
            st.session_state.evo_run_state = sh["state"]
            st.session_state.evo_report = sh["report"]

    # ── infer repo root (where app.py lives) ─────────────────────────── #
    import os

    repo_root = Path(__file__).parent.resolve()
    st.info(f"**Repo root (workspace):** `{repo_root}`")

    st.markdown("---")

    # ── existing meta_protocol.md detection ──────────────────────────────── #
    existing_meta = repo_root / "meta_protocol.md"
    if existing_meta.exists() and st.session_state.evo_wizard_step == 0:
        existing_meta_text = existing_meta.read_text(encoding="utf-8")
        st.info("📄 An existing `meta_protocol.md` was found in the repo.")
        col_rm, col_rn, _ = st.columns([1, 1, 3])
        with col_rm:
            if st.button(
                "♻️  Use existing meta_protocol.md", type="primary", key="btn_reuse_meta"
            ):
                st.session_state.evo_meta_protocol_md = existing_meta_text
                st.session_state.evo_wizard_step = 1
                st.rerun()
        with col_rn:
            if st.button("✏️  Generate new one", key="btn_regen_meta"):
                pass  # fall through to the form
        with st.expander("Preview existing meta_protocol.md"):
            st.code(existing_meta_text[:1500], language="markdown")
        st.markdown("---")

    # ─────────────────────────────────────────────────────────────────── #
    # Step 0 — define the evolution goal                                  #
    # ─────────────────────────────────────────────────────────────────── #
    if st.session_state.evo_wizard_step == 0:
        st.markdown("### 🎯 What do you want to evolve?")

        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown(
            "**Evolution goal** — describe the improvement, bug to fix, or feature to add"
        )
        st.session_state.evo_goal = st.text_area(
            "evo_goal_input",
            value=st.session_state.evo_goal,
            height=130,
            placeholder=(
                "e.g.\n"
                "Fix the context-estimation in opencode_runner.py — it currently uses a "
                "char/token ratio which is too rough. Replace it with tiktoken.\n\n"
                "Also add a proper logging handler so all supervisor events are written "
                "to evolution.log in the workspace."
            ),
            label_visibility="collapsed",
        )
        st.markdown("</div>", unsafe_allow_html=True)

        st.markdown('<div class="card">', unsafe_allow_html=True)
        st.markdown(
            "**Extra restrictions** *(optional)* — anything the agent must not touch"
        )
        st.session_state.evo_extra_restrictions = st.text_area(
            "evo_restrictions_input",
            value=st.session_state.evo_extra_restrictions,
            height=80,
            placeholder="e.g. Do not change the Streamlit UI layout. Do not add new dependencies.",
            label_visibility="collapsed",
        )
        st.markdown("</div>", unsafe_allow_html=True)

        col_gen, col_snap, _ = st.columns([1, 1, 3])
        with col_gen:
            gen_clicked = st.button("🧠  Generate meta_protocol.md", type="primary")
        with col_snap:
            snap_clicked = st.button("🔍  Preview codebase snapshot")

        if snap_clicked:
            with st.spinner("Scanning codebase…"):
                snap = snapshot_codebase(repo_root)
            st.markdown(f"**{len(snap.files)} files found**")
            with st.expander("File tree"):
                st.code(snap.tree())

        if gen_clicked:
            if not st.session_state.evo_goal.strip():
                st.error("Please describe your evolution goal.")
            else:
                import os

                _apply_api_config()

                with st.spinner("Scanning codebase and generating meta_protocol.md…"):
                    snap = snapshot_codebase(repo_root)
                    builder = MetaProtocolBuilder(
                        model=st.session_state.supervisor_model
                    )
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

    # ─────────────────────────────────────────────────────────────────── #
    # Step 1 — review meta_protocol + launch                             #
    # ─────────────────────────────────────────────────────────────────── #
    elif st.session_state.evo_wizard_step == 1:
        st.markdown("### 📄 Generated `meta_protocol.md`")
        st.caption("Review and edit, then click Launch.")

        edited = st.text_area(
            "evo_proto_edit",
            value=st.session_state.evo_meta_protocol_md,
            height=340,
            label_visibility="collapsed",
        )
        st.session_state.evo_meta_protocol_md = edited

        col_a, col_b, col_c, _ = st.columns([1, 1, 1, 2])
        with col_a:
            launch = st.button(
                "🚀  Launch Evolution",
                type="primary",
                disabled=st.session_state.evo_run_state == "running",
            )
        with col_b:
            if st.button("🔄  Regenerate"):
                st.session_state.evo_wizard_step = 0
                st.rerun()
        with col_c:
            if st.button(
                "⏹  Stop", disabled=st.session_state.evo_run_state != "running"
            ):
                if "_evo_stop" in st.session_state:
                    st.session_state._evo_stop.set()
                st.session_state.evo_run_state = "idle"

        if launch:
            _start_evolution(repo_root)

        # ── live log ─────────────────────────────────────────────────── #
        st.markdown("---")
        st.markdown("### 🖥️  Evolution Log")
        _render_evo_log()
        _render_evo_step_progress()

        if st.session_state.evo_run_state == "running":
            time.sleep(0.8)
            st.rerun()

        # ── report when done ─────────────────────────────────────────── #
        if (
            st.session_state.evo_run_state in ("success", "failure")
            and st.session_state.evo_report
        ):
            st.markdown("---")
            st.markdown("### 📊 Evolution Report")
            st.markdown(
                f'<div class="proto-preview">{st.session_state.evo_report}</div>',
                unsafe_allow_html=True,
            )
            st.download_button(
                "⬇  Download evolution_report.md",
                data=st.session_state.evo_report,
                file_name="evolution_report.md",
                mime="text/markdown",
            )

            col_r, _ = st.columns([1, 3])
            with col_r:
                if st.button("🔁  New Evolution Run"):
                    st.session_state.evo_wizard_step = 0
                    st.session_state.evo_run_state = "idle"
                    st.session_state.evo_log_events = []
                    st.session_state.evo_report = ""
                    st.rerun()


def _start_evolution(repo_root: Path):
    _save_settings()
    _apply_api_config()

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
    )

    # Thread-safe shared state — never touch st.session_state from the thread
    shared = {"events": [], "state": "running", "report": ""}
    stop_event = threading.Event()
    st.session_state.evo_log_events = shared["events"]
    st.session_state.evo_run_state = "running"
    st.session_state.evo_report = ""
    st.session_state._evo_shared = shared
    st.session_state._evo_stop = stop_event

    def _worker():
        loop = SelfEvolutionLoop(config)
        for event in loop.run_streaming():
            if stop_event.is_set():
                break
            shared["events"].append(event)
            if event.get("level") == "report":
                shared["report"] = event["msg"]

        if any(e["level"] == "success" for e in shared["events"]):
            shared["state"] = "success"
        else:
            shared["state"] = "failure"

        if not shared["report"]:
            rp = repo_root / "evolution_report.md"
            if rp.exists():
                shared["report"] = rp.read_text(encoding="utf-8")

    threading.Thread(target=_worker, daemon=True).start()


def _render_evo_log():
    state = st.session_state.evo_run_state
    placeholder = (
        "— waiting for evolution to start —" if state == "idle" else "— starting… —"
    )
    _render_events(st.session_state.evo_log_events, placeholder, skip={"report"})
    cp_events = [
        e
        for e in st.session_state.evo_log_events
        if "checkpoint saved" in e.get("msg", "").lower()
    ]
    if cp_events:
        st.caption(f"💾 {len(cp_events)} checkpoint(s) saved so far")


def _render_evo_step_progress():
    step_events = [
        e
        for e in st.session_state.evo_log_events
        if e.get("level") in ("step", "phase_transition")
    ]
    progress_events = [
        e for e in st.session_state.evo_log_events if e.get("level") == "step_progress"
    ]

    if progress_events:
        last_progress = progress_events[-1]
        msg = last_progress.get("msg", "")

        progress_col1, progress_col2 = st.columns([3, 1])
        with progress_col1:
            st.caption(f"📊 {msg}")
        with progress_col2:
            step_count = len(step_events)
            st.caption(f"🧭 {step_count} step(s) detected")

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
                for ev in step_events[-8:]:
                    lvl = ev.get("level", "")
                    if lvl == "step":
                        st.caption(f"• {ev.get('msg', '')[:80]}")
                    elif lvl == "phase_transition":
                        st.caption(f"⚡ {ev.get('msg', '')}")


# ═══════════════════════════════════════════════════════════════════════════ #
# Router                                                                      #
# ═══════════════════════════════════════════════════════════════════════════ #

page = st.session_state.page
if page == "wizard":
    page_wizard()
elif page == "run":
    page_run()
elif page == "report":
    page_report()
elif page == "evolve":
    page_evolve()
