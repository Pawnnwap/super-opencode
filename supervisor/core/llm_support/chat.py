from __future__ import annotations

import logging
import time

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    OpenAIError,
    RateLimitError,
)

from supervisor.core.llm_support.models import (
    _DONE_PHRASES,
    _check_completion_phrases,
    _is_token_limit_error,
    SupervisorVerdict,
)
from supervisor.monitoring.session_tracker import (
    estimate_request_tokens,
    truncate_with_fallback,
    warn_if_exceeds_limit,
)
from supervisor.monitoring.token_estimator import should_truncate
from supervisor.utils.text_utils import normalize_model_response

logger = logging.getLogger(__name__)


def chat(
    supervisor,
    user_content: str,
    is_intermediate: bool = False,
    history_content: str | None = None,
) -> SupervisorVerdict:
    should_record_user = (
        not is_intermediate
        or not supervisor._compact_intermediate_steps
        or supervisor.should_record_turn(user_content, "user")
    )
    if should_record_user and supervisor._last_opencode_output is not None:
        if user_content.strip() == supervisor._last_opencode_output.strip():
            should_record_user = False

    record_content = history_content if history_content is not None else user_content
    conv_text = "\n".join(
        msg["content"] for msg in supervisor._history if msg.get("content")
    )
    estimate = estimate_request_tokens(supervisor._system, conv_text, user_content)

    warning_msgs = warn_if_exceeds_limit(estimate, supervisor._max_tokens)
    for msg in warning_msgs:
        supervisor._token_warnings.append(msg)

    if should_truncate(estimate, supervisor._max_tokens):
        logger.warning(
            "--- FULL PROMPT EXCEEDING MAX TOKENS ---\n"
            "SYSTEM:\n%s\n"
            "HISTORY:\n%s\n"
            "USER:\n%s\n"
            "----------------------------------------",
            supervisor._system,
            conv_text,
            user_content,
        )

    if supervisor._truncation_enabled and should_truncate(
        estimate,
        supervisor._max_tokens,
    ):
        original_total = estimate.total
        user_content = fit_request_to_budget(supervisor, user_content)
        new_estimate = estimate_request_tokens(
            supervisor._system,
            "\n".join(
                m["content"] for m in supervisor._history if m.get("content")
            ),
            user_content,
        )
        trunc_warning = (
            f"Prompt truncated to fit within token limit. "
            f"Original: {original_total} tokens, New: {new_estimate.total} tokens, "
            f"Max available: {int(supervisor._max_tokens * (1.0 - 0.25))} tokens"
        )
        logger.warning(trunc_warning)
        supervisor._token_warnings.append(trunc_warning)

    messages = [{"role": "system", "content": supervisor._system}]
    messages.extend(supervisor._history)
    messages.append({"role": "user", "content": user_content})

    supervisor._log_prompt("Supervisor Chat", messages)
    return chat_with_retry(supervisor, messages, record_content, should_record_user)


def chat_with_retry(
    supervisor,
    messages: list[dict],
    record_content: str,
    should_record_user: bool,
) -> SupervisorVerdict:
    max_retries = 5
    empty_choices_retries = 0
    max_empty_choices_retries = 3
    attempt = 0
    transient_attempt = 0
    max_transient_retries = 4
    working_messages = list(messages)
    using_backup = False
    token_limit_retry = 0
    max_token_limit_retries = 3

    while attempt <= max_retries:
        current_model = supervisor._model_backup if using_backup else supervisor._model
        try:
            response = supervisor._client.chat.completions.create(
                model=current_model,
                messages=working_messages,
            )
            if not response.choices:
                if empty_choices_retries >= max_empty_choices_retries:
                    raise OpenAIError(
                        "LLM response returned no choices after %d retries"
                        % max_empty_choices_retries,
                    )
                wait = 15 * (2 ** empty_choices_retries)
                logger.warning(
                    "LLM response returned no choices, retry %d/%d after %ds",
                    empty_choices_retries + 1,
                    max_empty_choices_retries,
                    wait,
                )
                time.sleep(wait)
                empty_choices_retries += 1
                continue
            empty_choices_retries = 0
            reply = normalize_model_response(
                response.choices[0].message.content,
                "supervisor response",
            )
            break
        except (RateLimitError, APIConnectionError, APITimeoutError) as exc:
            if transient_attempt >= max_transient_retries:
                logger.error(
                    "Transient API error after %d retries, giving up: %s",
                    max_transient_retries,
                    exc,
                )
                raise
            wait = min(60, 5 * (2 ** transient_attempt))
            logger.warning(
                "Transient API error %s (attempt %d/%d), retrying after %ds",
                type(exc).__name__,
                transient_attempt + 1,
                max_transient_retries,
                wait,
            )
            time.sleep(wait)
            transient_attempt += 1
            continue
        except BadRequestError as exc:
            if _is_token_limit_error(exc):
                if token_limit_retry >= max_token_limit_retries:
                    logger.error(
                        "BadRequestError: input length exceeds model limit after %d retries",
                        max_token_limit_retries,
                    )
                    raise
                current_limit = (
                    supervisor._extract_token_limit_from_error(exc)
                    or supervisor._get_model_limit_for_model(current_model)
                )
                old_count = len(working_messages)
                working_messages = truncate_messages_for_limit(
                    working_messages,
                    current_limit,
                )
                new_count = len(working_messages)
                if new_count >= old_count:
                    working_messages = truncate_older_turns(working_messages)
                    new_count = len(working_messages)
                token_limit_retry += 1
                logger.warning(
                    "BadRequestError (input length) retry %d/%d: truncated messages from %d to %d for limit %d",
                    token_limit_retry,
                    max_token_limit_retries,
                    old_count,
                    new_count,
                    current_limit,
                )
                continue
            raise
        except (APIError, OpenAIError) as exc:
            if not using_backup and supervisor._model_backup:
                logger.warning(
                    "Primary model %s failed (%s), falling back to backup %s",
                    supervisor._model,
                    exc,
                    supervisor._model_backup,
                )
                using_backup = True
                attempt = 0
                continue
            if isinstance(exc, InternalServerError):
                code = getattr(exc, "code", None) or getattr(
                    exc,
                    "status_code",
                    None,
                )
                body = str(exc)
                if code != 511 and "max tokens" not in body.lower():
                    raise
                if attempt >= max_retries:
                    logger.error(
                        "Prompt still exceeds token limit after %d truncation attempts",
                        max_retries,
                    )
                    raise
                old_count = len(working_messages)
                working_messages = truncate_older_turns(working_messages)
                new_count = len(working_messages)
                removed = old_count - new_count
                attempt += 1
                logger.warning(
                    "Token-limit retry %d/%d: removed %d older message(s), now %d messages in request",
                    attempt,
                    max_retries,
                    removed,
                    new_count,
                )
            else:
                raise

    if should_record_user:
        supervisor._history.append({"role": "user", "content": record_content})
    supervisor._history.append({"role": "assistant", "content": reply})

    supervisor._extract_and_store_opencode_output(record_content, should_record_user)

    if len(supervisor._history) > supervisor._max_history_turns * 2:
        if supervisor._compact_intermediate_steps:
            supervisor.compact_history()
        else:
            supervisor._history = supervisor._history[:2] + supervisor._history[4:]

    all_met = _check_completion_phrases(reply, _DONE_PHRASES)
    return SupervisorVerdict(raw=reply, all_targets_met=all_met, feedback=reply)


