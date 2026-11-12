"""Last-resort operator feedback for uncaught UI handler exceptions.

NiceGUI's default exception handler is ``log.exception`` — a stderr line. The
browser never sees it, so an exception escaping an ``on_click`` handler makes
the click appear to do nothing at all. That was the 2026-11-12 "Launch button
does nothing" incident: ``shutil.Error`` from a MATLAB-locked file escaped the
Run page's narrow ``except ValueError``, and the only trace was console output
nobody was reading.

Every handler should still catch and surface its own failures — a specific
message beats a generic toast. This net exists so a future unguarded site
degrades to a visible red notice instead of silence.

Kept out of ``app.py`` on purpose: that module runs ``ui.run()`` at import time,
so nothing there is unit-testable.
"""

from __future__ import annotations

import contextlib
import logging

from nicegui import app as nicegui_app
from nicegui import ui

logger = logging.getLogger("ui.errors")

# Long enough to read a WinError path, short enough not to linger.
_NOTIFY_TIMEOUT_MS = 10_000
_MAX_MESSAGE_CHARS = 300


def message_for(exc: BaseException) -> str:
    """The single-line text shown to the operator."""
    return f"Action failed: {type(exc).__name__}: {str(exc)[:_MAX_MESSAGE_CHARS]}"


def report_unhandled(exc: BaseException) -> None:
    """Log the exception with its traceback and show the operator why."""
    logger.error("unhandled UI handler exception: %s", exc, exc_info=exc)
    # No client context (startup, a headless test) means no toast is possible;
    # the log line above is all that is available and raising here would
    # replace the real error with "no client context".
    with contextlib.suppress(Exception):
        ui.notify(message_for(exc), type="negative", timeout=_NOTIFY_TIMEOUT_MS)


def install() -> None:
    """Register the net on the app object (idempotent per name)."""
    nicegui_app.on_exception(report_unhandled)
