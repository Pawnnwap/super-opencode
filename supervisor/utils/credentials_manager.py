"""supervisor/credentials_manager.py — Secure credential caching with encryption."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

_ENV_FILE = Path.home() / ".opencode_supervisor" / "credentials.env"
_KEY_FILE = Path.home() / ".opencode_supervisor" / ".key"


def _get_key() -> bytes:
    """Load or generate encryption key."""
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KEY_FILE.write_bytes(key)
    return key


_cipher = Fernet(_get_key())


def save_credentials(api_key: str, base_url: str = "") -> None:
    """Encrypt and save credentials to .env file."""
    _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)

    encrypted_key = _cipher.encrypt(api_key.encode()).decode()
    encrypted_url = _cipher.encrypt(base_url.encode()).decode() if base_url else ""

    lines = [f"OPENAI_API_KEY={encrypted_key}"]
    if base_url:
        lines.append(f"OPENAI_BASE_URL={encrypted_url}")

    _ENV_FILE.write_text("\n".join(lines), encoding="utf-8")


def load_credentials() -> tuple[Optional[str], Optional[str]]:
    """Load and decrypt credentials from .env file."""
    if not _ENV_FILE.exists():
        return None, None

    try:
        content = _ENV_FILE.read_text(encoding="utf-8")
        api_key = None
        base_url = None

        for line in content.splitlines():
            if line.startswith("OPENAI_API_KEY="):
                encrypted = line.split("=", 1)[1].strip()
                if encrypted:
                    api_key = _cipher.decrypt(encrypted.encode()).decode()
            elif line.startswith("OPENAI_BASE_URL="):
                encrypted = line.split("=", 1)[1].strip()
                if encrypted:
                    base_url = _cipher.decrypt(encrypted.encode()).decode()

        return api_key or None, base_url or None
    except Exception:
        return None, None


def clear_credentials() -> None:
    """Remove cached credentials."""
    if _ENV_FILE.exists():
        _ENV_FILE.unlink()


def has_cached_credentials() -> bool:
    """Check if credentials exist in .env file."""
    return _ENV_FILE.exists()


def get_credentials_from_environment() -> tuple[Optional[str], Optional[str]]:
    """Get credentials from environment variables (highest priority)."""
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")
    return api_key, base_url


def validate_credentials(api_key: str, base_url: str = "") -> bool:
    """Validate credentials by making a test API call."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    if base_url:
        client.base_url = base_url.rstrip("/") + "/"

    try:
        client.models.list()
        return True
    except Exception:
        return False


def get_credentials() -> tuple[Optional[str], Optional[str]]:
    """
    Get credentials with priority:
    1. Environment variables
    2. Cached .env file (if valid)
    """
    env_key, env_url = get_credentials_from_environment()
    if env_key:
        return env_key, env_url

    cached_key, cached_url = load_credentials()
    if cached_key:
        if validate_credentials(cached_key, cached_url or ""):
            return cached_key, cached_url
        else:
            clear_credentials()

    return None, None
