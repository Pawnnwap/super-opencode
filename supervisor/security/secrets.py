"""Authenticated encryption for credentials persisted on disk."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

SECRET_KEYS = frozenset(
    {
        "openai_key",
        "openai_api_key",
        "codex_api_key",
        "jev_decision_api_key",
    }
)
_LOCK = threading.Lock()
_FERNET: Fernet | None = None
_KEY_PATH: Path | None = None


def _key_path() -> Path:
    override = os.environ.get("SUPER_OPENCODE_KEY_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".super-opencode" / "secret.key"


def _fernet() -> Fernet:
    global _FERNET, _KEY_PATH
    path = _key_path()
    with _LOCK:
        if _FERNET is not None and _KEY_PATH == path:
            return _FERNET
        try:
            key = path.read_text(encoding="utf-8").strip().encode()
        except OSError:
            key = Fernet.generate_key()
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(key)
                        handle.flush()
                        os.fsync(handle.fileno())
                except Exception:
                    path.unlink(missing_ok=True)
                    raise
            except FileExistsError:
                key = path.read_text(encoding="utf-8").strip().encode()
        _FERNET = Fernet(key)
        _KEY_PATH = path
        return _FERNET


def protect_secrets(values: dict) -> dict:
    protected = dict(values)
    if not any(protected.get(key) for key in SECRET_KEYS):
        return protected
    cipher = _fernet()
    for key in SECRET_KEYS:
        value = protected.pop(key, "")
        if value:
            protected[f"{key}_enc"] = cipher.encrypt(str(value).encode()).decode()
    return protected


def decrypt_secrets(values: dict) -> dict:
    restored = dict(values)
    if not any(restored.get(f"{key}_enc") for key in SECRET_KEYS):
        return restored
    cipher = _fernet()
    for key in SECRET_KEYS:
        encrypted = restored.pop(f"{key}_enc", "")
        if encrypted:
            restored[key] = cipher.decrypt(encrypted.encode()).decode()
    return restored


def seal_json(value: Any) -> str:
    return _fernet().encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()


def open_json(text: str) -> Any:
    return json.loads(_fernet().decrypt(text.encode()).decode())
