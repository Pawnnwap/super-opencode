"""supervisor/token_estimator.py — token estimation and prompt truncation."""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Average characters per token for English-like text (fallback)
_CHARS_PER_TOKEN = 4

# Safety margin: warn at this fraction of max
_WARN_AT_FRACTION = 0.80

# Truncation margin: keep this fraction of space available for response
_RESPONSE_MARGIN = 0.25

# Graduated warning thresholds (fraction of max_tokens)
_WARNING_THRESHOLDS = [0.50, 0.60, 0.70, 0.80, 0.90]

# tiktoken encoder (lazily loaded)
_tiktoken_encoder = None
_tiktoken_attempted = False


def _get_tiktoken_encoder():
    """Lazily load tiktoken encoder for o200k_base encoding (GPT-4o)."""
    global _tiktoken_encoder, _tiktoken_attempted
    if _tiktoken_encoder is not None:
        return _tiktoken_encoder
    if _tiktoken_attempted:
        return None

    _tiktoken_attempted = True
    try:
        import tiktoken
        _tiktoken_encoder = tiktoken.get_encoding("o200k_base")
        logger.info("tiktoken encoder loaded (o200k_base)")
    except Exception:
        _tiktoken_encoder = None
        logger.debug("tiktoken not available, using char-based estimation")
    return _tiktoken_encoder


def get_warning_thresholds() -> list[float]:
    """Return the graduated warning threshold fractions."""
    return list(_WARNING_THRESHOLDS)


def get_threshold_for_fraction(fraction: float) -> float | None:
    """Return the highest warning threshold that the given fraction has crossed, or None."""
    crossed = [t for t in _WARNING_THRESHOLDS if fraction >= t]
    return max(crossed) if crossed else None


@dataclass
class TokenEstimate:
    """Result of a token estimation."""

    total: int
    system_prompt: int
    conversation_history: int
    user_input: int

    @property
    def total_with_response(self) -> int:
        return self.total + int(self.total * _RESPONSE_MARGIN)


