"""An engine that says nothing and fails must not hand the judge `[exit N]`.

run_70c94806 closed BLOCKED with 0/5 targets met because every turn surfaced a
bare ``[exit 1]``. The judge reads that as "the agent produced no evidence" and
closes gates UNMET — on a harness fault, not agent non-compliance. The floor in
``run_prompt_common`` labels such a turn as an engine failure instead.
"""

from types import SimpleNamespace

from supervisor.runners import process_common
from supervisor.runners.process_common import (
    ConfigErrorPolicy,
    EngineAttempt,
    StreamOutcome,
    run_prompt_common,
)


def _runner(tmp_path, backup=None):
    return SimpleNamespace(
        opencode_executable="",
        opencode_model="primary",
        opencode_model_backup=backup,
        agent="",
        timeout=5,
        workspace=tmp_path,
        _last_result=None,
        _chars_exchanged=0,
    )


def _run(monkeypatch, tmp_path, *, stdout, stderr, returncode, backup=None):
    monkeypatch.setattr(
        process_common.subprocess,
        "Popen",
        lambda *_a, **_k: SimpleNamespace(pid=None, poll=lambda: None, kill=lambda: None),
    )
    monkeypatch.setattr(process_common, "feed_stdin_prompt", lambda *_a: None)

    def fake_consume(_proc, **_kwargs):
        if False:  # pragma: no cover - makes this a generator
            yield
        return StreamOutcome(
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            timed_out=False,
            tokens_total=0,
        )

    monkeypatch.setattr(process_common, "consume_process_stream", fake_consume)
    runner = _runner(tmp_path, backup)
    list(
        run_prompt_common(
            runner,
            "prompt",
            engine="opencode",
            find_exe_fn=lambda _exe: "opencode",
            prepare_attempt=lambda *_a: EngineAttempt(cmd=["fake"]),
            classify_line=lambda _line: None,
            config_error=ConfigErrorPolicy(phrases=("unable to connect",)),
        ),
    )
    return runner._last_result


def test_silent_nonzero_exit_is_labelled_not_bare(tmp_path, monkeypatch):
    res = _run(monkeypatch, tmp_path, stdout="", stderr="", returncode=1)
    out = res.output
    assert out != "[exit 1]", out
    assert "no output at all" in out
    assert "harness failure" in out
    assert "[exit 1]" in out  # the code is still stated, alongside the reason


def test_any_real_output_skips_the_floor(tmp_path, monkeypatch):
    res = _run(monkeypatch, tmp_path, stdout="Model unavailable: x", stderr="", returncode=1)
    assert res.output.startswith("Model unavailable: x")
    assert "harness failure" not in res.output


def test_config_error_wrapper_still_owns_its_case(tmp_path, monkeypatch):
    """A phrase match lands in stderr, so the floor stays out of the way."""
    res = _run(
        monkeypatch,
        tmp_path,
        stdout="Error: unable to connect to provider",
        stderr="",
        returncode=1,
    )
    assert "CONFIG ERROR" in res.stderr
    assert "Raw error:" in res.stderr
    assert "harness failure" not in res.output


def test_clean_exit_is_untouched(tmp_path, monkeypatch):
    res = _run(monkeypatch, tmp_path, stdout="", stderr="", returncode=0)
    assert res.output == ""
    assert res.ok is True
