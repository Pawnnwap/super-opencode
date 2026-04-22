from __future__ import annotations

import datetime
import logging

from supervisor.monitoring.session_tracker import estimate_request_tokens

logger = logging.getLogger(__name__)


def should_record_turn(supervisor, content: str, role: str = "user") -> bool:
    """Determine whether turn should be added to history."""
    if not supervisor._compact_intermediate_steps:
        return True
    if role != "user":
        return True

    content_lower = content.lower()
    intermediate_indicators = [
        "step ",
        "planning",
        "analyzing",
        "considering",
        "let me",
        "i'll need to",
        "creating",
        "writing",
        "modifying",
        "running test",
        "running tests",
        "test result",
        "progress:",
        "phase:",
        "moving to",
        "now ",
        "next ",
    ]
    final_indicators = [
        "all targets met",
        "all targets are met",
        "targets achieved",
        "task complete",
        "task is complete",
        "objectives met",
        "protocol satisfied",
        "feedback:",
        "recommendation:",
    ]

    is_intermediate = any(
        indicator in content_lower for indicator in intermediate_indicators
    )
    is_final = any(indicator in content_lower for indicator in final_indicators)
    return is_final or not is_intermediate


def compact_history(supervisor) -> None:
    """Remove intermediate steps while preserving essential flow."""
    if not supervisor._history or len(supervisor._history) <= 2:
        return
    if not supervisor._compact_intermediate_steps:
        return

    keep_count = min(supervisor._max_history_turns, len(supervisor._history) // 2)
    if keep_count < 1:
        return

    preserved: list[dict] = []
    if supervisor._history:
        preserved.append(supervisor._history[0])

    for msg in supervisor._history[1:]:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if (
            role == "user" and should_record_turn(supervisor, content, role)
        ) or role == "assistant":
            preserved.append(msg)

    if len(preserved) > keep_count + 1:
        if len(preserved) > 2:
            core = preserved[:2]
            tail = preserved[-keep_count:]
            supervisor._history = core + tail
        else:
            supervisor._history = preserved[
                -min(keep_count * 2, len(preserved)) :
            ]


def estimate_current_tokens(supervisor) -> int:
    """Estimate total tokens for current conversation state."""
    conv_text = "\n".join(m["content"] for m in supervisor._history)
    return estimate_request_tokens(supervisor._system, conv_text, "").total


def log_prompt(supervisor, title: str, messages: list[dict]) -> None:
    """Write raw prompt messages to debug log file."""
    try:
        log_dir = supervisor._workspace / ".opencode"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "supervisor_prompts.log"

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 80}\n")
            f.write(f"--- {title} ---\n")
            f.write(f"Timestamp: {datetime.datetime.now().isoformat()}\n")
            f.write(f"{'=' * 80}\n\n")
            for msg in messages:
                role = msg.get("role", "unknown").upper()
                content = msg.get("content", "")
                f.write(f"[{role}]\n{content}\n\n{'-' * 40}\n\n")
    except Exception as exc:
        logger.debug("Failed to log prompt: %s", exc)


def should_omit_step_context(
    supervisor,
    opencode_output: str,
    experience_context: str,
    feedback_context: str,
    protected_context: str,
) -> bool:
    """Decide whether to strip step-context from history to save tokens."""
    history_context = "".join(
        [experience_context, feedback_context, protected_context, opencode_output],
    )
    conv_text = "\n".join(
        m["content"] for m in supervisor._history if m.get("content")
    )
    estimate = estimate_request_tokens(supervisor._system, conv_text, history_context)
    threshold = int(supervisor._max_tokens * 0.65)
    return estimate.total > threshold


def extract_and_store_opencode_output(
    supervisor,
    content: str,
    should_record: bool,
) -> None:
    """Extract opencode output from stored prompt for dedupe tracking."""
    if not should_record:
        return
    for marker in ("--- opencode output ---", "--- opencode plan output ---"):
        start = content.find(marker)
        if start < 0:
            continue
        output_start = start + len(marker)
        end_marker = content.find("\n--- end ---", output_start)
        if end_marker >= 0:
            supervisor._last_opencode_output = content[output_start:end_marker].strip()
            return


def update_system_prompt(supervisor, new_preamble: str) -> None:
    """Rebuild active system prompt from base protocol + extra preamble."""
    supervisor._system = supervisor._system_base
    if new_preamble:
        supervisor._system += f"\n\n{new_preamble}"

