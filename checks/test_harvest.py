"""Harvester: selection helpers, clean folder layout, fallbacks, dispatch.

The contract under test:
- string auto-match sees id / workspace / batch / final report;
- every batch folder is fresh (two launches in one second never share one);
- every selected run leaves report.md + run.json, and a harvest.md even when
  the agent fails;
- the root INDEX.md always exists; HARVEST.md appears when a summary exists;
- a run that is no longer finished is skipped, not harvested;
- JobManager dispatches the "harvest" job type to HarvestLoop.
"""

from __future__ import annotations

import time
from pathlib import Path

from services.jobs import harvest as hv
from services.jobs import harvest_loop as hl
from services.jobs import job_manager as jm
from services.jobs.harvest_loop import HarvestLoop
from services.jobs.job_manager import JobManager
from services.jobs.state_store import JobStateStore
from supervisor.security.access_policy import AccessPolicy
from supervisor.utils.config import SupervisorConfig


def _run_state(workspace: Path, state: str = "SUCCESS", report: str = "") -> dict:
    return {
        "type": "run",
        "state": state,
        "config": {"workspace": str(workspace), "engine": "codex"},
        "report": report,
        "updated_at": time.time(),
    }


def _make_store(tmp_path: Path, states: dict[str, dict]) -> JobStateStore:
    store = JobStateStore(str(tmp_path / "store"))
    for job_id, state in states.items():
        store.save_job_state(job_id, state)
    return store


def _harvest_config(tmp_path: Path, job_ids: list[str], batch: Path | None = None):
    batch = batch or (tmp_path / "harvest")
    batch.mkdir(parents=True, exist_ok=True)
    return SupervisorConfig(
        protocol_path=batch / "protocol.md",
        workspace=batch,
        engine="codex",
        harvest_job_ids=tuple(job_ids),
        harvest_prompt="Extract the best arm and its evidence.",
        harvest_dir=str(batch),
        timeout=5,
    )


def _output_path(prompt: str) -> Path:
    """The output file named by the prompt's output contract."""
    lines = prompt.splitlines()
    for index, line in enumerate(lines):
        if "write" in line.lower() and "exactly" in line.lower():
            for following in lines[index + 1:]:
                if following.strip():
                    return Path(following.strip().strip("`"))
    raise AssertionError(f"prompt has no output contract:\n{prompt}")


class FakeRunner:
    """Writes the contracted markdown file (or nothing) and reports prose."""

    def __init__(self, write: bool = True, prose: str = "agent prose"):
        self.write = write
        self.prose = prose
        self.stopped = False

    def start(self, prompt: str):
        if self.write:
            target = _output_path(prompt)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# Fake harvest\n\nresult\n", encoding="utf-8")
        yield {"level": "info", "msg": "fake agent"}

    def read_prose(self) -> str:
        return self.prose

    def stop(self) -> None:
        self.stopped = True


def _patch_runner(monkeypatch, runner: FakeRunner | None = None):
    runner = runner or FakeRunner()
    monkeypatch.setattr(hl, "create_runner", lambda _config: runner)
    return runner


# --------------------------------------------------------------- helpers -- #


def test_match_searches_id_workspace_batch_and_report(tmp_path):
    state = _run_state(tmp_path / "study_climax", report="best arm: 9/10")
    state["batch_id"] = "acad1103_1r25"
    haystack = hv.search_text("run_aaaa1111", state)

    assert hv.match_query("climax", haystack)
    assert hv.match_query("9/10", haystack)
    assert hv.match_query("acad1103", haystack)
    assert hv.match_query("RUN_AAAA", haystack)
    assert hv.match_query("", haystack)
    assert not hv.match_query("definitely-absent", haystack)


def test_batch_folder_is_always_fresh(tmp_path):
    first = hv.make_batch_dir(tmp_path, now=1_700_000_000)
    second = hv.make_batch_dir(tmp_path, now=1_700_000_000)

    assert first.parent == tmp_path
    assert second.parent == tmp_path
    assert first != second
    assert first.is_dir() and second.is_dir()


def test_slug_names_the_workspace_and_job(tmp_path):
    state = _run_state(tmp_path / "study 2026/11")
    slug = hv.run_slug("run_aaaa1111", state)

    assert slug.endswith("__run_aaaa1111")
    assert "/" not in slug and " " not in slug


