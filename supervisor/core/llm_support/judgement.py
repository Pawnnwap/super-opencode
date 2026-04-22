from __future__ import annotations

from pathlib import Path

from supervisor.protocols.alignment import (
    AlignmentResult,
    verify_protocol_alignment as _verify_protocol_alignment,
)
from supervisor.core.llm_support.models import StepContext, SupervisorVerdict
from supervisor.protocols.protocol import Protocol
from supervisor.protocols.protocol_analyzer import ProtocolAnalysis, ProtocolAnalyzer


def judge(supervisor, opencode_output: str) -> SupervisorVerdict:
    from supervisor.prompts.templates import build_context_blocks, build_judge_prompt

    experience_context = supervisor._build_experience_context()
    protected_context, feedback_context = supervisor._get_evaluation_context()

    context_blocks = build_context_blocks(
        feedback_context,
        protected_context,
        experience_context,
    )
    msg = build_judge_prompt(opencode_output, context_blocks=context_blocks)
    history_msg = build_judge_prompt(opencode_output, context_blocks="")
    return supervisor._chat(msg, history_content=history_msg)


def judge_with_step_context(
    supervisor,
    opencode_output: str,
    step_context: StepContext,
) -> SupervisorVerdict:
    from supervisor.prompts import JUDGE_STEP_PROMPT

    protected_context, feedback_context = supervisor._get_evaluation_context()
    phases_str = (
        ", ".join(step_context.completed_phases)
        if step_context.completed_phases
        else "none"
    )
    experience_context = supervisor._build_experience_context()

    omit_sc = supervisor._should_omit_step_context(
        opencode_output,
        experience_context,
        feedback_context,
        protected_context,
    )

    msg = JUDGE_STEP_PROMPT.format(
        current_step=step_context.current_step,
        total_steps=step_context.total_steps_estimate,
        phase=step_context.phase,
        completed_phases=phases_str,
        experience_context=experience_context,
        feedback_context=feedback_context,
        protected_context=protected_context,
        opencode_output=opencode_output,
    )
    history_msg = JUDGE_STEP_PROMPT.format(
        current_step=0 if omit_sc else step_context.current_step,
        total_steps=0 if omit_sc else step_context.total_steps_estimate,
        phase="" if omit_sc else step_context.phase,
        completed_phases="" if omit_sc else phases_str,
        experience_context="",
        feedback_context="",
        protected_context="",
        opencode_output=opencode_output,
    )
    return supervisor._chat(msg, history_content=history_msg)


def judge_plan(
    supervisor,
    opencode_output: str,
    plan_round: int,
    total_plan_rounds: int,
    step_context: StepContext | None = None,
) -> SupervisorVerdict:
    from supervisor.prompts.templates import (
        build_context_blocks,
        build_plan_judge_prompt,
        build_step_context,
    )

    protected_context, feedback_context = supervisor._get_evaluation_context()

    context_info = ""
    if step_context is not None:
        phases_str = (
            ", ".join(step_context.completed_phases)
            if step_context.completed_phases
            else "none"
        )
        context_info = build_step_context(
            step_context.current_step,
            step_context.total_steps_estimate,
            step_context.phase,
            phases_str,
        ) + "\n"

    experience_context = supervisor._build_experience_context()
    context_blocks = build_context_blocks(
        feedback_context,
        protected_context,
        experience_context,
    )

    msg = build_plan_judge_prompt(
        opencode_output=opencode_output,
        plan_round=plan_round,
        total_plan_rounds=total_plan_rounds,
        step_context=context_info,
        context_blocks=context_blocks,
    )
    history_msg = build_plan_judge_prompt(
        opencode_output=opencode_output,
        plan_round=plan_round,
        total_plan_rounds=total_plan_rounds,
        step_context=context_info,
        context_blocks="",
    )

    verdict = supervisor._chat(msg, history_content=history_msg)
    return SupervisorVerdict(
        raw=verdict.raw,
        all_targets_met=False,
        feedback=verdict.feedback,
    )


def ask_for_compaction_instructions(supervisor) -> SupervisorVerdict:
    from supervisor.prompts import COMPACTION_INSTRUCTIONS_PROMPT

    return supervisor._chat(COMPACTION_INSTRUCTIONS_PROMPT)


