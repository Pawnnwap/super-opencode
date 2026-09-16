from supervisor.runners.command_common import safe_command_summary


def test_safe_command_summary_hides_prompt_and_sensitive_overrides():
    summary = safe_command_summary(
        [
            "codex",
            "exec",
            "-c",
            "model_providers.test.base_url=https://private.example.test",
            "--",
            "secret prompt content",
        ],
    )

    assert summary == "codex exec [arguments redacted]"
    assert "secret" not in summary
    assert "private.example" not in summary