def test_report_prefers_state_then_workspace_file(tmp_path):
    workspace = tmp_path / "study"
    workspace.mkdir()
    (workspace / "blocked_report.md").write_text("from disk\n", encoding="utf-8")

    assert hv.run_report(_run_state(workspace, report="from state\n")) == "from state\n"
    assert hv.run_report(_run_state(workspace)) == "from disk\n"


def test_config_builder_and_state_round_trip_harvest_fields(tmp_path):
    from services.config.settings import DEFAULTS
    from services.config.supervisor_config_builder import build_supervisor_config

    batch = tmp_path / "harvest"
    batch.mkdir()
    config = build_supervisor_config(
        DEFAULTS,
        protocol_path=batch / "protocol.md",
        workspace=batch,
        harvest_job_ids=("run_a", "run_b"),
        harvest_prompt="focus",
        harvest_dir=str(batch),
    )
    assert config.harvest_job_ids == ("run_a", "run_b")
    assert config.harvest_prompt == "focus"
    assert config.to_state_dict()["harvest_job_ids"] == ["run_a", "run_b"]

    manager = JobManager(store_dir=str(tmp_path / "store2"), max_parallel_jobs=1)
    manager.store.save_job_state(
        "harvest_cccc3333",
        {"type": "harvest", "state": "PENDING", "config": config.to_state_dict()},
    )
    restored = manager.config_from_state("harvest_cccc3333")
    assert restored is not None
    assert restored.harvest_job_ids == ("run_a", "run_b")
    assert restored.harvest_dir == str(batch)


# ------------------------------------------------------------------ loop -- #


def test_loop_writes_clean_folders_and_batch_summary(tmp_path, monkeypatch):
    ws_a = tmp_path / "study_a"
    ws_b = tmp_path / "study_b"
    ws_a.mkdir()
    ws_b.mkdir()
    store = _make_store(
        tmp_path,
        {
            "run_aaaa1111": _run_state(ws_a, report="# A report\n"),
            "run_bbbb2222": _run_state(ws_b, "BLOCKED", report="# B report\n"),
        },
    )
    _patch_runner(monkeypatch)
    config = _harvest_config(tmp_path, ["run_aaaa1111", "run_bbbb2222"])
    batch = Path(config.harvest_dir)

    events = list(HarvestLoop(config, store=store).run_streaming())

    folders = sorted(path for path in batch.iterdir() if path.is_dir())
    assert len(folders) == 2
    for folder in folders:
        assert (folder / "report.md").is_file()
        assert (folder / "run.json").is_file()
        assert (folder / "harvest.md").read_text(encoding="utf-8").startswith(
            "# Fake harvest",
        )
    index = (batch / "INDEX.md").read_text(encoding="utf-8")
    assert "run_aaaa1111" in index and "run_bbbb2222" in index
    assert (batch / "HARVEST.md").is_file()
    assert any(event.get("level") == "report" for event in events)
    assert events[-1]["level"] == "success"


def test_single_run_copies_harvest_as_batch_summary(tmp_path, monkeypatch):
    workspace = tmp_path / "study"
    workspace.mkdir()
    store = _make_store(tmp_path, {"run_aaaa1111": _run_state(workspace)})
    _patch_runner(monkeypatch)
    config = _harvest_config(tmp_path, ["run_aaaa1111"])
    batch = Path(config.harvest_dir)

    list(HarvestLoop(config, store=store).run_streaming())

    assert (batch / "HARVEST.md").read_text(encoding="utf-8").startswith(
        "# Fake harvest",
    )


def test_agent_failure_leaves_a_fallback_note(tmp_path, monkeypatch):
    workspace = tmp_path / "study"
    workspace.mkdir()
    store = _make_store(tmp_path, {"run_aaaa1111": _run_state(workspace)})
    _patch_runner(monkeypatch, FakeRunner(write=False, prose="nothing written"))
    config = _harvest_config(tmp_path, ["run_aaaa1111"])
    batch = Path(config.harvest_dir)

    events = list(HarvestLoop(config, store=store).run_streaming())

    folder = next(path for path in batch.iterdir() if path.is_dir())
    fallback = (folder / "harvest.md").read_text(encoding="utf-8")
    assert fallback.startswith("# Harvest fallback")
    assert "nothing written" in fallback
    assert (batch / "INDEX.md").is_file()
    assert events[-1]["level"] == "error"
    assert not any(event.get("level") == "success" for event in events)


