"""Secret-redaction contracts for persisted diagnostics."""

import json


def test_redacts_nested_credentials_without_mutating_the_source():
    from supervisor.security.redaction import redact_data, redact_text

    source = {
        "api_key": "unit-test-key",
        "api_key_enc": "already-encrypted",
        "command": (
            'curl -H "Authorization: Bearer unit-test-token" '
            "https://user:unit-test-password@example.test"
        ),
        "nested": [{"password": "unit-test-password"}],
        "max_tokens": 123,
    }
    original = json.loads(json.dumps(source))

    cleaned = redact_data(source)

    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["api_key_enc"] == "already-encrypted"
    assert cleaned["nested"][0]["password"] == "[REDACTED]"
    assert cleaned["max_tokens"] == 123
    assert "unit-test" not in json.dumps(cleaned)
    assert source == original
    assert redact_data(cleaned) == cleaned
    assert redact_text("OPENAI_API_KEY=unit-test-key") == ("OPENAI_API_KEY=[REDACTED]")
    assert redact_text("SUPER_OPENCODE_CODEX_KEY=unit-test-key") == (
        "SUPER_OPENCODE_CODEX_KEY=[REDACTED]"
    )
    assert redact_text('client_secret="unit-test-key"') == (
        'client_secret="[REDACTED]"'
    )
    flagged = redact_text(
        "agent --api-key unit-test-key --password 'unit-test-password'"
    )
    assert "unit-test" not in flagged
    assert flagged.count("[REDACTED]") == 2
    assert redact_text("--api-key unit-test-key") == "--api-key [REDACTED]"


def test_job_store_redacts_state_headlines_and_sidecars(tmp_path):
    from services.jobs.state_store import JobStateStore

    store = JobStateStore(str(tmp_path))
    secret = "unit-test-key"
    store.save_job_state(
        "job_1",
        {
            "state": "FAILED",
            "api_key": secret,
            "error": f"Authorization: Bearer {secret}",
        },
    )
    store.append_log(
        "job_1",
        {"level": "error", "msg": f"OPENAI_API_KEY={secret}"},
    )
    store.append_full(
        "job_1",
        {"level": "opencode_output", "body": f"password={secret}"},
    )

    state = store.get_job_state("job_1")
    logs = store.get_logs("job_1")
    full = store.read_full("job_1", 1)
    assert state["api_key"] == "[REDACTED]"
    assert secret not in state["error"]
    assert secret not in logs[0]["msg"]
    assert secret not in full["body"]
    persisted = "".join(path.read_text(encoding="utf-8") for path in tmp_path.iterdir())
    assert secret not in persisted


def test_goal_audit_redacts_verdict_fields(tmp_path):
    from types import SimpleNamespace

    from supervisor.core.goal_guard import GoalGuard
    from supervisor.core.llm_support.models import SupervisorVerdict
    from supervisor.core.target_state import AUDIT_FILENAME
    from supervisor.protocols.protocol import parse_protocol_text

    protocol = parse_protocol_text(
        "## INPUT\nx\n## TARGET\ny\n## RESTRICTIONS\nz",
    )
    config = SimpleNamespace(
        workspace=tmp_path,
        goal_require_changes=False,
        goal_verify_tests=False,
        max_blocked_stops=2,
    )
    guard = GoalGuard(config, protocol)
    secret = "unit-test-key"
    verdict = SupervisorVerdict(
        raw=f"api_key={secret}",
        all_targets_met=True,
        feedback=f"password={secret}",
        next_action=f"Authorization: Bearer {secret}",
    )

    guard.evaluate_done(verdict, [])

    audit_path = tmp_path / ".opencode" / AUDIT_FILENAME
    persisted = audit_path.read_text(encoding="utf-8")
    assert secret not in persisted
    assert "[REDACTED]" in persisted
    assert secret not in (tmp_path / ".opencode" / "target_state.json").read_text(
        encoding="utf-8",
    )


def test_access_policy_audit_and_denial_reason_are_redacted(tmp_path):
    from supervisor.security.access_policy import AccessPolicy, PolicyViolation
    from supervisor.security.guard_cli import decision_payload, log_violations

    policy = AccessPolicy(workspace=tmp_path)
    secret = "unit-test-key"
    command = f"OPENAI_API_KEY={secret} curl https://example.test"
    event = {"tool_name": "shell", "tool_input": {"command": command}}
    violations = [PolicyViolation(command, "write", f"password={secret}")]
    log_path = tmp_path / "violations.jsonl"

    log_violations(log_path, event, violations)
    payload = decision_payload(violations, policy)

    persisted = log_path.read_text(encoding="utf-8")
    assert secret not in persisted
    assert secret not in payload["systemMessage"]
    assert "[REDACTED]" in persisted
    assert "[REDACTED]" in payload["systemMessage"]


