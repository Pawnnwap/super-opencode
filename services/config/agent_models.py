"""Engine-matched agent model listing for the UI.

The agent-model droplist must list the models of the *selected* engine:
opencode models come from opencode's own CLI listing, codex models from the
configured OpenAI-compatible endpoint (`GET {base_url}/models`), and pi
models from `pi --list-models` ("provider/model" ids, exactly what
`pi --model` accepts). Codex without an external base_url keeps whatever
model its own ~/.codex config selects, so the UI falls back to free-text
entry there.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

from services.config.opencode_config import fetch_opencode_models
from supervisor.runners.pi_runner import find_pi


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


def fetch_pi_models(exe: str = "", timeout: int = 15) -> list[str]:
    """`pi --list-models` rows as "provider/model" ids.

    The table's first two whitespace-separated columns are provider and
    model (model ids contain no spaces), and `pi --model` accepts exactly
    the "provider/model" spelling. Unparseable output yields [] and the UI
    falls back to free-text entry.
    """
    try:
        resolved_exe = find_pi(exe)
    except FileNotFoundError:
        return []

    home_dir = str(Path.home())
    try:
        proc = subprocess.run(
            [resolved_exe, "--list-models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=home_dir,
            check=False,
            shell=(
                sys.platform == "win32"
                and resolved_exe.lower().endswith((".cmd", ".bat", ".ps1"))
            ),
        )
        if proc.returncode != 0:
            return []
        models: list[str] = []
        for line in proc.stdout.splitlines():
            parts = line.split()
            # Header row starts with "provider"; data rows are
            # "<provider> <model> <context> ..." with model ids containing
            # no spaces.
            if len(parts) < 2 or parts[0].lower() == "provider":
                continue
            models.append(f"{parts[0]}/{parts[1]}")
        return sorted(set(models))
    except Exception:  # noqa: BLE001 — listing is best-effort; UI falls back to text
        return []


def fetch_agent_models(
    engine: str,
    opencode_executable: str = "",
    codex_base_url: str = "",
    codex_api_key: str = "",
) -> list[str]:
    """Model list for the given execution engine (may be empty)."""
    engine = (engine or "opencode").strip().lower()
    if engine == "codex":
        return fetch_codex_models(codex_base_url, codex_api_key)
    if engine == "pi":
        return fetch_pi_models(opencode_executable)
    return fetch_opencode_models(opencode_executable)
