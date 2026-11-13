"""Interaction state that must survive a UI rebuild or restart.

NiceGUI re-creates elements whenever their parent is rebuilt: a
``@ui.refreshable`` section refresh, a board tick that re-renders its cards,
a page reload. A freshly constructed element starts at whatever ``value`` the
caller passed in, so a panel the operator had just opened silently folded
itself shut again - the "the page moved under my hands" class of bug.

The state is persisted as well as held in memory: a page reload (which the
browser does after the client is deleted) and a service restart both build
every panel from scratch, and an in-memory-only store made every panel come
back collapsed. It is a single small JSON file, rewritten atomically on each
toggle; this is a single-operator local tool, so two tabs agreeing is a
feature, not a conflict.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from nicegui import ui

logger = logging.getLogger(__name__)

_PATH = Path(".job_store") / "ui_panels.json"
_EXPANSIONS: dict[str, bool] | None = None


def _load() -> dict[str, bool]:
    try:
        raw = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): bool(v) for k, v in raw.items()}


def state() -> dict[str, bool]:
    """The remembered panel states (loaded lazily, once)."""
    global _EXPANSIONS
    if _EXPANSIONS is None:
        _EXPANSIONS = _load()
    return _EXPANSIONS


def stored_expansion(key: str) -> bool | None:
    """Stored open state of *key*, or ``None`` if never toggled."""
    return state().get(key)


def _persist() -> None:
    """Write the whole map atomically; a failed write must never break the UI."""
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_name(_PATH.name + ".tmp")
        tmp.write_text(json.dumps(state(), indent=1), encoding="utf-8")
        os.replace(tmp, _PATH)
    except OSError as exc:
        logger.warning("cannot persist panel state: %s", exc)


def state_expansion(
    key: str, text: str, *, default: bool = False, **kwargs,
) -> ui.expansion:
    """A ``ui.expansion`` whose open state survives rebuilds and restarts.

    :param key: stable identity of this panel (unique per page/panel)
    :param default: state used only until the operator toggles it once
    """
    known = stored_expansion(key)
    expansion = ui.expansion(
        text,
        value=default if known is None else known,
        **kwargs,
    )

    def _remember(e, k=key) -> None:
        state()[k] = bool(e.value)
        _persist()

    # Registered AFTER construction: building the element must not overwrite
    # the remembered state with the constructor default.
    expansion.on_value_change(_remember)
    return expansion


__all__ = ["state", "state_expansion", "stored_expansion"]
