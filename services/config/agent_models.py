"""Engine-matched agent model listing for the UI.

The agent-model droplist must list the models of the *selected* engine:
opencode models come from opencode's own CLI listing, codex models from the
configured OpenAI-compatible endpoint (`GET {base_url}/models`). Codex
without an external base_url keeps whatever model its own ~/.codex config
selects, so the UI falls back to free-text entry there.
"""

from __future__ import annotations

import json
import urllib.request

from services.config.opencode_config import fetch_opencode_models


def fetch_codex_models(base_url: str, api_key: str, timeout: int = 10) -> list[str]:
    """Model ids from an OpenAI-compatible endpoint (`{base_url}/models`)."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return []
    req = urllib.request.Request(f"{base}/models")
    if (api_key or "").strip():
        req.add_header("Authorization", f"Bearer {api_key.strip()}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 — listing is best-effort; UI falls back to text
        return []
    entries = payload.get("data") if isinstance(payload, dict) else payload
    ids = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("id"):
            ids.append(str(entry["id"]))
        elif isinstance(entry, str):
            ids.append(entry)
    return sorted(set(ids))


def fetch_agent_models(
    engine: str,
    opencode_executable: str = "",
    codex_base_url: str = "",
    codex_api_key: str = "",
) -> list[str]:
    """Model list for the given execution engine (may be empty)."""
    if (engine or "opencode").strip().lower() == "codex":
        return fetch_codex_models(codex_base_url, codex_api_key)
    return fetch_opencode_models(opencode_executable)
