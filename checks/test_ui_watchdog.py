"""UI serving-loop hardening: selector loop + watchdog wiring in app.py.

The 2026-11-01 incident: uvicorn's loop factory forced ProactorEventLoop
(IOCP) on Windows; a wedged completion port stopped ALL socket serving
(accepts/receives never completed) while the loop idled, worker threads ran
on, and nothing reached the log. app.py now opts into the selector loop via
`loop="none"` + WindowsSelectorEventLoopPolicy, and a watchdog thread probes
loop responsiveness every 10s so a freeze is timed and attributed in the
log. Serving health and revival belong to the task-owned soprun_watchdog
(watch_suprun.ps1): the old in-process HTTP probe was redundant with it,
never repaired anything, and each probe built a full page that churned one
"client deleted" line into ui.lifecycle a minute later.
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


def test_watchdog_is_loop_probe_only_serving_is_task_owned():
    assert "call_soon_threadsafe" in _APP_SRC
    assert "loop is frozen" in _APP_SRC
    # no in-process HTTP probing: redundant with the task-owned watchdog and
    # the probe churned a UI client per call
    assert "urllib" not in _APP_SRC
    # ... and the task-owned watchdog probes a side-effect-free route
    assert "/healthz" in _APP_SRC
    watch = (_REPO / "watch_suprun.ps1").read_text(encoding="utf-8")
    assert "healthz" in watch


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
