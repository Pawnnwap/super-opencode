"""Pi execution-agent support modules.

Mirror of ``opencode_support`` / ``codex_support`` for the pi coding agent
(``pi -p --mode json``, npm ``@earendil-works/pi-coding-agent``). Provides
executable location, command building, stream parsing, and a thin process
adapter over the shared three-engine driver
(:mod:`supervisor.runners.process_common`). Shared pieces (``RunResult``,
``extract_file_refs``, the tool-marker helper) are imported directly from
``opencode_support`` instead of being duplicated.
"""
