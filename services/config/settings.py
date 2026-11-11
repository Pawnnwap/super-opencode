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
    "log_detail",
    "max_parallel_jobs",
    "max_stored_jobs",
    "harvest_prompt",
    "harvest_dir",
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
    "enable_jev_decisions",
    "jev_decision_url",
    "jev_decision_api_key",
    "jev_decision_timeout",
    "jev_decision_threshold",
    "enable_blocked_rescue",
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
    "supervisor_model": "swift-1.5",
    "supervisor_model_backup": "swift-1.5",
    "supervisor_reasoning": "",
    "agent_reasoning": "",
    "engine": "opencode",
    "codex_base_url": "",
    "codex_api_key": "",
    "opencode_model": "swift-1.5",
    "opencode_model_backup": "swift-1.5",
    "opencode_executable": "",
    "max_retries": 3,
    "context_threshold": 60,
    "max_tokens": 150000,
    "timeout": 120,
    "plan_mode_rounds": 1,
    # "headline" = one key line per event, full bodies in the job sidecar;
    # "full" = verbatim events (debugging).
    "log_detail": "headline",
    # Upper bound on concurrently RUNNING jobs; extras queue as PENDING.
    "max_parallel_jobs": 4,
    # Terminal jobs beyond this count lose their log payloads (.logs/.full)
    # on the next enqueue; state summaries stay for the board history.
    "max_stored_jobs": 50,
    # Harvester: last operator prompt and output parent. Empty prompt means
    # the built-in default; empty dir follows the configured workspace.
    "harvest_prompt": "",
    "harvest_dir": "",
    "protected_files": [],
    "evo_goal": "",
    "evo_extra_restrictions": "",
    "enable_python_scanner": True,
    "enable_occam_razor": False,
    "enable_supervisor_skills": True,
    "supervisor_skills_dir": "",
    "npm_registry": "https://registry.npmmirror.com",
    # Structured decision service (jev): opt-in arbiter for the closing
    # decision. Disabled by default; the URL is pre-filled with the local
    # deployment used by this machine.
    "enable_jev_decisions": False,
    "jev_decision_url": "http://192.168.18.65/systemone/api/v1/systemone",
    "jev_decision_api_key": "",
    "jev_decision_timeout": 20,
    "jev_decision_threshold": 0.80,
    # First blocked verdict: force a web-researched recovery plan and let jev
    # rule on it before closing. Only active when the jev arbiter is enabled.
    "enable_blocked_rescue": True,
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


def sanitize_proxy_env() -> None:
    """Normalize bracketed IPv6 entries in NO_PROXY.

    httpx2 2.13.0 (pulled in by openai) turns every NO_PROXY entry into a
    URLPattern mount. A bracketed entry such as ``[::1]`` is not recognized as
    an IPv6 host, so it takes the wildcard branch (``all://*[::1]``) and URL
    parsing raises ``InvalidURL: Invalid port: ':1]'`` while the first client
    is constructed - every job fails instantly at startup. Brackets are
    optional in NO_PROXY; strip them once per process, before any client
    exists, keeping subnets (``[::1]/128``) intact.
    """
    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        entries: list[str] = []
        changed = False
        for part in raw.split(","):
            host = part.strip()
            if not host:
                continue
            addr, sep, subnet = host.partition("/")
            if addr.startswith("[") and addr.endswith("]"):
                addr = addr[1:-1]
                host = addr + sep + subnet
                changed = True
            entries.append(host)
        if changed:
            os.environ[key] = ",".join(entries)
