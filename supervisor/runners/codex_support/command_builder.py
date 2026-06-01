from __future__ import annotations

import logging
from pathlib import Path

from supervisor.prompts.commands import BREVITY_COMMAND
from supervisor.utils.text_utils import coerce_str, quote_prompt

logger = logging.getLogger(__name__)

_DOT_MODEL_FILE = Path(__file__).resolve().parents[2] / ".codex_model"

# Codex sandboxes file/command access via OS primitives (Seatbelt on macOS,
# Landlock on Linux). That sandbox is NOT available on Windows, so the only
# uniform cross-platform way to grant the agent full autonomy — matching
# opencode's auto-approve `run` mode — is to bypass approvals and sandbox.
# The supervisor already runs the agent inside an isolated, archived workspace
# copy, so the risk profile matches opencode's existing autoapprove behaviour.
_CODEX_AUTONOMY_FLAGS = [
    "--dangerously-bypass-approvals-and-sandbox",
    "--skip-git-repo-check",
    "--color",
    "never",
]

# Disable codex's `multi_agent` namespace tool for every supervised run: it
# keeps the agent single-threaded under the supervisor's control AND maximizes
# external-endpoint compatibility (minimal OpenAI-compatible Responses servers
# reject the namespace tool type — see LM Studio).
_CODEX_CONFIG_FLAGS = ["-c", "features.multi_agent=false"]

# Id + api-key env var for the ephemeral external provider the app injects via
# `-c` overrides when an external base_url is configured. Kept stable so the
# process layer can set the matching env var on the subprocess.
CODEX_PROVIDER_ID = "super_opencode"
CODEX_API_KEY_ENV = "SUPER_OPENCODE_CODEX_KEY"


def external_provider_flags(base_url: str, api_key: str) -> list[str]:
    """`-c` overrides defining an ephemeral OpenAI-compatible codex provider.

    Values are passed bare (no surrounding quotes): codex parses each `-c`
    value as TOML and falls back to the raw string when that fails, which
    sidesteps Windows shell quoting for URLs/identifiers. The api key itself is
    never placed on the command line — codex reads it from ``env_key`` (an env
    var the process layer sets), so it never leaks into process listings.
    """
    base_url = coerce_str(base_url, "base_url (codex provider)")
    if not base_url:
        return []
    pid = CODEX_PROVIDER_ID
    flags = [
        "-c", f"model_providers.{pid}.name={pid}",
        "-c", f"model_providers.{pid}.base_url={base_url}",
        "-c", f"model_providers.{pid}.wire_api=responses",
    ]
    if coerce_str(api_key, "api_key (codex provider)"):
        flags += ["-c", f"model_providers.{pid}.env_key={CODEX_API_KEY_ENV}"]
    flags += ["-c", f"model_provider={pid}"]
    return flags


def validate_message(message: str, context: str = "message") -> str | None:
    """Return cleaned message or None when empty after coercion."""
    message = coerce_str(message, context)
    if not message:
        logger.warning(
            "Empty message provided to codex (%s). Returning None to trigger graceful handling.",
            context,
        )
        return None
    return message


def fresh_session_prompt(prompt: str) -> str:
    """Inline brevity rules into the first codex prompt of a session."""
    return f"{BREVITY_COMMAND.strip()}\n\n{prompt}"


def build_cmd(
    *,
    exe: str,
    prompt: str,
    agent: str,
    codex_model: str | None,
    use_continue: bool,
    session_id: str | None,
    model: str | None = None,
    use_shell: bool = False,
    base_url: str = "",
    api_key: str = "",
) -> list[str]:
    """Build a ``codex exec`` CLI command list.

    Fresh run:        ``codex exec <flags> [-m MODEL] -- "<prompt>"``
    Continuation:     ``codex exec resume <id|--last> <flags> [-m MODEL] -- "<prompt>"``

    ``agent`` is accepted for signature parity with opencode but codex has no
    per-agent concept; plan/build behaviour is steered through the prompt text
    instead (see ``CodexRunner``).
    """
    exe = coerce_str(exe, "exe (codex build_cmd)")
    prompt = coerce_str(prompt, "prompt (codex build_cmd)")
    agent = coerce_str(agent, "agent (codex build_cmd)")

    raw_model_arg = coerce_str(model, "model arg (codex build_cmd)")
    raw_self_model = coerce_str(codex_model, "codex_model (codex build_cmd)")
    resolved_model = raw_model_arg or raw_self_model

    if not resolved_model and _DOT_MODEL_FILE.exists():
        resolved_model = _DOT_MODEL_FILE.read_text(encoding="utf-8").strip()
        logger.debug("Model resolved from .codex_model file: %r", resolved_model)

    logger.debug(
        "codex build_cmd - exe=%r agent=%r use_continue=%s session_id=%r "
        "model_arg=%r self_model=%r resolved_model=%r prompt_len=%d",
        exe,
        agent,
        use_continue,
        session_id,
        raw_model_arg,
        raw_self_model,
        resolved_model,
        len(prompt),
    )

    cmd: list[str] = [exe, "exec"]

    # IMPORTANT: codex's `resume` subcommand only accepts a small option set
    # (it rejects `--model` / `--color` / `-c`-after-resume in older parses).
    # All exec-level options must therefore be placed BEFORE the `resume`
    # token; after `resume` only the session selector and the prompt go.
    cmd += _CODEX_AUTONOMY_FLAGS
    cmd += _CODEX_CONFIG_FLAGS
    cmd += external_provider_flags(base_url, api_key)

    if resolved_model:
        cmd += ["--model", resolved_model]

    if use_continue:
        cmd.append("resume")
        # Prefer an explicit session id (captured from prior output) so
        # concurrent codex runs never cross sessions; fall back to --last.
        cmd.append(session_id if session_id else "--last")

    cmd.append("--")
    cmd.append(quote_prompt(prompt) if use_shell else prompt)

    return cmd
