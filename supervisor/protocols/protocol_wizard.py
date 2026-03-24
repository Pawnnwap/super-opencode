"""
supervisor/protocol_wizard.py

Interactively refines the three protocol sections with the LLM
and returns a polished Protocol object + the markdown string.

Used by the Streamlit UI (wizard_page.py).
"""

from __future__ import annotations

from openai import OpenAI

from supervisor.protocols.protocol import Protocol, parse_protocol_text
from supervisor.protocols.protocol_analyzer import ProtocolAnalyzer, ProtocolAnalysis

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

    def _chat(self, user_msg: str) -> str:
        kwargs = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _WIZARD_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        }
        if not self._model.startswith(("o1", "o3")):
            kwargs["temperature"] = 0.3

        response = self._client.chat.completions.create(**kwargs)
        return response.choices[0].message.content.strip()

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

        refined_md = self._chat(user_msg)
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
        return self._chat(user_msg)

    def analyze_sections(
        self,
        raw_input: str,
        raw_target: str,
        raw_restrictions: str,
    ) -> ProtocolAnalysis | None:
        """
        Analyze raw protocol sections and return quality feedback.
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
        except Exception:
            return None

    def analyze_refined(self, refined_md: str) -> ProtocolAnalysis | None:
        """
        Analyze a refined protocol markdown string.
        Returns None if the text cannot be parsed.
        """
        analyzer = ProtocolAnalyzer()
        try:
            return analyzer.analyze_text(refined_md)
        except Exception:
            return None
