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
    opencode_executable: str = ""
    supervisor_model: str = "gpt-4o"
    supervisor_model_backup: str | None = None
    opencode_model_backup: str | None = None
    timeout: int = 300
    log_level: str = "INFO"
    protected_files: tuple[str, ...] = ()
    read_external_feedback: bool = False
    max_tokens: int = 128_000
    max_protected_files_for_suggestions: int = 5
    truncation_enabled: bool = True
    max_history_turns: int = 40
    compact_intermediate_steps: bool = False
    plan_mode_rounds: int = 0
