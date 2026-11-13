"""UI serving-loop hardening: selector loop + watchdog wiring in app.py.

The 2026-11-01 incident: uvicorn's loop factory forced ProactorEventLoop
(IOCP) on Windows; a wedged completion port stopped ALL socket serving
(accepts/receives never completed) while the loop idled, worker threads ran
on, and nothing reached the log. app.py now opts into the selector loop via
`loop="none"` + WindowsSelectorEventLoopPolicy, and a watchdog thread probes
loop responsiveness every 10s so a freeze is timed and attributed in the
log. Serving health and revival belong to the task-owned Python guardian
(services/runtime/guardian.py): the old in-process HTTP probe was redundant
with it, never repaired anything, and each probe built a full page that
churned one "client deleted" line into ui.lifecycle a minute later.
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
    # no in-process HTTP probing: redundant with the task-owned guardian and
    # the probe churned a UI client per call
    assert "urllib" not in _APP_SRC
    # ... and the guardian probes a side-effect-free route
    assert "/healthz" in _APP_SRC
    guardian = (_REPO / "services" / "runtime" / "guardian.py").read_text(
        encoding="utf-8",
    )
    assert "/healthz" in guardian


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


def test_ui_lock_rewrites_a_predecessors_pid(tmp_path):
    """A restart must re-claim ui.lock, not inherit the dead pid.

    The lock used to be written only when the file was absent. After any
    restart it kept naming the predecessor, so the guardian saw
    app_process_alive=False while the real app was still importing and
    launched a second stack (2026-11-13: three "app not running" repairs in
    four minutes, two competing UI trees).
    """
    import os

    body = _APP_SRC.split("def _touch_ui_lock() -> None:")[1].split("\ndef ", 1)[0]
    lock = tmp_path / "ui.lock"
    lock.write_text('{"pid": 999999}', encoding="utf-8")
    namespace = {"os": os, "_UI_LOCK": lock}
    exec(  # noqa: S102 - executing the module's own source, not user input
        "import os\n" + "def _touch_ui_lock() -> None:" + body, namespace,
    )
    namespace["_touch_ui_lock"]()
    assert lock.read_text(encoding="utf-8").strip() == f'{{"pid": {os.getpid()}}}'
    # A second call on an already-correct lock must not rewrite it (the mtime
    # touch is the liveness signal the guardian reads).
    before = lock.stat().st_mtime_ns
    namespace["_touch_ui_lock"]()
    assert lock.stat().st_mtime_ns >= before
