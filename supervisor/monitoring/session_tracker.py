"""supervisor/monitoring/session_tracker.py — Unified token and context tracking.

Consolidates token estimation (from token_estimator) and context window
monitoring into a single SessionTracker that can be used across
llm_supervisor.py, loop.py, and self_evolution_loop.py.

The module re-exports all public symbols from token_estimator for backward
compatibility, so existing imports continue to work without modification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from supervisor.monitoring.token_estimator import (
    TokenEstimate,
    estimate_request_tokens,
    estimate_tokens,
    should_truncate,
    truncate_prompt,
    truncate_with_fallback,
    warn_if_exceeds_limit,
    get_threshold_for_fraction,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAX = 128_000


@dataclass
class SessionState:
    """Snapshot of the current session's token and context state."""

    current_tokens: int = 0
    max_tokens: int = 128_000
    files_read: list[str] = field(default_factory=list)
    prompt_head: str = ""
    last_warning_threshold: float | None = None
    compaction_triggered: bool = False


class SessionTracker:
    """Unified tracker combining token estimation with context monitoring.

    Provides a single interface for:
    - Estimating token counts for text/requests
    - Tracking context window usage across a session
    - Emitting graduated warnings at threshold crossings
    - Deciding when to compact or truncate
    """

    def __init__(
        self,
        threshold: float = 0.60,
        max_tokens: int = _DEFAULT_MAX,
        truncation_enabled: bool = True,
    ):
        self.threshold = threshold
        self.max_tokens = max_tokens
        self._current = 0
        self._last_warning_threshold: float | None = None
        self._truncation_enabled = truncation_enabled
        self.compaction_triggered = False
        self._files_read: list[str] = []
        self._prompt_head: str = ""

    # -- Token estimation (delegates to token_estimator) --

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Estimate token count for a text string."""
        return estimate_tokens(text)

    @staticmethod
    def estimate_request(
        system_prompt: str,
        conversation_history: str,
        user_input: str,
    ) -> TokenEstimate:
        """Estimate tokens for a complete API request."""
        return estimate_request_tokens(system_prompt, conversation_history, user_input)

    # -- Context tracking --

    def update(
        self,
        tokens: int,
        files_read: list[str] | None = None,
        prompt_head: str | None = None,
    ) -> None:
        """Update the session's context state.

        Args:
            tokens: Current estimated token count.
            files_read: List of files loaded into context.
            prompt_head: First 100 chars of the current prompt.
        """
        self._current = tokens
        if files_read is not None:
            self._files_read = files_read
        if prompt_head is not None:
            self._prompt_head = prompt_head[:100]
        logger.debug(
            "Context: ~%d / %d (%.1f%%)",
            tokens,
            self.max_tokens,
            self.fraction * 100,
        )

        # Graduated warning: emit warning at each threshold crossed
        current_threshold = get_threshold_for_fraction(self.fraction)
        if (
            current_threshold is not None
            and current_threshold != self._last_warning_threshold
        ):
            self._last_warning_threshold = current_threshold
            file_info = (
                f"Files: {', '.join(self._files_read)}"
                if self._files_read
                else "Files: none"
            )
            prompt_info = (
                f"Prompt head: {self._prompt_head}"
                if self._prompt_head
                else "Prompt head: (empty)"
            )
            logger.warning(
                "Context usage at %.0f%% threshold: %d / %d tokens (%.1f%%). %s | %s | %s",
                current_threshold * 100,
                tokens,
                self.max_tokens,
                self.fraction * 100,
                self._get_advice_for_threshold(current_threshold),
                file_info,
                prompt_info,
            )

    def _get_advice_for_threshold(self, thresh: float) -> str:
        if thresh >= 0.90:
            return "CRITICAL: Context nearly full. Immediate compaction strongly recommended."
        if thresh >= 0.80:
            return "WARNING: Context high. Compaction recommended soon."
        if thresh >= 0.70:
            return "Context usage elevated. Monitor closely."
        if thresh >= 0.60:
            return "Context approaching compaction threshold. Plan for cleanup."
        return "Context usage above 50%. Be mindful of token budget."

    # -- State queries --

    @property
    def fraction(self) -> float:
        if self.max_tokens <= 0:
            return 1.0
        return self._current / self.max_tokens

    @property
    def should_compact(self) -> bool:
        return self.fraction >= self.threshold

    @property
    def can_continue_session(self) -> bool:
        """Check if context is low enough to safely continue the opencode session."""
        return self.fraction < 0.50

    @property
    def approaching_limit(self) -> bool:
        """Check if context is approaching the warning threshold (80%)."""
        return self.fraction >= 0.80

    @property
    def is_critical(self) -> bool:
        """Check if context is at a critical level (>= 90%)."""
        return self.fraction >= 0.90

    @property
    def estimated_tokens(self) -> int:
        """Return current estimated token count."""
        return self._current

    @property
    def remaining_tokens(self) -> int:
        """Return estimated remaining token capacity."""
        return max(0, self.max_tokens - self._current)

    @property
    def truncation_enabled(self) -> bool:
        """Return whether truncation is enabled."""
        return self._truncation_enabled

    def get_reduction_advice(self) -> dict:
        """Provide advice on how to reduce context size."""
        overage = max(0, self._current - int(self.max_tokens * self.threshold))
        if self.fraction >= 0.90:
            recommendation = "CRITICAL: Immediate context compaction required"
        elif self.should_compact:
            recommendation = "Context compaction recommended"
        elif self.approaching_limit:
            recommendation = "Context usage high — prepare for compaction"
        else:
            recommendation = "Context usage is within acceptable range"
        return {
            "current_tokens": self._current,
            "max_tokens": self.max_tokens,
            "overage_tokens": overage,
            "should_compact": self.should_compact,
            "approaching_limit": self.approaching_limit,
            "is_critical": self.is_critical,
            "fraction": self.fraction,
            "recommendation": recommendation,
        }

    def get_truncation_status(self) -> dict:
        """Provide detailed truncation status information."""
        available = int(self.max_tokens * 0.75)
        return {
            "truncation_enabled": self._truncation_enabled,
            "current_tokens": self._current,
            "max_tokens": self.max_tokens,
            "available_tokens": available,
            "would_need_truncation": self._current > available,
            "fraction": self.fraction,
        }

    def reset(self) -> None:
        self._current = 0
        self._last_warning_threshold = None
        self.compaction_triggered = False

    # -- Warnings --

    def get_warnings(self, estimate: TokenEstimate, max_tokens: int) -> list[str]:
        """Get token warnings for a given estimate."""
        return warn_if_exceeds_limit(estimate, max_tokens)

    # -- Truncation --

    @staticmethod
    def truncate_prompt(
        text: str,
        max_tokens: int,
        preserve_end_ratio: float = 0.3,
    ) -> str:
        return truncate_prompt(text, max_tokens, preserve_end_ratio)

    @staticmethod
    def truncate_with_fallback(
        text: str,
        max_tokens: int,
        system_prompt: str = "",
        conversation_history: str = "",
    ) -> str:
        return truncate_with_fallback(
            text, max_tokens, system_prompt, conversation_history
        )

    # -- Session state access --

    @property
    def state(self) -> SessionState:
        return SessionState(
            current_tokens=self._current,
            max_tokens=self.max_tokens,
            files_read=self._files_read,
            prompt_head=self._prompt_head,
            last_warning_threshold=self._last_warning_threshold,
            compaction_triggered=self.compaction_triggered,
        )

    def get_context_info(self) -> dict:
        """Return a comprehensive context info dict for logging/UI."""
        advice = self.get_reduction_advice()
        truncation = self.get_truncation_status()
        return {
            "current_tokens": self._current,
            "max_tokens": self.max_tokens,
            "fraction": self.fraction,
            "should_compact": self.should_compact,
            "can_continue": self.can_continue_session,
            "approaching_limit": self.approaching_limit,
            "is_critical": self.is_critical,
            "files_read": self._files_read,
            "reduction_advice": advice,
            "truncation_status": truncation,
        }
