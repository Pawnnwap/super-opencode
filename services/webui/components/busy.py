"""Grey out buttons for the duration of an in-flight UI action.

Every button that starts a background or LLM call must disable itself until
that call settles. A slow call otherwise invites repeat clicks, which stack
duplicate long-running requests on NiceGUI's process-wide ``io_bound``
thread pool — the same pool every page shares for background work.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import AsyncIterator

from nicegui import ui


@contextlib.asynccontextmanager
async def busy_buttons(*buttons) -> AsyncIterator[None]:
    """Disable ``buttons`` until the wrapped awaitable settles.

    Re-enabling runs in a ``finally`` so a failed action still unlocks its
    buttons (the success paths usually reload the page instead).
    """
    for button in buttons:
        button.disable()
    try:
        yield
    finally:
        for button in buttons:
            button.enable()


class CancellableCall:
    """A single in-flight LLM call the user may abort from a Cancel button.

    Both the wizard's refine flow and the run page's feasibility draft need
    the same three steps: expose a stop event, honour a Cancel click, and
    clear the handle when the call settles. Each page had its own copy backed
    by a module-level dict, so a second in-flight call could silently
    overwrite the first one's handle.

    Deliberately ``begin``/``end`` rather than a context manager: both call
    sites already sit inside ``busy_buttons``, and one more wrapper level
    would re-indent their whole body for no gain.
    """

    def __init__(self) -> None:
        self._event: threading.Event | None = None

    @property
    def active(self) -> bool:
        return self._event is not None

    def begin(self) -> threading.Event:
        """Open a call and return its stop event."""
        event = threading.Event()
        self._event = event
        return event

    def end(self) -> None:
        """Close the call; the Cancel button can no longer reach it."""
        self._event = None

    def cancel(self, status_label=None, *, message: str = "Cancelling…") -> bool:
        """Request cancellation; False when nothing was in flight."""
        if self._event is None:
            ui.notify("Nothing is running.")
            return False
        self._event.set()
        if status_label is not None:
            status_label.set_text(message)
        return True
