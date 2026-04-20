from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from supervisor.utils.config import SupervisorConfig


def build_supervisor_config(
    session_state: Mapping[str, Any],
    protocol_path: Path,
    workspace: Path,
    **overrides,
) -> SupervisorConfig:
    defaults = dict(
        protocol_path=protocol_path,
        workspace=workspace,
        max_retries=int(session_state["max_retries"]),
        context_threshold=session_state["context_threshold"] / 100.0,
        opencode_model=session_state["opencode_model"] or None,
        opencode_model_backup=session_state["opencode_model_backup"] or None,
        opencode_executable=session_state["opencode_executable"],
        supervisor_model=session_state["supervisor_model"] or "gpt-4o",
        supervisor_model_backup=session_state["supervisor_model_backup"] or None,
        timeout=int(session_state["timeout"]) * 60,
        protected_files=tuple(session_state.get("protected_files", [])),
        max_tokens=int(session_state["max_tokens"]),
        enable_python_scanner=bool(session_state["enable_python_scanner"]),
    )
    defaults.update(overrides)
    return SupervisorConfig(**defaults)
