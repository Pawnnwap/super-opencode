"""Persisted user settings for the supervisor UI (framework-free).

The settings file (~/.opencode_supervisor_settings.json) is the single source
of truth for configuration values; the web UI reads/writes it through
:mfunc:`load_settings` / :func:`save_settings` and seeds its widgets from
:data:`DEFAULTS`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_SETTINGS_FILE = Path(
    os.path.join(str(Path.home()), ".opencode_supervisor_settings.json"),
)

_PERSIST_KEYS = [
    "openai_key",
    "base_url",
    "workspace",
    "supervisor_model",
    "supervisor_reasoning",
    "agent_reasoning",
    "engine",
    "codex_base_url",
    "codex_api_key",
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
    "enable_supervisor_skills",
    "supervisor_skills_dir",
    "npm_registry",
]

# Default values for every setting the UI binds to (persisted keys above plus
# session-only values kept here so all pages share one source of defaults).
DEFAULTS: dict = {
    "protocol_md": "",
    "raw_input": "",
    "raw_target": "",
    "raw_restrictions": "",
    "openai_key": "",
    "base_url": "",
    "workspace": "",
    "supervisor_model": "",
    "supervisor_model_backup": "",
    "supervisor_reasoning": "",
    "agent_reasoning": "",
    "engine": "opencode",
    "codex_base_url": "",
    "codex_api_key": "",
    "opencode_model": "",
    "opencode_model_backup": "",
    "opencode_executable": "",
    "max_retries": 3,
    "context_threshold": 60,
    "max_tokens": 150000,
    "timeout": 120,
    "plan_mode_rounds": 1,
    "protected_files": [],
    "evo_goal": "",
    "evo_extra_restrictions": "",
    "enable_python_scanner": True,
    "enable_occam_razor": False,
    "enable_supervisor_skills": True,
    "supervisor_skills_dir": "",
    "npm_registry": "https://registry.npmmirror.com",
}


def load_settings() -> dict:
    """Load persisted settings from disk. Returns {} if file missing or corrupt."""
    try:
        if _SETTINGS_FILE.exists():
            return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    return {}


def save_settings(values: dict) -> None:
    """Write the persisted subset of ``values`` to disk."""
    data = {k: values.get(k, "") for k in _PERSIST_KEYS}
    try:
        _SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def apply_api_config(values: dict) -> None:
    """Push API key and optional base URL into the environment for the SDK."""
    os.environ["OPENAI_API_KEY"] = values.get("openai_key") or "none"
    base_url = (values.get("base_url") or "").strip()
    if base_url:
        os.environ["OPENAI_BASE_URL"] = base_url
    elif "OPENAI_BASE_URL" in os.environ:
        del os.environ["OPENAI_BASE_URL"]
