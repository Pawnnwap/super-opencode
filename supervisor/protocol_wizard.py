"""
supervisor/protocol_wizard.py

Interactively refines the three protocol sections with the LLM
and returns a polished Protocol object + the markdown string.

Used by the Streamlit UI (wizard_page.py).
"""

from __future__ import annotations

from openai import OpenAI

from .protocol import Protocol, parse_protocol_text

_WIZARD_SYSTEM = """\
You are a technical project-management assistant.
Your job is to help a user write a clean, unambiguous protocol.md
for an autonomous coding agent called opencode.

The protocol has exactly three sections:

  ## INPUT        — what already exists / what the agent is given
  ## TARGET       — numbered, testable deliverables the agent must produce
  ## RESTRICTIONS — hard rules the agent must never violate

When the user gives you raw notes for any section, you must:
1. Rewrite them in precise, imperative language.
2. Make deliverables concrete and testable (good: "All pytest tests pass";
   bad: "the code should work").
3. Keep restrictions as clear prohibitions ("Do not …").
4. Return ONLY the full protocol.md content, no preamble, no commentary.
   The file must start with the three headings in order.
"""


class ProtocolWizard:
    """
    Drives a guided conversation to produce a refined protocol.md.
    """

    def __init__(self, model: str = "gpt-4o"):
        self._client = OpenAI()
        self._model = model

    # ------------------------------------------------------------------ #
    # One-shot refinement (used by the Streamlit form)                    #
    # ------------------------------------------------------------------ #

    def refine(
        self,
        raw_input: str,
        raw_target: str,
        raw_restrictions: str,
    ) -> tuple[str, Protocol]:
        """
        Send all three raw sections to the LLM in one shot.
        Returns (refined_markdown, Protocol).
        """
        user_msg = (
            "Please refine the following raw protocol notes into a clean protocol.md.\n\n"
            f"### INPUT (raw)\n{raw_input}\n\n"
            f"### TARGET (raw)\n{raw_target}\n\n"
            f"### RESTRICTIONS (raw)\n{raw_restrictions}"
        )

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _WIZARD_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
        )
        refined_md = response.choices[0].message.content.strip()
        protocol = parse_protocol_text(refined_md)
        return refined_md, protocol

    def refine_section(
        self,
        section_name: str,
        raw_text: str,
        existing_context: str = "",
    ) -> str:
        """
        Refine a single section in isolation (used for the iterative wizard).
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
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _WIZARD_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
