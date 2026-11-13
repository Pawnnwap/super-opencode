"""Regression tests: connectivity probe budget, warm codex home, tree-kill stop.

Network-free: the codex probe tests drive a fake runner so the ladder,
per-attempt budget and backup gating are verified without spawning a CLI.
"""

import subprocess
import time
from typing import ClassVar

# NOTE: import the module, never `from ... import test_codex_connectivity` —
# pytest would collect that imported probe function as a test and hit the
# network.
from services.config import connectivity
from supervisor.runners.opencode_runner import OpencodeRunner
from supervisor.runners.opencode_support.result import RunResult


def test_distinct_backup_drops_same_model():
    assert connectivity._distinct_backup("m/a", "m/a") is None
    assert connectivity._distinct_backup("m/a", "  m/a ") is None
    assert connectivity._distinct_backup("m/a", None) is None
    assert connectivity._distinct_backup("m/a", "m/b") == "m/b"


def test_stable_probe_home_is_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.delenv("TMPDIR", raising=False)
    first = connectivity._stable_probe_home("codex")
    second = connectivity._stable_probe_home("codex")

    assert first == second
    assert first.exists()
    assert first.name == "codex_probe_home"


def test_probe_cleanup_uses_the_run_id_the_probe_spawned_under(tmp_path, monkeypatch):
    """Cleanup must terminate the probe's own scope, not a name-derived one.

    The probe runs in an ephemeral workspace while its warm home is a stable
    shared directory, so the two names differ. Deriving the run id from the
    home produced an id that matched no scope and no ledger record, making
    the cleanup a no-op: the orphaned git.exe kept the home locked and
    rmtree silently failed.
    """
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.delenv("TMPDIR", raising=False)
    workspace = connectivity._ephemeral_workspace("codex")
    home = connectivity._stable_probe_home("codex")
    try:
        probe_run_id = connectivity._probe_run_id(workspace)
        assert probe_run_id != connectivity._probe_run_id(home)

        terminated: list[str] = []
        from supervisor.runtime import process_control

        monkeypatch.setattr(
            process_control, "terminate_run",
            lambda run_id, **_kw: terminated.append(run_id) or 0,
        )
        connectivity._discard_probe_home(home, probe_run_id)

        assert terminated == [probe_run_id], (
            "cleanup did not terminate the scope the probe spawned under"
        )
        assert not home.exists()
    finally:
        import shutil

        shutil.rmtree(workspace, ignore_errors=True)


class _FakeRunner:
    """Minimal runner surface the probe touches; records constructor kwargs.

    The probe also assigns ``codex_reasoning_effort`` / ``codex_extra_env`` as
    post-init attributes, so created instances are kept for asserting on them.
    """

    last_kwargs: ClassVar[dict] = {}
    instances: ClassVar[list] = []
    # Per-instance probe outcome knobs (class-level defaults overridable via
    # configure()).
    prose: str = "model says hi"
    timed_out: bool = False
    ok: bool = True

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs
        type(self).instances.append(self)
        self.__dict__.update(kwargs)
        self._last_result = RunResult(
            stdout=self.prose if self.prose else "",
            returncode=0 if self.ok else 1,
            timed_out=self.timed_out,
            prose=self.prose,
        )

    @classmethod
    def configure(cls, **knobs):
        cls.prose = knobs.get("prose", "model says hi")
        cls.timed_out = knobs.get("timed_out", False)
        cls.ok = knobs.get("ok", True)

    def _run_prompt(self, prompt):
        return iter(())

    def read_output(self, timeout=None):
        return (self.prose, self.timed_out)

    def last_diagnostic(self):
        return "exit=1 (fake diagnostic)"

    def stop(self):
        pass


