"""supervisor/runners/factory.py

Single entry point for constructing the execution-agent runner. Dispatches on
``config.engine`` ("opencode" default, or "codex"). Both runners share the same
public interface (``OpencodeRunner`` / ``CodexRunner(OpencodeRunner)``), so the
supervisor loop is agnostic to which backend it drives.
"""

from __future__ import annotations

import logging

from supervisor.runners.base_runner import BaseRunner

logger = logging.getLogger(__name__)


def resolve_engine(config) -> str:
    """Return the normalized engine name from a config ("opencode"|"codex")."""
    return (getattr(config, "engine", "opencode") or "opencode").strip().lower()


def create_runner(config, agent: str = "") -> BaseRunner:
    """Build the runner for the configured engine.

    Falls back to opencode for any unrecognized engine value so existing
    configs and persisted jobs keep working.
    """
    engine = resolve_engine(config)
    if engine == "codex":
        from supervisor.runners.codex_runner import CodexRunner

        logger.info("Creating CodexRunner (engine=codex, agent=%r)", agent)
        return CodexRunner.from_config(config, agent=agent)

    if engine != "opencode":
        logger.warning("Unknown engine %r; defaulting to opencode.", engine)

    from supervisor.runners.opencode_runner import OpencodeRunner

    logger.info("Creating OpencodeRunner (engine=opencode, agent=%r)", agent)
    return OpencodeRunner.from_config(config, agent=agent)


__all__ = ["create_runner", "resolve_engine"]
