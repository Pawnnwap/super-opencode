"""Process-wide application state for the NiceGUI UI.

Settings values live in the persisted JSON file (see
``services.config.settings``); this module wraps them in a small singleton
plus runtime-only flags that used to be Streamlit session state.
"""

from __future__ import annotations

import threading

from services.config.settings import (
    DEFAULTS,
    apply_api_config,
    load_settings,
    save_settings,
)


class AppState:
    """Singleton holder for settings values + runtime flags."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.values: dict = dict(DEFAULTS)
        self.values.update(
            {k: v for k, v in load_settings().items() if k in self.values},
        )
        # Runtime flags (not persisted): connectivity test results and the
        # fetched model list. Process-wide — this is a single-user local tool.
        self.flags: dict = {
            "opencode_test_passed": False,
            "supervisor_test_passed": False,
        }
        self.opencode_models: list[str] = []

    # -- mapping-style access to settings values ------------------------- #

    def __getitem__(self, key: str):
        return self.values.get(key)

    def __setitem__(self, key: str, value) -> None:
        with self._lock:
            self.values[key] = value

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def as_mapping(self) -> dict:
        """Mapping view for build_supervisor_config / save_settings."""
        return self.values

    def save(self) -> None:
        with self._lock:
            save_settings(self.values)

    def apply_api_config(self) -> None:
        with self._lock:
            apply_api_config(self.values)

    def tests_ok(self) -> bool:
        return bool(
            self.flags.get("opencode_test_passed"),
        ) and bool(self.flags.get("supervisor_test_passed"))


app_state = AppState()
