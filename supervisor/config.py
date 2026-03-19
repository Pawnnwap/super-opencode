"""supervisor/config.py — immutable run configuration."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SupervisorConfig:
    protocol_path: Path
    workspace: Path
    max_retries: int = 3
    context_threshold: float = 0.60
    opencode_model: str | None = None
    opencode_executable: str = ""   # explicit path/command; auto-detected if blank
    supervisor_model: str = "gpt-4o"
    timeout: int = 300
    log_level: str = "INFO"
