from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from supervisor.runners.opencode_runner import find_opencode


def find_opencode_config_dir() -> Path:
    config_dir = Path.home() / ".config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def atomic_write_json(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = path.with_suffix(".tmp")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(content, tmp, indent=2)
        tmp_path.replace(path)
    except (PermissionError, OSError):
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _load_config_json(path: Path, on_warning=None) -> dict:
    """Load OpenCode JSON, preserving common JSONC-style trailing commas."""
    text = path.read_text(encoding="utf-8")
    try:
        content = json.loads(text)
    except json.JSONDecodeError as exc:
        normalized = re.sub(r",(\s*[}\]])", r"\1", text)
        if normalized == text:
            raise
        try:
            content = json.loads(normalized)
        except json.JSONDecodeError:
            raise exc
        if on_warning:
            on_warning("Config used trailing commas; normalized during migration.")
    if not isinstance(content, dict):
        raise TypeError(f"Config must be a JSON object, got {type(content).__name__}")
    return content


def _is_legacy_hashline_config(value: object) -> bool:
    """Identify only Hashline servers previously installed by this app."""
    if not isinstance(value, dict) or value.get("type") != "local":
        return False
    command = value.get("command")
    if not isinstance(command, list):
        return False
    return any(
        isinstance(part, str)
        and part.replace("\\", "/").endswith("/mcp_server/hashline.py")
        for part in command
    )


def get_opencode_config_file(
    config_dir: Path,
    project_root: Path,
    on_info=None,
    on_warning=None,
) -> Path:
    target_file = config_dir / "opencode.json"
    old_file = config_dir / "config.json"
    if old_file.exists() and not target_file.exists():
        try:
            old_file.rename(target_file)
        except Exception:
            pass

    target_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        content = _load_config_json(target_file, on_warning=on_warning)
    except FileNotFoundError:
        content = {"$schema": "https://opencode.ai/config.json", "provider": {}}
    except json.JSONDecodeError as exc:
        if on_warning:
            on_warning(f"Config JSON invalid, resetting: {exc}")
        content = {"$schema": "https://opencode.ai/config.json", "provider": {}}
    except PermissionError:
        raise PermissionError(f"Cannot read config: {target_file}")

    dirty = False
    if "mcp" not in content or not isinstance(content["mcp"], dict):
        content["mcp"] = {}
        dirty = True

    python_cmd = sys.executable or "python"
    codehelp_path = str((project_root / "mcp_server" / "codehelp.py").resolve()).replace("\\", "/")

    removed_legacy_hashline = False
    if _is_legacy_hashline_config(content["mcp"].get("hashline")):
        del content["mcp"]["hashline"]
        removed_legacy_hashline = True
        dirty = True

    mcp_configs = {
        "codehelp": {
            "type": "local",
            "command": [python_cmd, codehelp_path],
            "enabled": True,
            "environment": {},
        },
    }

    for key, value in mcp_configs.items():
        if content["mcp"].get(key) != value:
            content["mcp"][key] = value
            dirty = True

    # Hashline previously denied native tools so its line-ID protocol was the
    # only edit path. Restore native tools only when removing that managed MCP
    # entry; preserve permissions deliberately chosen by the user otherwise.
    if removed_legacy_hashline and isinstance(content.get("permission"), dict):
        permissions = dict(content["permission"])
        changed_permissions = False
        for tool in ("read", "edit"):
            if permissions.get(tool) == "deny":
                permissions[tool] = "allow"
                changed_permissions = True
        if changed_permissions:
            content["permission"] = permissions
            dirty = True

    if dirty:
        atomic_write_json(target_file, content)
        if on_info:
            on_info(f"✓ Config updated: {target_file.name}")

    return target_file


def add_custom_provider_to_config(
    config_file: Path,
    service_name: str,
    base_url: str,
    api_key: str,
    model_names: list[str],
) -> None:
    if not service_name.strip():
        raise ValueError("service_name cannot be empty")
    if not base_url:
        raise ValueError("base_url cannot be empty")

    try:
        content = _load_config_json(config_file)
    except FileNotFoundError:
        content = {"$schema": "https://opencode.ai/config.json", "provider": {}}
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
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
    atomic_write_json(config_file, content)


def fetch_opencode_models(exe: str = "") -> list[str]:
    try:
        resolved_exe = find_opencode(exe)
    except FileNotFoundError:
        print(
            "[opencode-models] opencode executable not found — skipping model fetch.",
            file=sys.stderr,
        )
        return []

    home_dir = str(Path.home())
    try:
        proc = subprocess.run(
            [resolved_exe, "models"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=home_dir,
            shell=(
                sys.platform == "win32"
                and resolved_exe.lower().endswith((".cmd", ".bat", ".ps1"))
            ),
        )

        if proc.returncode != 0:
            error_output = proc.stderr.strip() or proc.stdout.strip()
            print(f"[opencode-models] Error: {error_output}", file=sys.stderr)
            return []

        models = []
        for line in proc.stdout.strip().splitlines():
            clean = line.strip()
            if not clean or any(
                clean.startswith(header)
                for header in ("-", "ID", "NAME", "PROMPT", "Error")
            ):
                continue
            models.append(clean.split()[0])

        return models
    except subprocess.TimeoutExpired:
        print("[opencode-models] Request timed out after 15s.", file=sys.stderr)
    except FileNotFoundError:
        print(f"[opencode-models] Executable '{resolved_exe}' not found.", file=sys.stderr)
    except Exception as exc:
        print(f"[opencode-models] Unexpected error: {exc}", file=sys.stderr)

    return []
