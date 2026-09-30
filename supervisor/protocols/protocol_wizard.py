"""supervisor/protocol_wizard.py

Interactively refines the three protocol sections with the LLM
and returns a polished Protocol object + the markdown string.

Used by the web UI (services/webui/pages/wizard.py).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from supervisor.protocols.protocol import Protocol, parse_protocol_text
from supervisor.protocols.protocol_analyzer import ProtocolAnalysis, ProtocolAnalyzer
from supervisor.protocols.target_audit import audit_target
from supervisor.utils.llm_stream import (
    GenerationCancelled,
    make_stream_client,
    stream_chat_text,
)

logger = logging.getLogger(__name__)

# After the first draft, the wizard tests it against the deterministic target
# audit and feeds the exact findings back as targeted edit rounds. A draft
# that still fails after this many edits is never returned to the caller.
GATE_FIX_ROUNDS = 3

# Protocol drafting/editing is transcription-grade work; a thinking model at
# the provider default streams minutes of reasoning per draft — and the gate
# loop multiplies that by every fix round. Endpoints that reject the
# parameter fall back to one plain call (same tolerance as feasibility_wizard).
_REASONING_BODY = {"reasoning_effort": "low"}

REQUIRED_TARGET = "Construct/refactor the codebase to eliminate redundancy by implementing base classes and shared utility functions."

# The appended item never satisfies the audit by itself (it names no
# evidence), so the model's own TARGET items must carry it. Idempotence
# marker: skip re-appending when the item is already in the draft.
_REQUIRED_TARGET_MARKER = "eliminate redundancy by implementing base classes"


class ProtocolGateError(Exception):
    """The refined protocol still fails the target audit after all fix rounds."""

    def __init__(self, issues: tuple[str, ...], fix_rounds: int):
        self.issues = issues
        self.fix_rounds = fix_rounds
        super().__init__(
            f"TARGET not ready after {fix_rounds} auto-fix round(s): "
            + " ".join(issues)
        )


def _append_required_target(md: str) -> str:
    if _REQUIRED_TARGET_MARKER in md:
        return md
    lines = md.split("\n")
    target_idx = None
    for i, line in enumerate(lines):
        if line.strip().lower() == "## target":
            target_idx = i
            break
    if target_idx is None:
        return md
    count = 0
    last_numbered_idx = None
    for i in range(target_idx + 1, len(lines)):
        line = lines[i].strip()
        if line and line[0].isdigit():
            count += 1
            last_numbered_idx = i
        if line.startswith("## "):
            break
    if last_numbered_idx is None:
        return md
    new_item = f"{count + 1}. {REQUIRED_TARGET}"
    lines.insert(last_numbered_idx + 1, new_item)
    return "\n".join(lines)


_WIZARD_SYSTEM = """\
Write clean, unambiguous protocol.md
for coding agent called opencode.

Protocol has 3 sections:

  ## INPUT        — what already exists / what the agent is given
  ## TARGET       — numbered, testable deliverables the agent must produce
  ## RESTRICTIONS — hard rules the agent must never violate

When user gives you raw notes for any section, you must:
1. Rewrite them in precise, imperative language.
2. Make deliverables concrete and testable (good: "All pytest tests pass";
   bad: "the code should work").
   TARGET is checked by a deterministic audit before acceptance: it must
   contain acceptance evidence (tests, pytest, benchmark, a measurable
   check), a completion state ("done when … passes"), and a failure
   condition (what blocks acceptance or requires replanning).
   Never leave "improve" or "enhance" unmeasured.
3. Keep restrictions as clear prohibitions ("Do not …").
4. Return ONLY the full protocol.md content, no preamble, no commentary.
   The file must start with the three headings in order.
