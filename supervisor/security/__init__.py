"""Hard, rule-based filesystem access policy for supervised agent runs.

The supervisor's prompt-level "stay inside the workspace" reminder is advice.
This package turns the same boundary into a deterministic rule that every
execution backend enforces *before* a tool call runs:

* :mod:`supervisor.security.access_policy` - the single authority on which
  paths may be read or written.
* :mod:`supervisor.security.engine_guards` - translates that authority into
  each backend's native enforcement (opencode permissions + plugin, pi
  ``tool_call`` extension, codex ``PreToolUse`` hook).
* :mod:`supervisor.security.whitelist_advisor` - AI-proposed read-only
  reference directories, validated against the same rules before acceptance.

Only the project cwd (the run workspace) is writable. Whitelisted directories
are always read-only, and everything outside both is denied.
"""

from supervisor.security.access_policy import (
    AccessPolicy,
    PolicyDecision,
    PolicyViolation,
    build_policy,
)

__all__ = [
    "AccessPolicy",
    "PolicyDecision",
    "PolicyViolation",
    "build_policy",
]
