"""Interactive state must survive a UI rebuild.

Two incidents, one root cause: NiceGUI re-creates elements when their parent
is rebuilt (a board tick, a ``@ui.refreshable`` section refresh, a page
reload), and a freshly constructed element starts at the caller's ``value``.

- The board's History panel was re-created on every changed signature - a
  streaming job changed it every 1.5 s - so opening History during a live run
  folded it shut again almost immediately.
- The wizard's sections are rebuilt when the workspace changes, folding
  panels the operator had opened.

Contract: a panel's open state is owned by the operator once toggled, and no
refresh may write it back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.webui import ui_state
from services.webui.components.board import JobBoard

SUCCESS = "SUCCESS"


class StubExpansion:
    """Stands in for ui.expansion; records every write to its open state."""

    def __init__(self, text="", value=False, on_value_change=None):
        self._value = bool(value)
        self.writes: list[bool] = []
        self.text = text
        self.visible = True
        self.callbacks = [on_value_change] if on_value_change else []

    @property
    def value(self) -> bool:
        return self._value

    @value.setter
    def value(self, new_value: bool) -> None:
        self._value = bool(new_value)
        self.writes.append(bool(new_value))
        for callback in self.callbacks:
            callback(type("E", (), {"value": self._value})())

    def set_text(self, text):
        self.text = text

    def set_visibility(self, visible):
        self.visible = visible

    def on_value_change(self, callback):
        self.callbacks.append(callback)

    # Operator interaction: the browser writes the value, not the server.
    def user_toggle(self, is_open: bool = True) -> None:
        self.value = is_open


class StubContainer:
    def __init__(self):
        self.clears = 0

    def clear(self):
        self.clears += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def clean_expansion_state(monkeypatch):
    monkeypatch.setattr(ui_state, "_EXPANSIONS", {})
    monkeypatch.setattr(
        ui_state.ui, "expansion",
        lambda text="", *, value=False, on_value_change=None, **kw: StubExpansion(
            text, value, on_value_change,
        ),
    )
    yield


def test_state_expansion_reopens_at_the_remembered_state(clean_expansion_state):
    first = ui_state.state_expansion("page.panel", "Panel")
    first.user_toggle(True)
    # A rebuild constructs a NEW element: it must come back open.
    second = ui_state.state_expansion("page.panel", "Panel")
    assert second.value is True
    assert second.writes == [], "construction must not write the state"


def test_state_expansion_uses_default_until_the_operator_toggles(
    clean_expansion_state,
):
    assert ui_state.state_expansion("a.panel", "Panel").value is False
    assert ui_state.state_expansion("b.panel", "Panel", default=True).value is True


def test_operator_close_is_also_remembered(clean_expansion_state):
    first = ui_state.state_expansion("page.panel", "Panel", default=True)
    first.user_toggle(False)
    assert ui_state.state_expansion("page.panel", "Panel", default=True).value is False


def _make_board(monkeypatch) -> JobBoard:
    board = JobBoard.__new__(JobBoard)
    board._history_signature = None
    board._history_title = ""
    board._history_auto_opened = False
    board._history_key = "board.run.history"
    board.history_exp = StubExpansion("History")
    board.history_grid = StubContainer()
    board._render_card = lambda job: None
    from services.webui.components import board as board_mod

    # The board binds the helper at import time; patch the binding it uses.
    monkeypatch.setattr(board_mod, "stored_expansion", lambda key: None)
    return board


def _job(job_id: str, state: str = SUCCESS) -> dict:
    return {"id": job_id, "status": {"type": "run", "state": state}}


def test_history_default_opens_once_on_an_idle_board(monkeypatch):
    board = _make_board(monkeypatch)
    board._render_history([_job("run_a")], auto_open=True)
    assert board.history_exp.value is True
    assert board.history_exp.writes == [True]


def test_operator_toggle_survives_later_refreshes(monkeypatch):
    board = _make_board(monkeypatch)
    board._render_history([_job("run_a")], auto_open=False)
    assert board.history_exp.writes == [], "an active board must not auto-open"

    board.history_exp.user_toggle(True)  # the operator opens History
    # A streaming job keeps changing the cards; every refresh rebuilds them.
    for index in range(5):
        board._render_history(
            [_job(f"run_{index}"), _job("run_a")], auto_open=False,
        )
    assert board.history_exp.value is True, (
        "a refresh collapsed History the operator had opened"
    )
    assert board.history_exp.writes == [True], (
        "refreshes must never write the open state"
    )


def test_operator_close_survives_later_refreshes(monkeypatch):
    board = _make_board(monkeypatch)
    board._render_history([_job("run_a")], auto_open=True)
    board.history_exp.user_toggle(False)
    for index in range(3):
        board._render_history([_job(f"run_{index}")], auto_open=True)
    assert board.history_exp.value is False


def test_unchanged_history_cards_are_not_rebuilt(monkeypatch):
    board = _make_board(monkeypatch)
    jobs = [_job("run_a"), _job("run_b")]
    board._render_history(jobs, auto_open=False)
    clears = board.history_grid.clears
    board._render_history(jobs, auto_open=False)
    assert board.history_grid.clears == clears


def test_history_is_hidden_when_no_terminal_jobs_remain(monkeypatch):
    board = _make_board(monkeypatch)
    board._render_history([_job("run_a")], auto_open=False)
    assert board.history_exp.visible is True
    board._render_history([], auto_open=False)
    assert board.history_exp.visible is False


def test_card_signature_ignores_log_churn():
    """A streaming run must not force a rebuild: cards never show logs."""
    board = JobBoard.__new__(JobBoard)
    quiet = _job("run_a", "RUNNING")
    streaming = _job("run_a", "RUNNING")
    streaming["status"]["updated_at"] = 99
    assert board._card_signature(quiet) == board._card_signature(streaming)


def test_board_never_rebuilds_the_whole_queue_column():
    text = Path(
        __import__("services.webui.components.board", fromlist=["x"]).__file__,
    ).read_text(encoding="utf-8")
    render_body = text.split("def _render(self")[1].split("\n    def ")[0]
    assert "queue_col.clear()" not in render_body, (
        "clearing the queue column destroys the History expansion"
    )


def test_log_panel_keeps_the_reader_position():
    """The log must not be pinned to the top on every arriving event."""
    from services.webui.components import log_panel

    text = Path(log_panel.__file__).read_text(encoding="utf-8")
    panel_body = text.split("class LogPanel")[1]
    update_body = panel_body.split("    def update(self, logs")[1].split("\n    def ")[0]
    assert "setInterval" not in update_body, (
        "the old 15-iteration top-pinning loop is what made the log unreadable"
    )
    assert "_restore_scroll_js" in update_body


def test_live_screen_offers_the_workspace_folder_button():
    """Every run detail page must reveal its workspace in one click."""
    from services.webui.components import live_screen

    text = Path(live_screen.__file__).read_text(encoding="utf-8")
    assert "open-folder" in text
    assert 'icon="folder_open"' in text
