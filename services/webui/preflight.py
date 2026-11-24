"""Launch preconditions as data, not prose.

Three places decided independently whether a launch was allowed — the run
page's ``locked`` flag, the wizard's hand-off checklist, and (not at all)
``launch_job`` itself — and they disagreed. The result was a button that
looked ready while the job was guaranteed to fail, plus two different causes
behind one identical "Open Protocol Wizard" call to action.

Every consumer now reads this module, so "can I launch?" has one answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Blocker:
    code: str
    label: str
    fix_href: str
    severity: str = "block"  # block | warn


def blockers(workspace: Path | str | None) -> list[Blocker]:
    """Everything standing between this workspace and a launch, in fix order."""
    from services.webui import i18n
    from services.webui.state import app_state
    from supervisor.protocols.protocol import resolve_protocol_path

    ws = Path(workspace) if workspace else None
    if ws is None or not ws.exists():
        return [
            Blocker(
                "no_workspace",
                i18n.t("no usable workspace path"),
                "/#configuration",
            ),
        ]

    out: list[Blocker] = []

    proto = resolve_protocol_path(ws)
    if not proto.is_file():
        out.append(
            Blocker(
                "no_protocol",
                i18n.tr("no {name} in {workspace}", name=proto.name, workspace=ws.name),
                "/#protocol",
            ),
        )

    if not app_state.tests_ok():
        out.append(
            Blocker(
                "probes_failed",
                i18n.t("connectivity probes not passed"),
                "/#connectivity",
            ),
        )

    try:
        from supervisor.security.access_profile import load_profile

        if not load_profile(ws).get("access_configured"):
            out.append(
                Blocker(
                    "access_not_configured",
                    i18n.t("access & protection not configured"),
                    "/#access",
                    "warn",
                ),
            )
    except Exception:
        logger.debug("Could not load access profile for %s", ws, exc_info=True)

    return out


def hard_blockers(
    workspace: Path | str | None,
    *,
    forgive_probes: bool = False,
) -> list[Blocker]:
    """Blockers that must disable Launch.

    ``forgive_probes`` is for the case where a job is already running: that
    proves the probes worked at the time, and the operator still needs to
    launch. A missing protocol is never forgiven — a live job says nothing
    about whether protocol.md exists now.
    """
    return [
        blocker
        for blocker in blockers(workspace)
        if blocker.severity == "block"
        and not (forgive_probes and blocker.code == "probes_failed")
    ]


__all__ = ["Blocker", "blockers", "hard_blockers"]
