"""supervisor/monitoring/session_tracker.py — Unified token and context tracking.

Consolidates token estimation (from token_estimator) and context window
monitoring (from context_monitor) into a single SessionTracker that can
be used across llm_supervisor.py, loop.py, and self_evolution_loop.py.

The module re-exports all public symbols from token_estimator and
context_monitor for backward compatibility, so existing imports continue
to work without modification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from supervisor.monitoring.context_monitor import ContextMonitor
from supervisor.monitoring.token_estimator import (TokenEstimate,
                                                   estimate_request_tokens,
                                                   estimate_tokens,
                                                   truncate_prompt,
                                                   truncate_with_fallback,
                                                   warn_if_exceeds_limit)

logger = logging.getLogger(__name__)


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
        max_tokens: int = 128_000,
        truncation_enabled: bool = True,
    ):
        self._monitor = ContextMonitor(threshold, max_tokens, truncation_enabled)
        self._state = SessionState(max_tokens=max_tokens)

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
        self._monitor.update(tokens, files_read=files_read, prompt_head=prompt_head)
        self._state.current_tokens = tokens
        if files_read is not None:
            self._state.files_read = files_read
        if prompt_head is not None:
            self._state.prompt_head = prompt_head[:100]

    # -- State queries (delegates to context_monitor) --

    @property
    def fraction(self) -> float:
        return self._monitor.fraction

    @property
    def should_compact(self) -> bool:
        return self._monitor.should_compact

    @property
    def can_continue_session(self) -> bool:
        return self._monitor.can_continue_session

    @property
    def approaching_limit(self) -> bool:
        return self._monitor.approaching_limit

    @property
    def is_critical(self) -> bool:
        return self._monitor.is_critical

    @property
    def estimated_tokens(self) -> int:
        return self._monitor.estimated_tokens

    @property
    def remaining_tokens(self) -> int:
        return self._monitor.remaining_tokens

    @property
    def truncation_enabled(self) -> bool:
        return self._monitor.truncation_enabled

    def get_reduction_advice(self) -> dict:
        return self._monitor.get_reduction_advice()

    def get_truncation_status(self) -> dict:
        return self._monitor.get_truncation_status()

    def reset(self) -> None:
        self._monitor.reset()
        self._state.current_tokens = 0
        self._state.last_warning_threshold = None
        self._state.compaction_triggered = False

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
        return self._state

    def get_context_info(self) -> dict:
        """Return a comprehensive context info dict for logging/UI."""
        advice = self.get_reduction_advice()
        truncation = self.get_truncation_status()
        return {
            "current_tokens": self._state.current_tokens,
            "max_tokens": self._state.max_tokens,
            "fraction": self.fraction,
            "should_compact": self.should_compact,
            "can_continue": self.can_continue_session,
            "approaching_limit": self.approaching_limit,
            "is_critical": self.is_critical,
            "files_read": self._state.files_read,
            "reduction_advice": advice,
            "truncation_status": truncation,
        }
