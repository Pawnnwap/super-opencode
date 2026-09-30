"""Loop-breaker restart guidance and the standing shell-heredoc rule.

Regression (run_dd910ecc): the agent re-issued `python - <<'EOF'` heredocs
through bash/PowerShell one-liners; the vague loop-breaker note ("do NOT
repeat it") failed to break the loop across 2 restarts, and no standing
prompt forbade the broken mechanism.
"""

from supervisor.core.loop_base import loop_breaker_section
from supervisor.prompts import (
    INIT_PROMPT_TEMPLATE,
    RESTART_PROMPT_TEMPLATE,
    SELF_EVOLUTION_INIT_PROMPT_TEMPLATE,
    SHELL_HEREDOC_RULE,
)


def test_heredoc_rule_in_all_standing_prompts():
    for template in (
        INIT_PROMPT_TEMPLATE,
        SELF_EVOLUTION_INIT_PROMPT_TEMPLATE,
        RESTART_PROMPT_TEMPLATE,
    ):
        assert SHELL_HEREDOC_RULE in template
        assert "heredoc" in template


def test_rules_have_no_braces_so_format_survives():
    assert "{" not in SHELL_HEREDOC_RULE and "}" not in SHELL_HEREDOC_RULE


def test_init_template_still_formats():
    out = INIT_PROMPT_TEMPLATE.format(
        protocol_text="PROTO",
        goal_section="",
        plan_section="",
        plan_output_section="",
        workspace="E:/ws",
        protected_files_desc="",
    )
    assert SHELL_HEREDOC_RULE in out
    assert out.rstrip().endswith("Begin.")


def test_self_evolution_template_still_formats():
    out = SELF_EVOLUTION_INIT_PROMPT_TEMPLATE.format(
        protocol_text="PROTO",
        baseline_note="",
        workspace="E:/ws",
        protected_files_desc="",
    )
    assert SHELL_HEREDOC_RULE in out


def test_restart_template_still_formats():
    out = RESTART_PROMPT_TEMPLATE.format(
        loop_section="LOOP\n",
        summary="SUM",
        task_state_section="",
        protocol_text="PROTO",
        workspace="E:/ws",
    )
    assert SHELL_HEREDOC_RULE in out
    assert out.startswith("LOOP\n")


def test_loop_breaker_quotes_reason_and_names_way_out():
    reason = (
        'repeated the same tool call ([shell] powershell.exe -Command '
        '"bash -lc \'cd /ws && python.exe - <<\'EOF\'...\'])'
    )
    section = loop_breaker_section(reason)
    assert reason in section
    assert "heredoc" in section
    assert "script file" in section
    assert "python -c" in section
    assert "read-only command" in section


def test_loop_breaker_handles_non_tool_loop_reasons():
    section = loop_breaker_section(
        "kept getting the same feedback across turns "
        "(--- SUPERVISOR CORRECTION NOTICE ---)",
    )
    assert "got stuck in a loop" in section
    assert "SUPERVISOR CORRECTION NOTICE" in section
