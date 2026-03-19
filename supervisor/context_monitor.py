"""supervisor/context_monitor.py — tracks context window usage."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_DEFAULT_MAX = 128_000


class ContextMonitor:
    def __init__(self, threshold: float = 0.60, max_tokens: int = _DEFAULT_MAX):
        self.threshold = threshold
        self.max_tokens = max_tokens
        self._current = 0

    def update(self, tokens: int) -> None:
        self._current = tokens
        logger.debug("Context: ~%d / %d (%.1f%%)", tokens, self.max_tokens, self.fraction * 100)

    @property
    def fraction(self) -> float:
        return self._current / self.max_tokens

    @property
    def should_compact(self) -> bool:
        return self.fraction >= self.threshold

    def reset(self) -> None:
        self._current = 0