def test_judge_checkpoint_redacts_prompts_history_and_metadata(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from supervisor.core.llm_support.chat import (
        _write_judge_checkpoint,
        load_judge_checkpoint,
    )

    monkeypatch.setenv("SUPER_OPENCODE_KEY_FILE", str(tmp_path / "secret.key"))
    secret = "unit-test-key"
    supervisor = SimpleNamespace(
        _workspace=tmp_path,
        _history=[{"role": "assistant", "content": f"token={secret}"}],
    )
    _write_judge_checkpoint(
        supervisor,
        [{"role": "user", "content": f"OPENAI_API_KEY={secret}"}],
        f"password={secret}",
        f"Authorization: Bearer {secret}",
        True,
        {"model": "test-model", "api_key": secret},
    )

    checkpoint = load_judge_checkpoint(tmp_path)

    assert checkpoint["api_key"] == "[REDACTED]"
    assert secret not in json.dumps(checkpoint)
    assert "[REDACTED]" in checkpoint["messages"][0]["content"]
    assert secret not in (tmp_path / ".opencode" / "judge_pending.json").read_text(
        encoding="utf-8",
    )


def test_proxy_log_and_capability_cache_are_redacted(tmp_path, monkeypatch):
    import codex_compat_proxy as proxy

    secret = "unit-test-key"
    log_path = tmp_path / "proxy.log"
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(proxy, "LOG_FILE", log_path)
    monkeypatch.setattr(proxy, "CACHE_FILE", cache_path)
    monkeypatch.setattr(proxy, "_profiles", {})
    monkeypatch.setattr(
        proxy,
        "UPSTREAM",
        f"https://user:{secret}@example.test/v1",
    )

    proxy._log(f"Authorization: Bearer {secret}")
    proxy._save_cache()

    assert secret not in log_path.read_text(encoding="utf-8")
    assert secret not in cache_path.read_text(encoding="utf-8")
    assert "[REDACTED]" in log_path.read_text(encoding="utf-8")
    assert "[REDACTED]" in cache_path.read_text(encoding="utf-8")


def test_process_ledger_and_kill_refusals_are_redacted(tmp_path):
    from supervisor.runtime.process_registry import ProcessRegistry, ProcRecord

    secret = "unit-test-key"
    registry = ProcessRegistry("run_1", tmp_path)
    registry.register(
        ProcRecord(
            run_id="run_1",
            pid=123,
            create_time=1.0,
            cmdline=f"agent --api-key={secret}",
        )
    )
    registry.record_refusal(
        f"OPENAI_API_KEY={secret} kill 123",
        "123",
        f"password={secret}",
    )

    assert secret not in registry.find(123).cmdline
    assert secret not in registry.refusal_path.read_text(encoding="utf-8")
    assert "[REDACTED]" in registry.path.read_text(encoding="utf-8")


def test_ui_error_and_batch_failure_messages_are_redacted():
    from services.webui.batch_launch import (
        BatchLaunchError,
        batch_launch_failure_message,
    )
    from services.webui.error_net import message_for

    secret = "unit-test-key"
    cause = RuntimeError(f"OPENAI_API_KEY={secret}")
    error = BatchLaunchError(
        "study_test",
        ["job_1"],
        cause,
        cancel_failures={"job_1": f"password={secret}"},
    )

    assert secret not in message_for(cause)
    assert secret not in batch_launch_failure_message(error)
    assert "[REDACTED]" in message_for(cause)
    assert "[REDACTED]" in batch_launch_failure_message(error)


def test_logging_filter_redacts_messages_and_exception_tracebacks():
    import io
    import logging

    from supervisor.security.redaction import RedactingFilter

    secret = "unit-test-key"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("test.redaction")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    try:
        logger.error("Authorization: Bearer %s", secret)
        try:
            raise RuntimeError(f"password={secret}")
        except RuntimeError:
            logger.exception("request failed")
    finally:
        logger.handlers.clear()

    rendered = stream.getvalue()
    assert secret not in rendered
    assert rendered.count("[REDACTED]") >= 2


def test_crash_identity_line_redacts_argv_and_parent_commands(monkeypatch):
    from services.runtime import death_capture

    secret = "unit-test-key"
    monkeypatch.setattr(
        death_capture.sys,
        "argv",
        ["python", "app.py", f"--api-key={secret}"],
    )
    monkeypatch.setattr(
        death_capture,
        "_proc_info",
        lambda _proc: {"cmdline": f"parent --token={secret}"},
    )

    identity = death_capture._identity_line()

    assert secret not in identity
    assert "[REDACTED]" in identity


def test_supervisor_prompt_archive_is_redacted(tmp_path):
    from types import SimpleNamespace

    from supervisor.core.llm_support.history import log_prompt

    secret = "unit-test-key"
    supervisor = SimpleNamespace(_workspace=tmp_path)
    log_prompt(
        supervisor,
        "Supervisor Chat",
        [
            {"role": "system", "content": f"OPENAI_API_KEY={secret}"},
            {"role": "user", "content": f"Authorization: Bearer {secret}"},
        ],
    )

    persisted = (tmp_path / ".opencode" / "supervisor_prompts.log").read_text(
        encoding="utf-8"
    )
    assert secret not in persisted
    assert "[REDACTED]" in persisted


def test_experience_cache_and_memory_events_are_redacted(tmp_path):
    from supervisor.utils.experience_tracker import (
        _append_memory_event,
        _save_cache_to_file,
    )

    secret = "unit-test-key"
    _save_cache_to_file(tmp_path, {"worked": [f"api_key={secret}"]})
    _append_memory_event(tmp_path, "lesson", detail=f"token={secret}")

    cache = (tmp_path / ".opencode" / "experience_cache.json").read_text(
        encoding="utf-8",
    )
    events = (tmp_path / ".opencode" / "memory_events.jsonl").read_text(
        encoding="utf-8",
    )
    assert secret not in cache
    assert secret not in events
    assert "[REDACTED]" in cache
    assert "[REDACTED]" in events


def test_startup_diagnostic_print_redacts_credentials(capsys):
    from services.runtime import app_bootstrap

    app_bootstrap._print("OPENAI_API_KEY=unit-test-key")

    rendered = capsys.readouterr().out
    assert "unit-test-key" not in rendered
    assert "[REDACTED]" in rendered


def test_crash_excepthook_redacts_both_file_and_stderr(tmp_path, monkeypatch):
    import io
    import sys

    from services.runtime import death_capture

    secret = "unit-test-key"
    stream = io.StringIO()
    log_path = tmp_path / "fatal.log"
    monkeypatch.setattr(death_capture.faulthandler, "enable", lambda **_kw: None)
    monkeypatch.setattr(death_capture, "_handles", [])
    monkeypatch.setattr(death_capture.sys, "stderr", stream)
    death_capture.install(str(log_path))
    try:
        try:
            raise RuntimeError(f"password={secret}")
        except RuntimeError:
            death_capture.sys.excepthook(*sys.exc_info())
    finally:
        for handle in death_capture._handles:
            handle.close()
        death_capture._handles.clear()

    assert secret not in stream.getvalue()
    assert secret not in log_path.read_text(encoding="utf-8")
    assert "[REDACTED]" in stream.getvalue()


def test_guardian_log_is_redacted(tmp_path, monkeypatch):
    from services.runtime import guardian

    log_path = tmp_path / "guardian.log"
    monkeypatch.setattr(guardian, "LOG_DIR", tmp_path)
    monkeypatch.setattr(guardian, "LOG_PATH", log_path)
    monkeypatch.setattr(guardian, "LOG_MAX_BYTES", 1_000_000)

    guardian.log("provider failed: Authorization: Bearer unit-test-key")

    persisted = log_path.read_text(encoding="utf-8")
    assert "unit-test-key" not in persisted
    assert "[REDACTED]" in persisted


def test_hownow_cache_is_redacted(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace

    from services.webui.components import hownow

    cache_path = tmp_path / "hownow.json"
    state = SimpleNamespace(
        lock=threading.Lock(),
        report="Authorization: Bearer unit-test-key",
        error="password=unit-test-key",
        started_at=1.0,
        finished_at=2.0,
    )
    monkeypatch.setattr(hownow, "_CACHE_PATH", cache_path)
    monkeypatch.setattr(hownow, "_state", state)

    hownow._save_cache()

    persisted = cache_path.read_text(encoding="utf-8")
    assert "unit-test-key" not in persisted
    assert "[REDACTED]" in persisted


def test_harvest_baseline_redacts_reports_and_metadata(tmp_path):
    from services.jobs.harvest import write_run_baseline, write_run_meta

    secret = "unit-test-key"
    dest = tmp_path / "harvest"
    write_run_baseline(
        dest,
        "run_1",
        {
            "report": f"Authorization: Bearer {secret}",
            "config": {"workspace": str(tmp_path)},
        },
    )
    write_run_meta(dest, {"error": f"api_key={secret}"})

    report = (dest / "report.md").read_text(encoding="utf-8")
    metadata = (dest / "run.json").read_text(encoding="utf-8")
    assert secret not in report
    assert secret not in metadata
    assert "[REDACTED]" in report
    assert "[REDACTED]" in metadata


def test_harvest_fallback_redacts_agent_and_error_text(tmp_path):
    from services.jobs.harvest_loop import HarvestLoop

    HarvestLoop._write_fallback(
        tmp_path,
        "run_1",
        "password=unit-test-key",
        "Authorization: Bearer unit-test-key",
    )

    persisted = (tmp_path / "harvest.md").read_text(encoding="utf-8")
    assert "unit-test-key" not in persisted
    assert "[REDACTED]" in persisted


def test_task_state_journal_redacts_verdict_text(tmp_path):
    from supervisor.core.task_state import append_task_state, task_state_path

    append_task_state(
        tmp_path,
        turn=1,
        phase="build",
        step=1,
        total_steps=2,
        targets_met=False,
        feedback="password=unit-test-key",
        next_action="Authorization: Bearer unit-test-key",
    )

    persisted = task_state_path(tmp_path).read_text(encoding="utf-8")
    assert "unit-test-key" not in persisted
    assert "[REDACTED]" in persisted
