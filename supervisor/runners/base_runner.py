from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class BaseRunner:
    """Base class for all runner implementations.
    
    Provides common workspace management and lifecycle state.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._alive = False

    @property
    def is_alive(self) -> bool:
        """Return True if the runner is currently active."""
        return self._alive

    def stop(self) -> None:
        """Stop the runner and mark it as inactive."""
        self._alive = False

