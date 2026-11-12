"""UI event-loop offload: the 1.5 s/2 s pollers must not block the loop.

A bare HTTP GET of the app once took 50 s while four codex jobs streamed:
the board's 1.5 s timer and the detail page's 2 s tick read job stores
synchronously ON the asyncio event loop, and under the workers' disk/GIL
pressure the loop stalled long enough for every browser websocket to die
("UI loses connection"). These tests pin the wiring that moved that work
onto worker threads (nicegui.run.io_bound) and killed the uncapped
whole-log parse per detail-page navigation.
"""
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _read(*parts: str) -> str:
    return (_REPO.joinpath(*parts)).read_text(encoding="utf-8")


def test_board_refresh_collects_on_a_worker_thread():
    board = _read("services", "webui", "components", "board.py")
    assert "async def refresh" in board
    assert "await nicegui_run.io_bound(self._collect)" in board
    # One-off paths (page load, delete) stay synchronous by design.
    assert "def refresh_sync" in board
    assert "self.refresh_sync()" in board


def test_detail_tick_fetches_off_the_loop():
    screen = _read("services", "webui", "components", "live_screen.py")
    assert "async def tick" in screen
    assert "await nicegui_run.io_bound(" in screen


def test_detail_navigation_does_not_parse_whole_logs():
    # The summary-only lookup moved into the shared launcher, so both pages
    # inherit it by calling render_live_screen rather than by each repeating
    # the call (which is exactly how one page could drift to full logs).
    launch = _read("services", "webui", "launch.py")
    assert "manager.get_job_summary(job_id)" in launch
    assert ".get_job_status(" not in launch
    for page in ("run.py", "harvest.py"):
        src = _read("services", "webui", "pages", page)
        assert "render_live_screen(" in src
        # A mention in a comment is fine; a call is not.
        assert ".get_job_status(" not in src


def test_log_download_is_built_on_demand_not_per_tick():
    panel = _read("services", "webui", "components", "log_panel.py")
    # The button fetches on a worker thread; update() never builds the text.
    assert "async def _download" in panel
    assert "await nicegui_run.io_bound(self._download_text)" in panel
    update_body = panel.split("def update")[1].split("\n    def ")[0]
    assert "_download" not in update_body


def test_board_metrics_keys_match_store_state_vocabulary():
    board = _read("services", "webui", "components", "board.py")
    states = _read("services", "jobs", "job_states.py")
    # Store states are UPPERCASE; lowercase keys silently zeroed every count
    # and the metrics line read "0 active / 0 success" forever. The vocabulary
    # is now single-sourced, so the counts cannot drift from the store.
    assert "ACTIVE_JOB_STATES" in states and "TERMINAL_JOB_STATES" in states
    assert '"RUNNING"' in states
    assert '"RUNNING": 0' not in board  # no re-typed literal table
    assert "ACTIVE_JOB_STATES | TERMINAL_JOB_STATES" in board
    assert ".upper()" in board