def fit_request_to_budget(supervisor, user_content: str) -> str:
    """Shrink history and user content to fit within token budget."""
    from supervisor.monitoring.token_estimator import estimate_tokens, truncate_prompt

    available = int(supervisor._max_tokens * 0.75)
    system_tokens = estimate_tokens(supervisor._system)
    budget = max(available - system_tokens, supervisor._max_tokens // 8)

    def _msg_tokens(msg: dict) -> int:
        return estimate_tokens(msg.get("content", ""))

    history_tokens = sum(_msg_tokens(m) for m in supervisor._history)
    user_tokens = estimate_tokens(user_content)
    if history_tokens + user_tokens <= budget:
        return user_content

    dropped_any = False
    while supervisor._history and history_tokens + user_tokens > budget:
        if len(supervisor._history) <= 2:
            break
        removed = supervisor._history.pop(0)
        history_tokens -= _msg_tokens(removed)
        dropped_any = True
        if (
            removed.get("role") == "user"
            and supervisor._history
            and supervisor._history[0].get("role") == "assistant"
        ):
            removed2 = supervisor._history.pop(0)
            history_tokens -= _msg_tokens(removed2)

    per_msg_cap = max(budget // 4, 2000)
    if history_tokens + user_tokens > budget:
        for msg in supervisor._history:
            if _msg_tokens(msg) > per_msg_cap:
                original = msg.get("content", "")
                msg["content"] = truncate_prompt(original, per_msg_cap)
        history_tokens = sum(_msg_tokens(m) for m in supervisor._history)

    if history_tokens + user_tokens > budget:
        remaining_for_user = max(budget - history_tokens, budget // 4)
        user_content = truncate_prompt(user_content, remaining_for_user)

    if dropped_any:
        logger.warning(
            "Proactively trimmed history to fit budget (%d tokens, %d messages left)",
            budget,
            len(supervisor._history),
        )
    return user_content


def truncate_older_turns(messages: list[dict]) -> list[dict]:
    if len(messages) <= 2:
        return messages

    result = [messages[0]]
    remaining = messages[1:]
    skip = 0
    if remaining and remaining[0].get("role") == "user":
        skip = 1
        if len(remaining) > 1 and remaining[1].get("role") == "assistant":
            skip = 2
    elif remaining and remaining[0].get("role") == "assistant":
        skip = 1

    result.extend(remaining[skip:])
    return result


def truncate_messages_for_limit(
    messages: list[dict],
    model_limit: int,
) -> list[dict]:
    """Truncate messages to fit within specific model token limit."""
    from supervisor.monitoring.token_estimator import estimate_tokens

    if len(messages) <= 1:
        return messages

    result = [messages[0]]
    remaining = messages[1:]

    system_tokens = estimate_tokens(messages[0].get("content", ""))
    target_budget = int(model_limit * 0.75) - system_tokens
    if target_budget <= 0:
        logger.warning(
            "System prompt exceeds 75% of model limit, returning minimal messages",
        )
        if len(messages) > 1:
            last_user = None
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    last_user = msg
                    break
            if last_user:
                truncated_content = truncate_with_fallback(
                    last_user.get("content", ""),
                    model_limit // 2,
                )
                return [
                    messages[0],
                    {"role": "user", "content": truncated_content},
                ]
        return messages[:1]

    current_tokens = 0
    preserved = []
    for msg in reversed(remaining):
        msg_tokens = estimate_tokens(msg.get("content", ""))
        if current_tokens + msg_tokens <= target_budget:
            preserved.insert(0, msg)
            current_tokens += msg_tokens

    result.extend(preserved)
    return result

