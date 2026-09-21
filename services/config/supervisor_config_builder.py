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
        engine=session_state.get("engine", "opencode") or "opencode",
        codex_base_url=str(session_state.get("codex_base_url", "") or "").strip(),
        codex_api_key=str(session_state.get("codex_api_key", "") or ""),
        opencode_model=session_state["opencode_model"] or None,
        opencode_model_backup=session_state["opencode_model_backup"] or None,
        opencode_executable=session_state["opencode_executable"],
        enable_headroom=bool(session_state.get("enable_headroom", True)),
        headroom_executable=str(session_state.get("headroom_executable", "") or "").strip(),
        headroom_allow_custom_provider=bool(
            session_state.get("headroom_allow_custom_provider", False),
        ),
        supervisor_model=session_state["supervisor_model"] or "gpt-4o",
        supervisor_model_backup=session_state["supervisor_model_backup"] or None,
        supervisor_reasoning=str(session_state.get("supervisor_reasoning", "") or "").strip(),
        agent_reasoning=str(session_state.get("agent_reasoning", "") or "").strip(),
        timeout=int(session_state["timeout"]) * 60,
        protected_files=tuple(session_state.get("protected_files", [])),
        max_tokens=int(session_state["max_tokens"]),
        enable_python_scanner=bool(session_state["enable_python_scanner"]),
        enable_occam_razor=bool(session_state.get("enable_occam_razor", False)),
        # Snapshot credentials at enqueue. Running tasks will use their own
        # captured copy instead of whatever the live UI last wrote to env.
        openai_api_key=str(session_state.get("openai_key", "") or ""),
        openai_base_url=str(session_state.get("base_url", "") or "").strip(),
    )
    defaults.update(overrides)
    return SupervisorConfig(**defaults)
