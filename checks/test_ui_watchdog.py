"""UI serving-loop hardening: selector loop + watchdog wiring in app.py.

The 2026-11-01 incident: uvicorn's loop factory forced ProactorEventLoop
(IOCP) on Windows; a wedged completion port stopped ALL socket serving
(accepts/receives never completed) while the loop idled, worker threads ran
on, and nothing reached the log. app.py now opts into the selector loop via
`loop="none"` + WindowsSelectorEventLoopPolicy, and a watchdog thread probes
loop responsiveness and HTTP serving every 10s so a recurrence is timed and
attributed in the log.
"""
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_APP_SRC = (_REPO / "app.py").read_text(encoding="utf-8")


def test_policy_is_set_before_ui_run():
    assert "WindowsSelectorEventLoopPolicy" in _APP_SRC
    assert _APP_SRC.index("WindowsSelectorEventLoopPolicy") < _APP_SRC.index("ui.run(")


def test_uvicorn_defers_loop_choice_to_the_policy():
    # Without loop="none", uvicorn's loop factory forces ProactorEventLoop on
    # win32 and the policy above is ignored.
    assert 'loop="none"' in _APP_SRC


def test_engineio_disconnect_stub_is_complete():
    # The old stub carried only REQUEST_METHOD; the complete environ carries
    # the ASGI callables so engineio can always answer.
    assert '"asgi.send": send' in _APP_SRC
    assert '"asgi.receive": receive' in _APP_SRC


def test_watchdog_probes_both_loop_and_serving():
    assert "call_soon_threadsafe" in _APP_SRC
    assert "urllib.request.urlopen" in _APP_SRC
    assert _APP_SRC.index("call_soon_threadsafe") < _APP_SRC.index("urllib.request.urlopen")
    assert "loop is frozen" in _APP_SRC
    assert "HTTP serving probe failed" in _APP_SRC


def test_selector_loop_is_the_platform_default_under_the_policy():
    if sys.platform != "win32":
        import pytest

        pytest.skip("Windows-only policy contract")
    import asyncio

    loop = asyncio.WindowsSelectorEventLoopPolicy().new_event_loop()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()
