"""supervisor/config.py — immutable run configuration."""

from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class SupervisorConfig:
    protocol_path: Path
    workspace: Path
    max_retries: int = 3
    context_threshold: float = 0.60
    # Execution agent backend: "opencode" (default), "codex", or "pi". All
    # are open-source coding CLIs and work on Windows + Linux. The
    # opencode_model / opencode_executable / opencode_model_backup fields
    # below are reused generically as the chosen agent's model / executable /
    # fallback model.
    engine: str = "opencode"
    opencode_model: str | None = None
    opencode_executable: str = ""
    # Run opencode with --pure (no user plugins/skills). Supervised runs stay
    # isolated from global agent customization that could hijack the task;
    # leave off to keep the user's plugin environment.
    opencode_pure: bool = False
    supervisor_model: str = "swift-1.5"
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
    # Job-log detail mode: "headline" (default) condenses large blocks
    # (prompts/outputs/verdicts/suggestions) to one key line each with the
    # full body in the job's .full sidecar; "full" restores verbatim events.
    # Error events are always written in full either way.
    log_detail: str = "headline"
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
    # current turn. The default keeps only the most recent round verbatim —
    # earlier rounds remain available as compact digests for continuity.
    # history_verbatim_turns=0 disables the verbatim tier.
    history_verbatim_turns: int = 1
    history_budget_fraction: float = 0.35
    # Pre-execution feasibility gate (structural design item 5): optional path to
    # a JSON recording known gate ceilings / blocked gates (empty = fall back to
    # <workspace>/feasibility_facts.json when present). It sits outside logs/
    # because that directory is archived at every fresh cycle, which used to
    # leave each relaunch with an empty evidence base. Rendered as a tiny
    # bounded block for both the coding agent and the judge, so an empirically
    # unreachable done-condition is surfaced up front instead of being
    # rediscovered over 15-20 pre-registered attempts.
    feasibility_facts_path: str = ""
    max_feasibility_block_chars: int = 2200
    # Judge-context budget (structural design item 4): when the same judge input
    # repeats byte-for-byte (typical of repeated stand-by restatements), replay
    # the stored verdict without another LLM call.
    reuse_identical_verdicts: bool = True
    plan_mode_rounds: int = 0
    enable_python_scanner: bool = True
    enable_occam_razor: bool = False
    # Goal guard (book ch.8 exit validation): treat the judge's DONE as a
    # proposal validated against deterministic evidence before the run ends.
    enable_goal_guard: bool = True
    # Blocked DONE proposals tolerated before the guard accepts an unverified
    # completion instead of looping forever (Claude-Code Stop-hook cap).
    max_blocked_stops: int = 2
    # BLOCKED exit: a run whose done-condition is demonstrably unreachable ends
    # BLOCKED (terminal, exit code 2) instead of re-judging the same unmet
    # criteria forever. max_blocked_stops only caps *blocked DONE proposals*,
    # which requires all_targets_met to be true first - so a target the judge
    # refuses to mark MET (the ceiling case: 4 runs at 28h+, 164 judge turns on
    # a study whose gates peak at 7/10) had no exit at all.
    enable_blocked_exit: bool = True
    # Consecutive judged turns with an identical unmet-criteria set AND no
    # workspace change before the run is declared blocked.
    blocked_after_stagnant_verdicts: int = 6
    # Turn floor: never declare blocked this early, the target may be slow-starting.
    min_turns_before_blocked: int = 12
    # Recorded feasibility facts already say the target is unreachable: this
    # many consecutive judged turns with no passing-gate improvement is enough
    # on its own (deterministic path, no judge involvement).
    blocked_after_unreachable_turns: int = 4
    # First-blocked rescue: when jev is enabled, the first blocked verdict is
    # not final. The judge must research the blocker on the web and write a
    # concrete recovery plan; jev rules whether it is feasible. A feasible plan
    # cancels the blocked exit and becomes the executor's next prompt. Any
    # failure (judge call, web search, jev outage) falls through to BLOCKED.
    # max_blocked_rescues=0 disables the second chance.
    enable_blocked_rescue: bool = True
    max_blocked_rescues: int = 1
    # Require observable workspace file changes before accepting completion
    # (disable for pure-analysis goals that legitimately change no files).
    goal_require_changes: bool = True
    # Archive retention: keep only the newest N .archive/ snapshots when a new
    # one is written (0 = unlimited). The archiver is supervisor infrastructure
    # for rollback — pruning is its job, never the agent's; without a cap the
    # snapshots grow unbounded (a 49 GB .archive once pushed the agent into
    # deleting the whole directory on its own initiative).
    archive_keep_last: int = 5
    # Archive backend: "git" = incremental snapshots (orphan commits + tags in
    # .archive/snapshots.git, unchanged files shared by hash); "copy" = the
    # legacy full-copy snapshot directories. git falls back to copy when the
    # git executable is missing or fails twice in a row.
    archive_backend: str = "git"
    # Additionally run the workspace test suite as the DONE gate.
    goal_verify_tests: bool = False
    # Judge evidence harness: auto-inject workspace facts into judge prompts
    # and resolve the judge's NEED_EVIDENCE resource requests (open registry).
    enable_judge_harness: bool = True
    # Supervisor skill bank: a catalog of supervisor-special skills is shown
    # to the judge, which may load bodies on demand via LOAD_SKILL (bounded,
    # judge-context only — the supervisor stays a plain API-call judge).
    # supervisor_skills_dir: optional extra bank directory beyond the
    # built-in bank and <workspace>/.opencode/supervisor_skills.
    enable_supervisor_skills: bool = True
    supervisor_skills_dir: str = ""
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

    # Structured decision service (jev), opt-in. It arbitrates bounded
    # supervisor choices whose only evidence is textual and ambiguous - the
    # closing decision first. Deterministic evidence (gate counts, file
    # existence, tests, protected paths, freshness) stays hard and is never
    # delegated. Empty URL or enable=False keeps the legacy heuristics.
    enable_jev_decisions: bool = False
    jev_decision_url: str = ""
    jev_decision_api_key: str = ""
    jev_decision_timeout: float = 20.0
    # P(close) at or above this is required before the arbiter may close.
    jev_decision_threshold: float = 0.80

    def to_state_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["protocol_path"] = str(self.protocol_path)
        data["workspace"] = str(self.workspace)
        data["protected_files"] = list(self.protected_files)
        return data