def _run_codex_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(connectivity, "CodexRunner", _FakeRunner)
    monkeypatch.setattr(connectivity, "find_codex", lambda explicit: "codex.cmd")
    monkeypatch.setattr(connectivity, "_ephemeral_workspace", lambda prefix: tmp_path)
    _FakeRunner.last_kwargs = {}
    _FakeRunner.instances = []
    return connectivity.test_codex_connectivity(
        "codex.cmd", "m/a", "m/a",
        timeout=45, base_url="http://x/v1", api_key="k",
    )


def test_codex_probe_passes_on_model_text_despite_exit_error(tmp_path, monkeypatch):
    """Model returned text but the CLI exited non-zero: connection is OK."""
    _FakeRunner.configure(prose="hi there", ok=False)
    ok, msg = _run_codex_probe(monkeypatch, tmp_path)

    assert ok, msg
    assert "codex responded" in msg


def test_codex_probe_passes_on_model_text_despite_stream_timeout(tmp_path, monkeypatch):
    """The answer arrived but cleanup ran past the stream timeout: still OK."""
    _FakeRunner.configure(prose="hi there", timed_out=True, ok=False)
    ok, msg = _run_codex_probe(monkeypatch, tmp_path)

    assert ok, msg


def test_codex_probe_fails_on_timeout_without_model_text(tmp_path, monkeypatch):
    _FakeRunner.configure(prose="", timed_out=True, ok=False)
    ok, msg = _run_codex_probe(monkeypatch, tmp_path)

    assert not ok
    assert "timed out without model text" in msg


def test_codex_probe_fails_on_error_without_model_text(tmp_path, monkeypatch):
    """Stderr/error-event text is NOT model text — a real failure stays one."""
    _FakeRunner.configure(prose="", timed_out=False, ok=False)
    ok, msg = _run_codex_probe(monkeypatch, tmp_path)

    assert not ok
    assert "without model text" in msg
    assert "fake diagnostic" in msg


def test_failed_probe_wipes_warm_home_for_self_heal(tmp_path, monkeypatch):
    """A killed probe can poison the shared warm home; failure must wipe it."""
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.delenv("TMPDIR", raising=False)
    # Resolve (and create) the home up front: the getter mkdirs as a side
    # effect, so the final assertion must check this path, not call it again.
    home = connectivity._stable_probe_home("codex")
    _FakeRunner.configure(prose="", timed_out=True, ok=False)
    ok, msg = _run_codex_probe(monkeypatch, tmp_path)

    assert not ok, msg
    assert not home.exists()


def test_codex_probe_gives_each_attempt_its_own_budget_and_warm_home(
    tmp_path, monkeypatch
):
    _FakeRunner.configure()
    ok, msg = _run_codex_probe(monkeypatch, tmp_path)

    assert ok, msg
    first_attempt = _FakeRunner.instances[0]
    # First ladder step succeeds: the runner budget IS the attempt budget
    # (remaining), never the whole probe budget again.
    assert 10 <= first_attempt.timeout <= 45
    # Same-model backup is dropped: a timeout retry must not rerun "m/a".
    assert _FakeRunner.last_kwargs["opencode_model_backup"] is None
    # CODEX_HOME is the warm shared home, not a throwaway under the workspace.
    assert first_attempt.codex_extra_env["CODEX_HOME"] == str(
        connectivity._stable_probe_home("codex")
    )


def test_stop_kills_whole_process_tree(tmp_path):
    """A `.cmd`-shim-style process tree must leave no grandchild behind."""
    runner = OpencodeRunner(
        workspace=tmp_path, timeout=5, opencode_executable="unused",
    )
    # cmd.exe (direct child) spawns ping.exe (grandchild) — mirrors the
    # cmd.exe -> codex.exe tree produced by npm-shim installs.
    runner._process = subprocess.Popen(
        ["cmd", "/c", "ping", "-n", "30", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    runner.stop()
    time.sleep(1.0)

    listing = subprocess.run(
        ["tasklist", "/fo", "csv"], capture_output=True, timeout=10, check=False
    ).stdout.decode("mbcs", "replace")
    assert "ping.exe" not in listing.lower()
