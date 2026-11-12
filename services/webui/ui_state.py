"""Interaction state that must survive a UI rebuild.

NiceGUI re-creates elements whenever their parent is rebuilt: a
``@ui.refreshable`` section refresh, a board tick that re-renders its cards,
a page reload. A freshly constructed element starts at whatever ``value`` the
caller passed in, so a panel the operator had just opened silently folded
itself shut again - the "the page moved under my hands" class of bug.

The state is process-wide on purpose: this is a single-operator local tool,
and two tabs showing the same board should show the same panels open.
"""

from __future__ import annotations

from nicegui import ui

_EXPANSIONS: dict[str, bool] = {}


def stored_expansion(key: str) -> bool | None:
    """Stored open state of *key*, or ``None`` if never toggled."""
    return _EXPANSIONS.get(key)


def state_expansion(
    key: str, text: str, *, default: bool = False, **kwargs,
) -> ui.expansion:
    """A ``ui.expansion`` whose open state survives rebuilds and reloads.

    :param key: stable identity of this panel (unique per page/panel)
    :param default: state used only until the operator toggles it once
    """
    known = _EXPANSIONS.get(key)
    expansion = ui.expansion(
        text,
        value=default if known is None else known,
        **kwargs,
    )

    def _remember(e, k=key) -> None:
        _EXPANSIONS[k] = bool(e.value)

    # Registered AFTER construction: building the element must not overwrite
    # the remembered state with the constructor default.
    expansion.on_value_change(_remember)
    return expansion


__all__ = ["state_expansion", "stored_expansion"]
