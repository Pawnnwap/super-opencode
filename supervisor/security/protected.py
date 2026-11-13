"""Files a run owns and an agent must never rewrite.

The run's control files live in the workspace root, which is also the agent's
only writable root. The path policy therefore allows writing them, but two of
them are executable input to the harness:

* ``feasibility_facts.json`` — its ``check.cmd`` is run by the gate rubric on
  every judging round, so an agent write is a propose→execute channel;
* ``access_protection.json`` — it defines the whitelist and the protected set,
  so an agent write could widen its own boundary.

This module is the single place that decides "is this path one of those".
Both native gates (the Python ``guard_cli`` and the JS plugin, via
``guard_cli --check-protected``) call it, so the rule cannot drift between
them.
"""

from __future__ import annotations

import json
from pathlib import Path

from supervisor.security.access_profile import CONFIG_FILES, PROFILE_NAME

# Always protected, whatever the profile says: the profile itself and the
# facts file whose command the harness executes.
_ALWAYS_PROTECTED: tuple[str, ...] = (PROFILE_NAME, "feasibility_facts.json")


def _protected_names(workspace: Path) -> set[str]:
    """Basenames that may not be written, from defaults plus the profile."""
    names = {*_ALWAYS_PROTECTED, *CONFIG_FILES}
    try:
        data = json.loads(
            (Path(workspace) / PROFILE_NAME).read_text(encoding="utf-8-sig"),
        )
    except (OSError, ValueError):
        return names
    if isinstance(data, dict):
        for item in data.get("protected_files") or ():
            text = str(item).strip()
            if text:
                names.add(Path(text).name)
    return names


def protected_reason(workspace: str | Path, candidate: str | Path) -> str:
    """Why *candidate* may not be written, or '' when it is not protected.

    Only paths inside the workspace are considered: outside paths are already
    refused by the path policy, and a same-named file elsewhere is not this
    run's control file.
    """
    try:
        root = Path(workspace).resolve()
        target = Path(candidate)
        if not target.is_absolute():
            target = root / target
        target = target.resolve()
        relative = target.relative_to(root)
    except (OSError, ValueError):
        return ""
    if relative.name in _protected_names(root):
        return (
            f"{relative.as_posix()} is a run control file; the supervisor "
            "owns it and an agent write would change the run's own rules"
        )
    return ""


__all__ = ["protected_reason"]
