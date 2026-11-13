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


def test_shell_reads_the_store_through_one_shared_snapshot():
    """render_shell walks the store three times per page render.

    A live store holds hundreds of summary files (retention prunes payloads
    only) and on 2026-11-13 that walk froze the event loop inside
    render_shell -> active_jobs -> json.load: the websocket dropped, the
    client was deleted, and the page reloaded with every panel collapsed.
    """
    src = _read("services", "webui", "layout.py")
    assert "_SNAPSHOT_TTL" in src
    assert "def _store_snapshot" in src
    # Both shell readers must go through the snapshot, not the store.
    for reader in ("def _queue_stats_line", "def active_jobs"):
        body = src.split(reader)[1].split("\ndef ", 1)[0]
        assert "_store_snapshot(" in body, f"{reader} still walks the store"
        assert ".list_jobs()" not in body
    # The detail page's sibling switcher shares it too.
    screen = _read("services", "webui", "components", "live_screen.py")
    switcher = screen.split("def _render_switcher")[1].split("\n    def ", 1)[0]
    assert "_store_snapshot(" in switcher
    assert ".list_jobs()" not in switcher


def test_store_snapshot_is_cached_and_scoped_per_store():
    """The snapshot is reused within the TTL and never leaks between stores."""
    from services.webui import layout

    class CountingStore:
        def __init__(self, rows):
            self.rows = rows
            self.reads = 0

        def list_jobs(self):
            return list(self.rows)

        def get_job_state(self, job_id):
            self.reads += 1
            return self.rows.get(job_id)

    class Manager:
        def __init__(self, store):
            self.store = store

    store = CountingStore({"a": {"type": "run", "state": "RUNNING"}})
    manager = Manager(store)
    layout._snapshots.clear()

    assert len(layout.active_jobs(manager)) == 1
    reads = store.reads
    assert reads == 1
    # Second read inside the TTL must not touch the store again.
    layout.active_jobs(manager)
    layout._queue_stats_line(manager, "run")
    assert store.reads == reads

    # A different store object is its own cache entry, never the old rows.
    other = CountingStore({"b": {"type": "run", "state": "SUCCESS"}})
    assert layout.active_jobs(Manager(other)) == []
    layout._snapshots.clear()


def test_state_cache_skips_reparsing_unchanged_files(tmp_path):
    """A full store walk after the first tick must be stats, not parses.

    Retention prunes log payloads only, so summaries accumulate forever and a
    finished job's summary is immutable. Re-reading all of them every 1.5 s
    was the disk work that froze the loop on 2026-11-13.
    """
    from services.jobs.state_store import JobStateStore

    store = JobStateStore(str(tmp_path))
    for i in range(30):
        store.save_job_state(f"run_{i:03d}", {"state": "SUCCESS", "updated_at": i})

    cache: dict = {}
    first = sum(
        1 for j in store.list_jobs()
        if store.get_job_state_cached(j, cache) is not None
    )
    assert first == 30
    assert len(cache) == 30

    # Identical stamps -> no re-parse. Rewriting one file bumps its stamp.
    import services.jobs.state_store as ss

    parses = {"n": 0}
    real_read = ss.JobStateStore._read_state_file

    def counting_read(self, path):
        parses["n"] += 1
        return real_read(self, path)

    # pylint: disable-next=protected-access
    ss.JobStateStore._read_state_file = counting_read
    try:
        for j in store.list_jobs():
            store.get_job_state_cached(j, cache)
        assert parses["n"] == 0, f"re-parsed {parses['n']} unchanged files"
        store.save_job_state("run_005", {"state": "FAILED", "updated_at": 99})
        store.get_job_state_cached("run_005", cache)
        assert parses["n"] == 1, "a rewritten summary must be re-read"
        assert cache["run_005"][1]["state"] == "FAILED"
    finally:
        # pylint: disable-next=protected-access
        ss.JobStateStore._read_state_file = real_read


def test_board_collect_uses_the_state_cache(tmp_path):
    """The 1.5 s board tick must not re-parse every summary."""
    import services.jobs.state_store as ss
    from services.jobs.state_store import JobStateStore
    from services.webui.components.board import JobBoard

    store = JobStateStore(str(tmp_path))
    for i in range(25):
        store.save_job_state(
            f"run_{i:03d}", {"type": "run", "state": "SUCCESS", "updated_at": i},
        )

    class Manager:
        def __init__(self, s):
            self.store = s

        def get_job_summary(self, job_id):
            return self.store.get_job_state(job_id)

    board = JobBoard.__new__(JobBoard)
    board.job_manager = Manager(store)
    board.job_type = "run"
    board._state_cache = {}

    jobs, unreadable = board._collect()
    assert len(jobs) == 25 and unreadable == 0

    parses = {"n": 0}
    real_read = ss.JobStateStore._read_state_file

    def counting_read(self, path):
        parses["n"] += 1
        return real_read(self, path)

    # pylint: disable-next=protected-access
    ss.JobStateStore._read_state_file = counting_read
    try:
        jobs, _ = board._collect()
        assert len(jobs) == 25
        assert parses["n"] == 0, (
            f"board tick re-parsed {parses['n']} unchanged summaries"
        )
    finally:
        # pylint: disable-next=protected-access
        ss.JobStateStore._read_state_file = real_read
