"""supervisor/runners/factory.py

Single entry point for constructing the execution-agent runner. Dispatches on
``config.engine`` ("opencode" default, "codex", or "pi"). All runners are
siblings of one rich base: ``BaseRunner`` carries the entire public surface
(turn lifecycle, session pinning, step detection, archiving), and
``OpencodeRunner`` / ``CodexRunner`` / ``PiRunner`` implement only their
engine seams, so the supervisor loop is agnostic to which backend it drives.
"""

from __future__ import annotations

import logging

from supervisor.runners.base_runner import BaseRunner

logger = logging.getLogger(__name__)


def resolve_engine(config) -> str:
    """Return the normalized engine name from a config ("opencode"|"codex"|"pi")."""
    return (getattr(config, "engine", "opencode") or "opencode").strip().lower()


def create_runner(config, agent: str = "", run_id: str = "") -> BaseRunner:
    """Build the runner for the configured engine.

    Falls back to opencode for any unrecognized engine value so existing
    configs and persisted jobs keep working. ``run_id`` (default:
    ``config.run_id``) is the ownership id every child process of this run is
    ledgered under.
    """
    run_id = str(run_id or getattr(config, "run_id", "") or "")
    engine = resolve_engine(config)
    if engine == "codex":
        from supervisor.runners.codex_runner import CodexRunner

        logger.info("Creating CodexRunner (engine=codex, agent=%r)", agent)
        return _with_run_id(CodexRunner.from_config(config, agent=agent), run_id)

    if engine == "pi":
        from supervisor.runners.pi_runner import PiRunner

        logger.info("Creating PiRunner (engine=pi, agent=%r)", agent)
        return _with_run_id(PiRunner.from_config(config, agent=agent), run_id)

    if engine != "opencode":
        logger.warning("Unknown engine %r; defaulting to opencode.", engine)

    from supervisor.runners.opencode_runner import OpencodeRunner

    logger.info("Creating OpencodeRunner (engine=opencode, agent=%r)", agent)
    return _with_run_id(OpencodeRunner.from_config(config, agent=agent), run_id)


def _with_run_id(runner: BaseRunner, run_id: str) -> BaseRunner:
    """Stamp the ownership id (from_config reads config.run_id when present)."""
    if run_id and not getattr(runner, "run_id", ""):
        runner.run_id = run_id
    return runner


__all__ = ["create_runner", "resolve_engine"]
