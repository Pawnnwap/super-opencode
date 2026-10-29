"""Guard: `codex exec resume --last` must not fire while runs run in parallel.

Parallel replicas share one CODEX_HOME; a run that failed to capture its
own session id and then resumed "--last" could adopt another replica's
rollout. JobManager sets SOP_PARALLEL_RUNS once more than one job is
RUNNING; the command builder then degrades to a fresh session.

Also covers the proxy fix in the same concurrency family: the flattened
tool-name map is request-scoped, so two models flattening at once can no
longer rewrite each other's response tool-call names.
"""

import json

import pytest

from supervisor.runners.codex_support.command_builder import build_cmd


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SOP_PARALLEL_RUNS", raising=False)


def _cmd(**overrides):
    kwargs = dict(
        exe="codex",
        prompt="do the thing",
        agent="",
        codex_model=None,
        use_continue=True,
        session_id=None,
        use_shell=False,
    )
    kwargs.update(overrides)
    return build_cmd(**kwargs)


def test_last_resume_allowed_when_solo():
    cmd = _cmd()
    assert "resume" in cmd and "--last" in cmd


def test_last_resume_suppressed_under_parallel(monkeypatch):
    monkeypatch.setenv("SOP_PARALLEL_RUNS", "1")
    cmd = _cmd()
    assert "resume" not in cmd
    assert "--last" not in cmd


def test_explicit_session_id_still_resumes_under_parallel(monkeypatch):
    monkeypatch.setenv("SOP_PARALLEL_RUNS", "1")
    cmd = _cmd(session_id="sess_42")
    assert "resume" in cmd and "sess_42" in cmd


def test_job_manager_sets_and_clears_parallel_flag(tmp_path, monkeypatch):
    import threading
    import time as time_mod

    from services.jobs import job_manager as jm
    from services.jobs.job_manager import JobManager
    from supervisor.utils.config import SupervisorConfig

    class ParkedLoop:
        release = threading.Event()

        def __init__(self, config):
            self.runner = None

        def request_stop(self):
            pass

        def run_streaming(self):
            yield {"level": "info", "msg": "start"}
            while not ParkedLoop.release.is_set():
                time_mod.sleep(0.02)

    monkeypatch.delenv("SOP_PARALLEL_RUNS", raising=False)
    monkeypatch.setattr(jm, "SupervisorLoop", ParkedLoop)
    monkeypatch.setattr(jm, "SelfEvolutionLoop", ParkedLoop)

    manager = JobManager(str(tmp_path / ".job_store"), max_parallel_jobs=4)

    def wait_for(predicate, timeout=10.0):
        deadline = time_mod.time() + timeout
        while time_mod.time() < deadline:
            if predicate():
                return True
            time_mod.sleep(0.05)
        return False

    ids = []
    for i in range(2):
        ws = tmp_path / f"ws{i}"
        ws.mkdir()
        ids.append(manager.enqueue_job(
            "run",
            SupervisorConfig(protocol_path=ws / "protocol.md", workspace=ws),
        ))
    assert wait_for(lambda: all(
        (manager.store.get_job_state(j) or {}).get("state") == "RUNNING"
        for j in ids
    ))
    import os

    assert os.environ.get("SOP_PARALLEL_RUNS") == "1"

    ParkedLoop.release.set()
    assert wait_for(lambda: all(
        (manager.store.get_job_state(j) or {}).get("state") in ("SUCCESS", "FAILED")
        for j in ids
    ))
    assert os.environ.get("SOP_PARALLEL_RUNS") is None


def test_proxy_name_map_is_request_scoped(monkeypatch):
    """Two concurrent apply_profile calls must not share flatten mappings."""
    import importlib.util
    from pathlib import Path

    proxy_path = Path(__file__).resolve().parents[1] / "codex_compat_proxy.py"
    spec = importlib.util.spec_from_file_location("codex_compat_proxy", proxy_path)
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)

    def namespaced(prefix: str, tools: list[str]) -> dict:
        return {
            "model": "m",
            "tools": [{
                "type": "namespace",
                "name": prefix,
                "tools": [
                    {"type": "function", "name": n,
                     "parameters": {"type": "object"}}
                    for n in tools
                ],
            }],
        }

    prof = {"namespace_tools": False}
    _a, _notes_a, map_a = proxy.apply_profile(namespaced("alpha", ["shared"]), prof, "responses")
    _b, _notes_b, map_b = proxy.apply_profile(namespaced("beta", ["shared"]), prof, "responses")

    # Same original tool name under two namespaces: each request keeps its
    # own mapping; restoring one response never applies the other's.
    assert map_a["alpha__shared"] == ("alpha", "shared")
    assert map_b["beta__shared"] == ("beta", "shared")
    assert "alpha__shared" not in map_b and "beta__shared" not in map_a

    response = json.dumps([
        {"type": "function_call", "name": "beta__shared", "arguments": "{}"},
    ]).encode()
    restored = json.loads(proxy._restore_names(response, map_b))
    assert restored[0]["name"] == "shared" and restored[0]["namespace"] == "beta"
    # With the wrong (other request's) map the name stays untouched.
    untouched = json.loads(proxy._restore_names(response, map_a))
    assert untouched[0]["name"] == "beta__shared"