def test_unfinished_run_is_skipped(tmp_path, monkeypatch):
    workspace = tmp_path / "study"
    workspace.mkdir()
    store = _make_store(
        tmp_path, {"run_aaaa1111": _run_state(workspace, state="RUNNING")},
    )
    _patch_runner(monkeypatch)
    config = _harvest_config(tmp_path, ["run_aaaa1111"])
    batch = Path(config.harvest_dir)

    events = list(HarvestLoop(config, store=store).run_streaming())

    assert not any(path.is_dir() for path in batch.iterdir())
    assert any("not finished" in event.get("msg", "") for event in events)
    assert events[-1]["level"] == "error"


def _capture_turn_configs(monkeypatch) -> list:
    """Record the per-turn configs; skip the native gate install side effects."""
    captured: list = []
    monkeypatch.setattr(
        hl, "create_runner",
        lambda config: (captured.append(config), FakeRunner())[1],
    )
    monkeypatch.setattr(
        HarvestLoop, "_install_access_guard", staticmethod(lambda *_a, **_k: None),
    )
    return captured


def test_agent_turn_writes_in_the_run_folder_only(tmp_path, monkeypatch):
    """The turn's cwd is the folder the prompt names, and the source run is
    readable through a default read-only whitelist entry (2026-11-12: the
    source workspace was the cwd, so every harvest.md write was denied)."""
    workspace = tmp_path / "study"
    workspace.mkdir()
    (workspace / "indicator.py").write_text("# arm\n", encoding="utf-8")
    store = _make_store(tmp_path, {"run_aaaa1111": _run_state(workspace)})
    captured = _capture_turn_configs(monkeypatch)
    config = _harvest_config(tmp_path, ["run_aaaa1111"])
    batch = Path(config.harvest_dir)

    list(HarvestLoop(config, store=store).run_streaming())

    folder = next(path for path in batch.iterdir() if path.is_dir())
    run_config = captured[0]
    policy = AccessPolicy.from_config(run_config)
    assert run_config.workspace == folder
    assert policy.is_writable(folder / "harvest.md")
    assert not policy.is_writable(workspace / "harvest.md")
    assert policy.is_readable(workspace / "indicator.py")


def test_batch_summary_turn_runs_in_the_harvest_folder(tmp_path, monkeypatch):
    ws_a = tmp_path / "study_a"
    ws_b = tmp_path / "study_b"
    ws_a.mkdir()
    ws_b.mkdir()
    store = _make_store(
        tmp_path,
        {
            "run_aaaa1111": _run_state(ws_a),
            "run_bbbb2222": _run_state(ws_b),
        },
    )
    captured = _capture_turn_configs(monkeypatch)
    config = _harvest_config(tmp_path, ["run_aaaa1111", "run_bbbb2222"])
    batch = Path(config.harvest_dir)

    list(HarvestLoop(config, store=store).run_streaming())

    assert len(captured) == 3  # two runs + the batch summary
    assert captured[-1].workspace == batch
    assert {str(root) for root in captured[-1].access_whitelist} >= {
        str(ws_a), str(ws_b),
    }


# -------------------------------------------------------------- dispatch -- #


class StubHarvest:
    def __init__(self, config, store=None):
        self.config = config
        self.runner = None

    def request_stop(self) -> None:
        pass

    def run_streaming(self):
        yield {"level": "info", "msg": "stub harvest"}
        yield {"level": "report", "msg": "# Stub index"}
        yield {"level": "success", "msg": "done"}


def _wait_terminal(manager, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = manager.get_job_summary(job_id) or {}
        if state.get("state") in {"SUCCESS", "FAILED", "CANCELLED", "BLOCKED"}:
            return state
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach a terminal state")


def test_job_manager_dispatches_harvest_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jm, "HarvestLoop", StubHarvest)
    manager = JobManager(store_dir=str(tmp_path / "store"), max_parallel_jobs=1)
    config = _harvest_config(tmp_path, ["run_aaaa1111"])

    job_id = manager.enqueue_job("harvest", config)
    final = _wait_terminal(manager, job_id)

    assert final["type"] == "harvest"
    assert final["state"] == "SUCCESS"
    assert final["report"] == "# Stub index"
    assert final["config"]["harvest_job_ids"] == ["run_aaaa1111"]
    assert final["config"]["harvest_dir"] == config.harvest_dir
