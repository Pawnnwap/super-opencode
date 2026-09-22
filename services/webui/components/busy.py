"""Grey out buttons for the duration of an in-flight UI action.

Every button that starts a background or LLM call must disable itself until
that call settles. A slow call otherwise invites repeat clicks, which stack
duplicate long-running requests on NiceGUI's process-wide ``io_bound``
thread pool — the same pool every page shares for background work.
"""

from __future__ import annotations

import contextlib
from typing import AsyncIterator


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