def ask_for_deletion_permission(
    supervisor,
    candidates: list[str],
    workspace: Path,
) -> SupervisorVerdict:
    if not candidates:
        return ask_for_compaction_instructions(supervisor)

    file_list = "\n".join(f"  - {f}" for f in candidates)
    from supervisor.prompts import DELETION_PERMISSION_PROMPT

    msg = DELETION_PERMISSION_PROMPT.format(file_list=file_list)
    return supervisor._chat(msg)


def report_final_status(
    supervisor,
    reason: str,
    opencode_output: str,
) -> str:
    msg = (
        f"Run ending. Reason: {reason}\n\n"
        "Write a final report:\n"
        "1. What has been completed.\n"
        "2. Last known bug / blocker.\n"
        "3. Remaining undone tasks.\n\n"
        f"Latest opencode output:\n{opencode_output}"
    )
    return supervisor._chat(msg).raw


def generate_suggestions(
    supervisor,
    opencode_output: str,
    current_summary: str = "",
    step_context: StepContext | None = None,
) -> tuple[str, list[str]]:
    from supervisor.prompts.templates import build_context_blocks, build_step_context

    protected_files, chosen_paths = supervisor._read_protected_files_for_suggestions()
    protected_context = ""
    if protected_files:
        sections = []
        for path, content in protected_files.items():
            sections.append(f"--- {path} ---\n{content}\n--- end {path} ---")
        protected_context = (
            "\n\n## Current Protected Files State\n"
            + "\n\n".join(sections)
            + "\n\n"
        )

    step_context_block = ""
    if step_context:
        phases_str = (
            ", ".join(step_context.completed_phases)
            if step_context.completed_phases
            else "none"
        )
        step_context_block = build_step_context(
            step_context.current_step,
            step_context.total_steps_estimate,
            step_context.phase,
            phases_str,
        )

    summary_context = (
        f"\n\nCurrent implementation summary:\n{current_summary}"
        if current_summary
        else ""
    )

    preamble = (
        "Based on the opencode output below and the current implementation status,\n"
        "generate actionable suggestions for improving the code or approach.\n"
        "Focus on:\n"
        "1. Code quality improvements\n"
        "2. Potential bugs or edge cases\n"
        "3. Performance optimizations\n"
        "4. Better patterns or practices\n"
        "5. Missing tests or error handling\n\n"
    )
    postscript = (
        "Output ONLY the suggestions in a clear, actionable format. "
        "If no suggestions are needed, output 'No suggestions at this time.'"
    )

    context_blocks = build_context_blocks("", "", protected_context)
    msg = (
        f"{preamble}"
        f"--- Step Context ---\n{step_context_block}"
        f"{context_blocks}"
        f"--- opencode output ---\n"
        f"{opencode_output}\n--- end ---{summary_context}\n\n"
        f"{postscript}"
    )

    history_step_context = (
        build_step_context(0, 0, "unknown", "none") if step_context else ""
    )
    history_msg = (
        f"{preamble}"
        f"--- Step Context ---\n{history_step_context}"
        f"--- opencode output ---\n"
        f"{opencode_output}\n--- end ---{summary_context}\n\n"
    )

    return supervisor._chat(msg, history_content=history_msg).raw, chosen_paths


def analyze_protocol(
    supervisor,
    protocol: Protocol,
    use_llm: bool = False,
) -> ProtocolAnalysis:
    analyzer = ProtocolAnalyzer()
    analysis = analyzer.analyze(protocol)

    if use_llm:
        msg = (
            "Analyze the following protocol for an autonomous coding agent. "
            "Evaluate its quality in terms of clarity, testability, and completeness. "
            "Identify any vague targets, missing restrictions, or unclear inputs. "
            "Provide specific, actionable suggestions for improvement.\n\n"
            f"## INPUT\n{protocol.input_section}\n\n"
            f"## TARGET\n{protocol.target_section}\n\n"
            f"## RESTRICTIONS\n{protocol.restrictions_section}\n\n"
            "Rate each section (INPUT, TARGET, RESTRICTIONS) on clarity, "
            "testability, and completeness (0-100). "
            "Then list specific issues and suggestions."
        )
        _ = supervisor._chat(msg, is_intermediate=True)

    return analysis


def verify_protocol_alignment(
    opencode_output: str,
    protocol: Protocol,
) -> AlignmentResult:
    return _verify_protocol_alignment(opencode_output, protocol)
