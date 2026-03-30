"""
supervisor/meta_protocol_builder.py

Generates a protocol.md targeted at self-evolution:
  - INPUT  is derived automatically from the live codebase snapshot
  - TARGET is derived from the user's evolution goal
  - RESTRICTIONS are hardened defaults for safe self-modification,
    optionally extended by the user

The resulting protocol is written to <workspace>/meta_protocol.md and
is what the self-evolution loop feeds to opencode.
"""

from __future__ import annotations

from pathlib import Path

from openai import OpenAI

from supervisor.analyzers.codebase_analyzer import CodebaseSnapshot

_BUILDER_SYSTEM = """\
You are a technical architect writing a protocol.md for an autonomous
coding agent that will modify the very codebase it lives in.

The protocol has exactly three sections in this order:
  ## INPUT        — factual description of what currently exists
  ## TARGET       — numbered, testable, concrete deliverables
  ## RESTRICTIONS — hard rules for safe self-modification

Rules for a good self-evolution protocol:
- INPUT must accurately describe the current code (modules, entry points,
  key classes).  Do NOT invent files that don't exist.
- TARGET items must be independently verifiable (test passes, behaviour
  observable, file exists with specific content).
- RESTRICTIONS must include at minimum:
    * Do not delete or rename core modules without migrating all imports.
    * Every change must leave the codebase in a runnable state.
    * Do not modify .archives/ or any archive files.
    * Preserve backward compatibility of the SupervisorLoop.run_streaming() API.
- Output ONLY the protocol.md content — no preamble, no code fences.
"""


class MetaProtocolBuilder:
    def __init__(self, model: str = "gpt-4o"):
        self._client = OpenAI()
        self._model = model

    def build(
        self,
        evolution_goal: str,
        snapshot: CodebaseSnapshot,
        extra_restrictions: str = "",
    ) -> str:
        """
        Return a refined meta_protocol.md string.
        """
        digest = snapshot.digest_for_prompt(max_files=20)

        user_msg = (
            f"## Evolution goal\n{evolution_goal}\n\n"
            f"## Extra restrictions from user\n"
            f"{extra_restrictions if extra_restrictions.strip() else '(none)'}\n\n"
            f"{digest}"
        )

        kwargs = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _BUILDER_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
        }
        if not self._model.startswith(("o1", "o3")):
            kwargs["temperature"] = 0.2

        response = self._client.chat.completions.create(**kwargs)
        return response.choices[0].message.content.strip()


def write_meta_protocol(content: str, workspace: Path) -> Path:
    path = workspace / "meta_protocol.md"
    path.write_text(content, encoding="utf-8")
    return path
