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
        # Debounced settings writes: one timer at a time, replaced on every
        # edit (see schedule_save).
        self._save_lock = threading.Lock()
        self._save_timer: threading.Timer | None = None
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
        # Engine the cached model list belongs to (models are re-fetched when
        # the droplist engine differs).
        self.model_list_engine: str = ""

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

    def schedule_save(self, delay: float = 0.4) -> None:
        """Coalesce a burst of edits into a single write, off the event loop.

        Every bound field used to call :meth:`save` on each keystroke: typing
        a 40-character API key meant 40 full JSON writes, and the same pattern
        already produced a frozen loop on 2026-11-13. The timer writes from its
        own thread, so a slow disk can never stall a websocket tick.
        """
        with self._save_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            timer = threading.Timer(delay, self._flush_scheduled)
            timer.daemon = True
            self._save_timer = timer
        timer.start()

    def _flush_scheduled(self) -> None:
        with self._save_lock:
            self._save_timer = None
        try:
            self.save()
        except Exception:  # noqa: BLE001 — settings are best-effort
            pass

    def flush(self) -> None:
        """Write any pending edit immediately (launch, blur, shutdown).

        Callers that need the file on disk before acting must use this rather
        than :meth:`save`: ``save`` cannot see an edit still sitting in the
        debounce window.
        """
        with self._save_lock:
            timer = self._save_timer
            self._save_timer = None
        if timer is not None:
            timer.cancel()
        self.save()

    def apply_api_config(self) -> None:
        with self._lock:
            apply_api_config(self.values)

    def tests_ok(self) -> bool:
        """Both probes passed — now, or on a still-valid recorded run.

        The in-process flags win (a fresh pass is immediate truth). The
        fallback keeps a restart from re-locking a machine that was already
        proven, as long as nothing the probe depended on changed.
        """
        if bool(self.flags.get("opencode_test_passed")) and bool(
            self.flags.get("supervisor_test_passed"),
        ):
            return True
        from services.webui import probes

        return probes.fresh(self.values)


app_state = AppState()
