import json
import os
from pathlib import Path

import streamlit as st

_SETTINGS_FILE = Path(
    os.path.join(str(Path.home()), ".opencode_supervisor_settings.json"),
)

_PERSIST_KEYS = [
    "openai_key",
    "base_url",
    "workspace",
    "supervisor_model",
    "opencode_model",
    "supervisor_model_backup",
    "opencode_model_backup",
    "opencode_executable",
    "max_retries",
    "context_threshold",
    "max_tokens",
    "timeout",
    "plan_mode_rounds",
    "raw_input",
    "raw_target",
    "raw_restrictions",
    "evo_goal",
    "evo_extra_restrictions",
    "enable_python_scanner",
    "enable_occam_razor",
]


def load_settings() -> dict:
    """Load persisted settings from disk. Returns {} if file missing or corrupt."""
    try:
        if _SETTINGS_FILE.exists():
            return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_settings() -> None:
    """Write current session_state values for persisted keys to disk."""
    data = {k: st.session_state.get(k, "") for k in _PERSIST_KEYS}
    try:
        _SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def apply_api_config():
    """Push API key and optional base URL into the environment for the SDK."""
    os.environ["OPENAI_API_KEY"] = st.session_state.openai_key or "none"
    if st.session_state.base_url.strip():
        os.environ["OPENAI_BASE_URL"] = st.session_state.base_url.strip()
    elif "OPENAI_BASE_URL" in os.environ:
        del os.environ["OPENAI_BASE_URL"]