def estimate_tokens(text: str) -> int:
    """Estimate token count using tiktoken if available, else char-based ratio."""
    if not text:
        return 0
    enc = _get_tiktoken_encoder()
    if enc is not None:
        try:
            return max(1, len(enc.encode(text)))
        except Exception:
            pass
    return max(1, len(text) // _CHARS_PER_TOKEN)


def estimate_request_tokens(
    system_prompt: str,
    conversation_history: str,
    user_input: str,
) -> TokenEstimate:
    """Estimate tokens for a complete API request."""
    sys_tokens = estimate_tokens(system_prompt)
    hist_tokens = estimate_tokens(conversation_history)
    user_tokens = estimate_tokens(user_input)
    return TokenEstimate(
        total=sys_tokens + hist_tokens + user_tokens,
        system_prompt=sys_tokens,
        conversation_history=hist_tokens,
        user_input=user_tokens,
    )


def should_warn(estimate: TokenEstimate, max_tokens: int) -> bool:
    """Check if estimated tokens approach the model's maximum."""
    threshold = int(max_tokens * _WARN_AT_FRACTION)
    return estimate.total >= threshold


def should_truncate(estimate: TokenEstimate, max_tokens: int) -> bool:
    """Check if truncation is needed to fit within token limit."""
    available = int(max_tokens * (1.0 - _RESPONSE_MARGIN))
    return estimate.total > available


def truncate_prompt(
    text: str,
    max_tokens: int,
    preserve_end_ratio: float = 0.3,
) -> str:
    """Truncate prompt text keeping the end intact.

    Args:
        text: The text to truncate.
        max_tokens: Maximum allowed tokens.
        preserve_end_ratio: Fraction of max_tokens to preserve from the end.

    Returns:
        Truncated text with truncation marker if truncated, original if within limits.

    """
    estimated = estimate_tokens(text)
    available = int(max_tokens * (1.0 - _RESPONSE_MARGIN))

    if estimated <= available:
        return text

    # Calculate how many tokens we can keep
    keep_tokens = available
    preserve_end_tokens = int(keep_tokens * preserve_end_ratio)
    preserve_start_tokens = keep_tokens - preserve_end_tokens

    # Convert token counts back to character counts
    start_chars = preserve_start_tokens * _CHARS_PER_TOKEN
    end_chars = preserve_end_tokens * _CHARS_PER_TOKEN

    if len(text) <= start_chars + end_chars:
        return text

    start_text = text[:start_chars]
    end_text = text[-end_chars:]

    marker = (
        f"\n\n[... TRUNCATED: ~{estimated - keep_tokens} tokens removed "
        f"(preserving {preserve_end_ratio * 100:.0f}% from end) ...]\n\n"
    )

    truncated = start_text + marker + end_text
    new_estimate = estimate_tokens(truncated)
    logger.info(
        "Prompt truncated: %d → %d tokens (max %d)",
        estimated, new_estimate, max_tokens,
    )
    return truncated


def truncate_with_fallback(
    text: str,
    max_tokens: int,
    system_prompt: str = "",
    conversation_history: str = "",
) -> str:
    """Intelligently truncate text with fallback strategy.

    1. First, truncate the conversation history (oldest parts first).
    2. Then truncate user input preserving the end.
    3. System prompt is never truncated.

    Returns the truncated user_input portion.
    """
    estimate = estimate_request_tokens(system_prompt, conversation_history, text)

    if not should_truncate(estimate, max_tokens):
        return text

    available = int(max_tokens * (1.0 - _RESPONSE_MARGIN))
    sys_tokens = estimate_tokens(system_prompt)

    # How many tokens available for history + user input
    budget = available - sys_tokens
    if budget <= 0:
        logger.warning("System prompt alone exceeds token budget!")
        return truncate_prompt(text, max_tokens // 4)

    # Try truncating conversation history first
    hist_tokens = estimate_tokens(conversation_history)
    user_tokens = estimate_tokens(text)

    if hist_tokens + user_tokens > budget:
        # Allocate more to user input, less to history
        hist_budget = int(budget * 0.3)
        user_budget = budget - hist_budget

        if user_tokens > user_budget:
            logger.warning(
                "User input (%d tokens) exceeds budget (%d), truncating.",
                user_tokens, user_budget,
            )
            return truncate_prompt(text, user_budget)

    return text


def warn_if_exceeds_limit(estimate: TokenEstimate, max_tokens: int) -> list[str]:
    """Log warnings when estimated tokens exceed various thresholds.

    Returns a list of warning messages that were logged.
    """
    warnings = []
    available = int(max_tokens * (1.0 - _RESPONSE_MARGIN))

    if estimate.total > available:
        msg = (
            f"Prompt exceeds max tokens: {estimate.total} > {available} "
            f"(system: {estimate.system_prompt}, history: {estimate.conversation_history}, "
            f"user: {estimate.user_input}). Truncation will be applied."
        )
        logger.warning(msg)
        warnings.append(msg)
    elif estimate.total >= int(max_tokens * _WARN_AT_FRACTION):
        msg = (
            f"Prompt approaching token limit: {estimate.total}/{max_tokens} "
            f"({estimate.total / max_tokens * 100:.0f}%)"
        )
        logger.warning(msg)
        warnings.append(msg)

    return warnings


def safe_truncate_request(
    system_prompt: str,
    conversation_history: str,
    user_input: str,
    max_tokens: int,
    truncation_enabled: bool = True,
) -> tuple[str, str, str, list[str]]:
    """Safely handle token estimation, warning, and optional truncation.

    Args:
        system_prompt: The system prompt text
        conversation_history: The conversation history text
        user_input: The user input text
        max_tokens: Maximum allowed tokens
        truncation_enabled: Whether to apply truncation (default True)

    Returns:
        Tuple of (truncated_system, truncated_history, truncated_user, warning_messages)

    """
    estimate = estimate_request_tokens(system_prompt, conversation_history, user_input)
    warning_msgs = warn_if_exceeds_limit(estimate, max_tokens)

    truncated_sys = system_prompt
    truncated_hist = conversation_history
    truncated_user = user_input

    if truncation_enabled and should_truncate(estimate, max_tokens):
        available = int(max_tokens * (1.0 - _RESPONSE_MARGIN))
        sys_tokens = estimate_tokens(system_prompt)

        # Budget for history + user input
        budget = available - sys_tokens

        if budget > 0:
            hist_tokens = estimate_tokens(conversation_history)
            user_tokens = estimate_tokens(user_input)

            if hist_tokens + user_tokens > budget:
                # Allocate 30% to history, 70% to user
                hist_budget = int(budget * 0.3)
                user_budget = budget - hist_budget

                if hist_tokens > hist_budget:
                    truncated_hist = truncate_prompt(conversation_history, hist_budget, preserve_end_ratio=0.5)

                if user_tokens > user_budget:
                    truncated_user = truncate_prompt(user_input, user_budget)
                    warning_msgs.append(
                        f"User input truncated from ~{user_tokens} to ~{estimate_tokens(truncated_user)} tokens",
                    )
                    logger.warning(warning_msgs[-1])

    return truncated_sys, truncated_hist, truncated_user, warning_msgs
