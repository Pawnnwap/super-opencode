from __future__ import annotations

import streamlit as st

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
.log-info { color: #8b949e; }
.log-warn { color: #e3b341; }
.log-error { color: #f85149; }
.log-success { color: #3fb950; font-weight: 600; }
.log-opencode_prompt, .log-opencode_output, .log-supervisor_response,
.log-supervisor_read_files, .log-step, .log-phase_transition,
.log-step_progress, .log-heartbeat, .log-supervisor_suggestions,
.log-log-plan_phase { white-space: pre-wrap; }
.log-opencode_prompt { color: #79c0ff; }
.log-opencode_output { color: #c9d1d9; }
.log-supervisor_response, .log-supervisor_suggestions { color: #d2a8ff; }
.log-supervisor_read_files, .log-step_progress { color: #a5d6ff; }
.log-step, .log-heartbeat { color: #39d353; font-weight: 600; }
.log-phase_transition { color: #f0883e; font-weight: 600; }
.log-log-plan_phase { color: #79c0ff; font-style: italic; }
.log-rule { color: #21262d; display:block; }
.log-block-hdr { color: #58a6ff; font-weight: 600; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.5px; }
div[data-testid="stProgress"] > div > div { background-color: #21262d !important; }
div[data-testid="stProgress"] > div > div > div { background: linear-gradient(90deg, #1f6feb, #58a6ff) !important; }
.card { background: #161b22; border: 1px solid #21262d; border-radius: 10px; padding: 1.2rem 1.5rem; margin-bottom: 1rem; }
.pill { display: inline-block; padding: 2px 12px; border-radius: 999px; font-size: 0.75rem; font-family: 'JetBrains Mono', monospace; font-weight: 600; margin-left: 8px; }
.pill-idle { background:#21262d; color:#8b949e; }
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


def apply_page_shell() -> None:
    st.set_page_config(
        page_title="opencode Supervisor",
        page_icon="🤻",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
