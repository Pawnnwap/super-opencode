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
    for page in ("run.py", "evolve.py"):
        src = _read("services", "webui", "pages", page)
        assert "manager.get_job_summary(job_id)" in src


def test_log_download_is_built_on_demand_not_per_tick():
    panel = _read("services", "webui", "components", "log_panel.py")
    # The button fetches on a worker thread; update() never builds the text.
    assert "async def _download" in panel
    assert "await nicegui_run.io_bound(self._download_text)" in panel
    update_body = panel.split("def update")[1].split("\n    def ")[0]
    assert "_download" not in update_body