"""


def _fix_prompt(protocol_md: str, issues: tuple[str, ...]) -> str:
    findings = "\n".join(f"- {issue}" for issue in issues)
    return (
        "The protocol.md you produced failed a deterministic acceptance "
        "audit.\n\n"
        f"{protocol_md}\n\n"
        "Audit findings that must be fixed:\n"
        f"{findings}\n\n"
        "Edit the TARGET so every finding resolves: name concrete acceptance "
        "evidence (e.g. the pytest suite or benchmark that must pass), the "
        "observable done condition, and what blocks acceptance. Keep the "
        "three headings and everything else unchanged.\n\n"
        "Return ONLY the corrected full protocol.md content, no preamble, "
        "no commentary."
    )


class ProtocolWizard:
    """Drives a guided conversation to produce a refined protocol.md.
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        # Streamed + bounded: a long refine used to blow past the SDK's 600s
        # read timeout and then retry twice more, freezing the UI for half
        # an hour. See supervisor.utils.llm_stream.
        self._client = make_stream_client(api_key=api_key, base_url=base_url)
        self._model = model

    # ------------------------------------------------------------------ #
    # One-shot refinement (used by the wizard form)                    #
    # ------------------------------------------------------------------ #

    def _chat(
        self,
        user_msg: str,
        on_progress: Callable[[int, int, int], None] | None = None,
        stop_event: Any = None,
    ) -> str:
        messages = [
            {"role": "system", "content": _WIZARD_SYSTEM},
            {"role": "user", "content": user_msg},
        ]
        try:
            return stream_chat_text(
                self._client,
                self._model,
                messages,
                temperature=0.3,
                field_name="protocol wizard response",
                on_progress=on_progress,
                stop_event=stop_event,
                extra_body=_REASONING_BODY,
            )
        except (GenerationCancelled, TimeoutError):
            raise
        except Exception as exc:  # noqa: BLE001 — parameter rejection
            logger.info(
                "Protocol wizard: reasoning_effort rejected (%s); retrying "
                "without it", exc,
            )
            return stream_chat_text(
                self._client,
                self._model,
                messages,
                temperature=0.3,
                field_name="protocol wizard response",
                on_progress=on_progress,
                stop_event=stop_event,
            )

    def refine(
        self,
        raw_input: str,
        raw_target: str,
        raw_restrictions: str,
        on_progress: Callable[[int, int, int], None] | None = None,
        stop_event: Any = None,
        on_round: Callable[[int, int, tuple[str, ...]], None] | None = None,
        max_fix_rounds: int = GATE_FIX_ROUNDS,
    ) -> tuple[str, Protocol]:
        """Send all three raw sections to the LLM in one shot.

        Every draft is tested against the deterministic target audit; a
        failing draft is edited again with the exact audit findings until
        the gate passes. After ``max_fix_rounds`` edit rounds a failing
        draft raises :class:`ProtocolGateError` — a failed version is
        never returned.

        ``on_round(fix_round, max_fix_rounds, issues)`` fires before each
        edit round so the UI can show what the gate rejected.

        Returns (refined_markdown, Protocol).
        """
        user_msg = (
            "Please refine the following raw protocol notes into a clean protocol.md.\n\n"
            f"### INPUT (raw)\n{raw_input}\n\n"
            f"### TARGET (raw)\n{raw_target}\n\n"
            f"### RESTRICTIONS (raw)\n{raw_restrictions}"
        )

        refined_md = self._chat(
            user_msg, on_progress=on_progress, stop_event=stop_event,
        )

        issues: tuple[str, ...] = ()
        for fix_round in range(max_fix_rounds + 1):
            candidate = _append_required_target(refined_md)
            try:
                protocol = parse_protocol_text(candidate)
            except ValueError as exc:
                # Unparseable output is a gate failure like any other:
                # feed the exact parse error back for a targeted edit.
                issues = (str(exc),)
            else:
                audit = audit_target(
                    protocol.target_section, protocol.restrictions_section,
                )
                if audit.is_actionable:
                    return candidate, protocol
                issues = audit.issues
            if fix_round == max_fix_rounds:
                break
            if on_round is not None:
                on_round(fix_round + 1, max_fix_rounds, issues)
            refined_md = self._chat(
                _fix_prompt(candidate, issues),
                on_progress=on_progress, stop_event=stop_event,
            )
        raise ProtocolGateError(issues, max_fix_rounds)

    def refine_section(
        self,
        section_name: str,
        raw_text: str,
        existing_context: str = "",
    ) -> str:
        """Refine a single section in isolation (used for the iterative wizard).
        Returns the rewritten section text (no heading).
        """
        context_note = (
            f"\n\nContext from other sections already written:\n{existing_context}"
            if existing_context
            else ""
        )
        user_msg = (
            f"Refine the {section_name} section for a protocol.md file.\n"
            f"Raw notes:\n{raw_text}"
            f"{context_note}\n\n"
            f"Return ONLY the body text for the {section_name} section "
            "(no heading, no preamble)."
        )
        return self._chat(user_msg)

    def analyze_sections(
        self,
        raw_input: str,
        raw_target: str,
        raw_restrictions: str,
    ) -> ProtocolAnalysis | None:
        """Analyze raw protocol sections and return quality feedback.
        Returns None if the text cannot be parsed into a valid protocol.
        """
        analyzer = ProtocolAnalyzer()
        # Build a temporary protocol-like text for analysis
        temp_text = (
            f"## INPUT\n\n{raw_input}\n\n"
            f"## TARGET\n\n{raw_target}\n\n"
            f"## RESTRICTIONS\n\n{raw_restrictions}\n"
        )
        try:
            return analyzer.analyze_text(temp_text)
        except Exception as exc:
            logger.warning("Protocol analysis failed for raw sections: %s", exc)
            return None

    def analyze_refined(self, refined_md: str) -> ProtocolAnalysis | None:
        """Analyze a refined protocol markdown string.
        Returns None if the text cannot be parsed.
        """
        analyzer = ProtocolAnalyzer()
        try:
            return analyzer.analyze_text(refined_md)
        except Exception as exc:
            logger.warning("Protocol analysis failed for refined markdown: %s", exc)
            return None
