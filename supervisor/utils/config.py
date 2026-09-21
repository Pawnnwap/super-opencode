"""supervisor/config.py — immutable run configuration."""

from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class SupervisorConfig:
    protocol_path: Path
    workspace: Path
    max_retries: int = 3
    context_threshold: float = 0.60
    # Execution agent backend: "opencode" (default) or "codex". Both are
    # open-source coding CLIs and work on Windows + Linux. The opencode_model
    # / opencode_executable / opencode_model_backup fields below are reused
    # generically as the chosen agent's model / executable / fallback model.
    engine: str = "opencode"
    opencode_model: str | None = None
    opencode_executable: str = ""
    # Run opencode with --pure (no user plugins/skills). Supervised runs stay
    # isolated from global agent customization that could hijack the task;
    # leave off to keep the user's plugin environment.
    opencode_pure: bool = False
    # When available, run native OpenAI/Anthropic OpenCode providers through
    # a process-local Headroom proxy. Unsupported custom providers stay direct
    # unless explicitly allowed below, so savings setup cannot break them.
    enable_headroom: bool = True
    headroom_executable: str = ""
    headroom_allow_custom_provider: bool = False
    supervisor_model: str = "gpt-4o"
    supervisor_model_backup: str | None = None
    # Thinking/reasoning control for real runs ("" = model default).
    # supervisor_reasoning: passed to the supervisor's chat.completions calls
    # as extra_body.reasoning_effort. agent_reasoning: opencode `--variant`
    # or codex `-c model_reasoning_effort`, depending on engine. Values must
    # come from the model's own variant names (the UI droplist aligns them);
    # connectivity probes ignore these and always probe at the lowest effort.
    supervisor_reasoning: str = ""
    agent_reasoning: str = ""
    opencode_model_backup: str | None = None
    timeout: int = 300
    log_level: str = "INFO"
    protected_files: tuple[str, ...] = ()
    read_external_feedback: bool = False
    max_tokens: int = 128_000
    max_protected_files_for_suggestions: int = 5
    truncation_enabled: bool = True
    max_history_turns: int = 40
    compact_intermediate_steps: bool = False
    # Supervisor context shaping: recent turns stay verbatim, older turns are
    # condensed to their verdict digest, and the whole history is capped at a
    # share of max_tokens so already-judged output cannot crowd out the
    # current turn. history_verbatim_turns=0 disables the verbatim tier.
    history_verbatim_turns: int = 4
    history_budget_fraction: float = 0.35
    plan_mode_rounds: int = 0
    enable_python_scanner: bool = True
    enable_occam_razor: bool = False
    # Goal guard (book ch.8 exit validation): treat the judge's DONE as a
    # proposal validated against deterministic evidence before the run ends.
    enable_goal_guard: bool = True
    # Blocked DONE proposals tolerated before the guard accepts an unverified
    # completion instead of looping forever (Claude-Code Stop-hook cap).
    max_blocked_stops: int = 2
    # Require observable workspace file changes before accepting completion
    # (disable for pure-analysis goals that legitimately change no files).
    goal_require_changes: bool = True
    # Additionally run the workspace test suite as the DONE gate.
    goal_verify_tests: bool = False
    # Judge evidence harness: auto-inject workspace facts into judge prompts
    # and resolve the judge's NEED_EVIDENCE resource requests (open registry).
    enable_judge_harness: bool = True
    # OpenAI credentials captured at enqueue time so the running loop is not
    # affected by later UI changes to os.environ. When empty, downstream
    # clients fall back to the env vars (OPENAI_API_KEY / OPENAI_BASE_URL).
    openai_api_key: str = ""
    openai_base_url: str = ""
    # External OpenAI-compatible API for the codex engine (the equivalent of
    # opencode's custom provider). When codex_base_url is set, the runner
    # injects an ephemeral codex provider via `-c` overrides pointed at this
    # endpoint (wire_api=responses) and feeds codex_api_key through an env var.
    # The endpoint must support the OpenAI Responses API (/v1/responses).
    # Empty -> codex uses whatever provider its own ~/.codex/config.toml selects.
    codex_base_url: str = ""
    codex_api_key: str = ""

    def to_state_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["protocol_path"] = str(self.protocol_path)
        data["workspace"] = str(self.workspace)
        data["protected_files"] = list(self.protected_files)
        return data
