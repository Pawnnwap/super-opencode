"""Shared test isolation for per-user cryptographic state."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_secret_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_OPENCODE_KEY_FILE", str(tmp_path / "secret.key"))
