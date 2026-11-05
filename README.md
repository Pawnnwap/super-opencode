# opencode-supervisor

> **A self-evolving autonomous coding agent — it supervises, judges, and improves itself.**

---

[English](./README.md) | [中文](./README_zh.md)

A NiceGUI-driven, dual-loop autonomous coding system. `SupervisorLoop` drives protocol-guided
task execution with an LLM-based judge (`LLMSupervisor`) that evaluates every iteration for
protocol alignment. `SelfEvolutionLoop` turns the same machinery on the codebase itself —
running test-gated self-improvement with automatic regression rollback. File changes use each
execution backend's native read, edit, patch, and write tools.

Key capabilities:

- **Dual-loop architecture** — `SupervisorLoop` for external tasks, `SelfEvolutionLoop` for self-improvement
- **Triple execution backend** — OpenCode (default), Codex, or pi, selected via `config.engine`; the supervisor loop is backend-agnostic
- **Evidence-grounded judge** — `LLMSupervisor` evaluates agent output against protocol targets at every step, grounded in deterministic workspace facts via the `JudgeHarness` evidence registry
- **Goal guard** — Evidence-gated completion: "all targets met" is a proposal, validated against deterministic evidence
- **Loop & stagnation detection** — Intra-turn tool-loop detection plus cross-turn workspace-fingerprint stagnation with a nudge ladder
- **Native file editing** — OpenCode, Codex, and pi use their maintained read, edit, patch, and write tools
- **Web UI** — NiceGUI dashboard: Protocol Wizard, Live Run, and Self-Evolution pages, with a persisted background-job board and full-page live job screens
- **Plan mode** — Configurable pre-execution planning rounds with read-only opencode analysis
- **Occam's razor pass** — Optional post-success redundancy-reduction stage that runs on an archive copy, never the live workspace
- **Task-state journal** — Append-only `TASK_STATE.md` progress journal re-injected after context resets
- **Headline job logs** — every event persists as one key line; large blocks (prompts / outputs / verdicts / suggestions) keep their full bodies in a per-job sidecar (`.job_store/<job>.full`), and error events are always written in full
- **Parallel study batches** — launch N isolated instances of one protocol from the Run page (workspace clones or protocol-only seeds); the board groups them into one batch card with one-click cancel, under a configurable concurrency cap
- **Incremental workspace archiving** — `.archive/` snapshots are git object stores (one orphan commit per snapshot): unchanged files are shared by hash and retention actually frees space, with automatic full-copy fallback when git is unavailable
- **Vulnerability scanning** — 9-tool static analysis pipeline (Bandit, Semgrep, Ruff, etc.)
- **Context management** — Token-aware monitoring with graduated warnings and auto-compaction
- **Feasibility gate** - optional `feasibility_facts.json` (workspace root, outside the archived `logs/`) records known gate ceilings and blocked gates; an unreachable done-condition is surfaced up front instead of being rediscovered over many pre-registered attempts. The Live Run form offers **Generate draft**: the supervisor model transcribes gates from `protocol.md` and ceilings evidenced in prior-attempt artifacts (`status.json`, `REPORT.md`, …) into the form for review — it never invents numbers
- **Judge-context budget** - tiered history compaction plus identical-input verdict reuse: a byte-identical judge input replays the stored verdict without another LLM call

---

## Prerequisites

- Python 3.11 or higher
- An API key for OpenAI or any compatible provider (e.g., NVIDIA NIM, Ollama)
- opencode CLI installed (see installation below) — or the Codex CLI / pi CLI
  if you select the Codex / pi execution backend
  (pi: `npm install -g @earendil-works/pi-coding-agent`)

---

## Installing opencode

### Installing Node.js / npm

opencode is distributed as an npm package. If you don't have Node.js installed, download
and install the LTS release from the [official Node.js site](https://nodejs.org/).
This provides the `npm` command on every supported platform (Windows, macOS, Linux).

Verify the install:

```bash
node --version
npm --version
```

### Installing opencode

**Windows (npm):**

```bash
npm install -g opencode-ai
```

**macOS / Linux (curl):**

```bash
curl -fsSL https://opencode.ai/install | bash
```

The `opencode` executable will be placed on your PATH:

- **Windows:** `%AppData%\npm\opencode.cmd`  (typically `C:\Users\<you>\AppData\Roaming\npm\opencode.cmd`)
- **macOS / Linux:** `~/.opencode/bin/opencode`

Run `where opencode` (Windows) or `which opencode` (macOS/Linux) to see the resolved
path. To override auto-detection, set `"opencode_executable"` in
`~/.opencode_supervisor_settings.json`; leave it unset and the supervisor locates
the CLI automatically.

### Upgrading opencode

**Windows:**

```bash
npm install -g opencode-ai@latest
```

**macOS / Linux:**

```bash
curl -fsSL https://opencode.ai/install | bash
```

The app also runs these commands automatically on startup (disable via
`OPENCODE_SKIP_UPGRADE=1` or `skip_upgrade: true` in `~/.opencode_supervisor_settings.json`).

---

## Supervisor API Configuration

The supervisor accepts any OpenAI-compatible API endpoint. Below are pre-configured free-tier options:

| Provider | Base URL | Model | Notes |
|----------|----------|-------|-------|
| **Google AI Studio** | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemma-4-31b-it` | ~1.5K free requests per day |
| **Nvidia NIM** | `https://integrate.api.nvidia.com/v1` | `nvidia/nemotron-3-super-120b-a12b` | Free tier available |
| **iflow** *(deprecated)* | `https://apis.iflow.cn/v1` | `iflow/qwen3-coder-plus` | Service stops after 17th Apr 2026 |

> **Privacy Warning:** Free API services may use your input data for model training. Be careful with sensitive or proprietary information. Consider switching to paid tiers if privacy is a concern.

---

## Setup

### 1. Create a Virtual Environment

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

**macOS/Linux:**
```bash
python -m venv venv
source venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -e . --force-reinstall
```

> **Note:** `pip install -e .` is required so that the `supervisor` package is
> importable from anywhere — including when the app launches `app.py` from
> a different working directory.

Alternatively, you can install from `requirements.txt` directly:

```bash
pip install -r requirements.txt
```

Core dependencies are defined in `pyproject.toml`: `openai`, `nicegui`,
`tiktoken`, `pytest`, `rich`, `psutil`, and `mcp`. The
`requirements.txt` file additionally pins the scanner toolchain (Bandit,
Pylint, pyflakes, Semgrep, pip-audit, Ruff, Vulture, deadcode, pyscn) and the
auto-fix helpers (autoflake, isort, autopep8, pyupgrade).

> **MCP server:** The `mcp` package is required for the Codehelp MCP server
> (`mcp_server/codehelp.py`), which provides code-assistance tools to OpenCode and Codex.
---

## Running the Application

```powershell
.\start_suprun.ps1                # proxy (127.0.0.1:8321) + app (127.0.0.1:8501)
.\start_suprun.ps1 -Upstream http://127.0.0.1:8111   # model endpoint (default)
.\start_suprun.ps1 -Restart       # relaunch the app; -ProxyOnly / -AppOnly scope the run
```

`start_suprun.ps1` is idempotent: pieces already listening are left
running, and a proxy whose live upstream differs from `-Upstream` is
restarted so the parameter always wins. Equivalent manual start:
`python app.py` (then the app serves `http://127.0.0.1:8501`).

The job worker watches its own source tree (`supervisor/`, `services/`). When
the tree changes, it stops claiming new jobs, lets live jobs finish on the
code they started with, then re-execs to load the new code — so a deployed
fix applies without a manual worker restart, and a stale daemon can never
silently run a future job.

---

## What the Supervisor Does

The supervisor system runs the opencode agent in a controlled feedback loop:

1. **Protocol-driven execution** — A `protocol.md` file defines INPUT, TARGET,
   and RESTRICTIONS that guide the agent's behavior
2. **Real-time monitoring** — Context window usage is tracked and compaction
   triggers automatically when needed
3. **Workspace safety** — The system blocks out-of-workspace path references to
   prevent unintended modifications
4. **Checkpointing** — Every successful iteration is snapshotted to `.checkpoints/`
5. **Workspace archiving** — Workspace state is preserved in `.archive/` before
   each run and after each iteration
6. **Self-evolution** — The system can analyze and improve its own codebase,
   running tests before and after each change with automatic rollback on regression
7. **Goal guard** — Completion claims are verified against deterministic
   evidence before the run ends (see below)
8. **Loop detection** — Repeated tool calls within one turn are detected and
   the run is killed and restarted with fixed context instead of burning tokens;
   cross-turn workspace stagnation escalates through a nudge ladder
9. **Occam's razor pass** — After success, an optional stage lets the agent strip
   redundant code from an archived copy of the final workspace
10. **Task-state journal** — A `TASK_STATE.md` progress journal preserves
   "what was already tried" across context resets

---

## Goal Guard (Evidence-Gated Completion)

The judge's "all targets met" is treated as a *proposal*, not an acceptance.
Before a run may end, the goal guard validates it — following the
propose → validate → execute → audit discipline (工程本体论 ch. 8):

- **Evidence-grounded judging** — The judge does not rely on the agent's
  prose alone: the `JudgeHarness` registry renders cheap deterministic
  evidence into every judge prompt (changed files, workspace tree,
  audit-trail tail), and the judge can request expensive evidence on demand
  (`NEED_EVIDENCE: <name>`, e.g. a test run or file read), which the loop
  resolves once before re-judging.
- **Structured verdicts** — The judge ends every reply with a per-target
  checklist (`[MET]`/`[UNMET]` + evidence), a `NEXT_ACTION` instruction that is
  forwarded to the agent, and a `DONE: yes|no` flag. Free-text replies still
  work via the legacy done-phrase matching.
- **Exit validation** — A DONE proposal is blocked when any criterion is still
  `[UNMET]`, or when the run produced no observable workspace changes (the
  agent's own claims are never sufficient evidence).
- **Blocked-stop cap** — After `max_blocked_stops` blocked proposals, the guard
  accepts the completion and flags it as unverified, so the loop can neither
  stop prematurely nor run forever.
- **Stagnation nudges** — When the workspace fingerprint stays unchanged across
  judged turns, the agent first gets a nudge, then a forced strategy-change
  demand, then a loop-restart — legitimate read/test phases are not punished
  because only the workspace fingerprint counts.
- **Audit trail** — Every verdict and decision is appended to
  `.opencode/goal_audit.jsonl` (timestamp, criteria, evidence, decision,
  reason) so a run can be replayed and audited after the fact.
- **Progress memory** — Direction tracking and Reflexion-style lessons persist
  in `.opencode/target_state.json` and are re-injected into judge and restart
  prompts, so guidance survives restarts.

Configuration (`SupervisorConfig`): `enable_goal_guard` (default `True`),
`max_blocked_stops` (default `2`), `goal_require_changes` (default `True`;
disable for pure-analysis goals), `goal_verify_tests` (default `False`; run the
workspace test suite as the final gate).

---

## Loop & Stagnation Detection

Two pure, LLM-free detectors keep runs honest:

- **Intra-turn (`LoopDetector`)** — watches live tool markers inside one
  streamed turn. Same-tool repetition (including args), small tool cycles, or
  tool storms without prose trigger a kill-and-resume with fixed context
  instead of burning the whole turn on a loop.
- **Cross-turn (`StagnationDetector`)** — compares workspace fingerprints
  (file-content hashes) across judged turns. A busy agent whose workspace
  never changes escalates through nudge → forced strategy change → loop
  restart, so legitimate read/test phases are not punished (only the
  workspace fingerprint counts).

### BLOCKED exit (unreachable target)

Every escape hatch above still required `all_targets_met` to be true first —
so a target the judge refuses to mark MET (a study whose gates peak at 7/10,
say) had no exit at all: the loop re-judged the identical unmet criteria
forever. Those runs now close as **BLOCKED** — terminal, exit code 2, amber
on the board — instead of FAILED, because the run is not broken, the target
is unreachable under recorded constraints (`enable_blocked_exit`, default
on). Four evidence-bound triggers, all behind a turn floor
(`min_turns_before_blocked`, default 12):

- **attainment stagnation** — `blocked_after_stagnant_verdicts` (6)
  consecutive judged turns with no obligation newly MET and no gate newly
  passed. Keyed on attainment, never on workspace change: an agent that
  deletes a backup or rewrites `summary.md` every turn makes a diff forever;
- **stand-down directive** — the judge's `NEXT_ACTION` says "None / stand
  down / await the supervisor" for the same 6 turns while targets stay
  UNMET. The loop is waiting on an external decision and cannot produce one;
- **agent-declared closure** — a terminal event in
  `.opencode/goal_audit.jsonl` (`goal_terminal`, `*_closed_*`), or an
  explicit ruling line in `.opencode/closure.md` / `ESCALATION.md`
  (`TERMINAL RULING ... CLOSED`, written during this run). The ruling is
  cross-checked against recorded feasibility facts before a *permanent*
  `closed` is written;
- **recorded unreachable facts** — the feasibility facts declare the target
  unreachable and the passing-gate count has stopped improving for
  `blocked_after_unreachable_turns` (4) turns.

A blocked run writes `blocked_report.md` with the reason and lands as a
BLOCKED job on the board; the operator, not the loop, chooses the next move.
The durable state is written too: `closed` when the closure is supported (or
harness-measured), `blocked` when the agent asserted it but the facts hold
measured evidence of reachability (a passing gate or a ceiling that meets the
bar) — never left at `running`. An all-open facts file is neutral, not
evidence, and does not override a terminal ruling.

---

## Occam's Razor Pass

After a run succeeds, an optional post-success stage (`enable_occam_razor`,
default off) lets the agent strip redundant code and logic. It never edits the
live workspace: the final code is copied into an archive-owned workspace, and
opencode reduces only redundant code/logic inside that copy.

---

## Task-State Journal

Compaction writes a single `summary.md` snapshot and restarts the agent in a
fresh session, so long tasks crossing several resets lose the thread of what
was already tried. The supervisor therefore appends one compact entry per
judged turn to `TASK_STATE.md` in the workspace (turn number, time, phase, and
a one-line headline). On a context reset, the journal tail is injected into
the restart prompt so the agent resumes with continuity.

---

## Web UI Pages

Three NiceGUI pages, served at `/` and `/wizard` (Protocol Wizard), `/run`,
and `/evolve`. Launching a task or an evolution enqueues a background job via
the `JobManager` — job state and logs persist under `.job_store/`, so runs
continue independently of the browser. The left drawer shows live queue
stats and locks Run/Evolve until the connectivity tests pass.

Job logs are headline-sized by default: each event persists as one key
line, full bodies of large blocks go to `<job_id>.full` sidecar files
(retrievable per event via the live screen's **查看全文**), error events
are always written in full, and repeated warnings coalesce into ×N
markers. Terminal jobs beyond the *Stored jobs* cap lose their log
payloads on the next enqueue (state summaries stay).

### ① Protocol Wizard
Fill in INPUT / TARGET / RESTRICTIONS in plain language → click
**Refine with AI** → review the generated `protocol.md` → Accept & Save.

The wizard includes:
- **Configuration panel** — Execution engine (OpenCode, Codex, or pi), API key
  and base URL, workspace path, supervisor and agent models (plus backup
  models), max retries, context threshold, turn timeout, max tokens, npm
  registry for CLI upgrades, and switches for the Python scanner
  and the Occam's razor pass. A **Clean artifacts** button removes run
  leftovers from the workspace.
- **Protected Files** — Mark files that the agent cannot modify or delete
- **.opencodeignore** — Configure ignore patterns for files excluded from
  context retrieval
- **Connectivity Tests** — One-click agent and supervisor connectivity
  checks; Live Run and Self-Evolution stay locked until both pass
- **Live quality analysis** — Real-time scoring of protocol clarity,
  testability, and completeness as you type

### ② Live Run
Start the supervisor loop against any project workspace. Each launch opens a
full-page live job screen (`/run/{job_id}`) that auto-refreshes every 2
seconds; a Task Board on the page lists active and finished runs.

Features:
- Step-by-step progress tracking with a phase breadcrumb
- Plan mode — configurable planning rounds in the launch form (default 1,
  0 disables)
- **Parallel instances** — set *Parallel instances* above 1 to clone the
  workspace N times (or seed protocol-only copies) and launch them as one
  study batch; replicas land under `<workspace>_studies/<batch>_r1..rN`,
  render as a single grouped board card with a **Cancel batch** button,
  and respect the *Max parallel jobs* cap (extras queue as PENDING)
- Token usage bar with color-coded thresholds (green / yellow / red)
- Compact rolling **Latest** strip (newest events) plus the full log panel
  behind an expansion — headline events link to their full bodies via
  *event #* + **查看全文**
- Log search, noise filter, and download
- Stop a running job at any time
- Final report with download button (available after the run completes)

### ③ Self-Evolution
Point the system at **its own source tree**.

1. Describe what you want debugged or improved
2. Optionally add extra restrictions
3. **Generate meta_protocol.md** — LLM reads the live source and writes
   a precise protocol with accurate INPUT and testable TARGETs
4. Review / edit, then **Launch Evolution**

Self-evolution features:

| Feature | Detail |
|---------|--------|
| Test baseline | `pytest` (or syntax check) runs before opencode touches anything |
| Per-iteration tests | Tests re-run after every supervisor judgement |
| Regression guard | Tests worse → auto-rollback to last good checkpoint |
| Checkpointing | Every non-regressing iteration is snapshotted to `.checkpoints/` |
| Workspace archiving | Every iteration is archived to `.archive/` with metadata |
| TARGET state | `.opencode/target_state.json` records evidence, tried directions, and stagnation |
| Rejected candidates | Regression candidates are archived before rollback and appended to `.opencode/staged_improvements.jsonl` |
| Stagnation control | Two rejected/no-evidence iterations require a fresh diagnosis; repeated directions are blocked |
| Evolution report | `evolution_report.md` — changed files, test delta, best checkpoint |

---

## Architecture

```
app.py                              Web UI  (3 pages: Wizard, Live Run, Self-Evolution)
supervisor/
  __init__.py                       Package marker
  memory_policy_evaluator.py        Fixed-suite gate for durable-memory policies

  core/
    loop.py                         SupervisorLoop — main supervised agent loop
    loop_base.py                    BaseLoop — common state machine, event yielding
    self_evolution_loop.py          SelfEvolutionLoop — self-modification with test gating
    goal_guard.py                   Evidence-gated completion (propose → validate → execute → audit)
    occam_razor.py                  Post-success redundancy-reduction pass on an archive copy
    task_state.py                   Append-only TASK_STATE.md journal for restart continuity
    target_evaluator.py             Independent test/evidence acceptance gate
    target_state.py                 Persistent TARGET evidence and staged candidates
    llm_supervisor.py               LLM judge that evaluates agent output
    judge_harness.py                Evidence registry grounding the judge (auto + NEED_EVIDENCE resources)
    llm_support/                    Judge internals: chat, context, history, judgement, models

  analyzers/
    codebase_analyzer.py            Snapshots source tree for LLM context
    opencode_step_detector.py       Detects step progress in agent output
    loop_detector.py                Intra-turn loop detection (repeated tool calls)
    stagnation_detector.py          Cross-turn workspace stagnation with nudge ladder

  protocols/
    protocol.py                     Parse / validate protocol.md (INPUT, TARGET, RESTRICTIONS)
    protocol_wizard.py              OpenAI-SDK protocol refiner
    protocol_analyzer.py            Quality scoring (clarity, testability, completeness)
    meta_protocol_builder.py        Generates meta_protocol.md from evolution goal + snapshot
    target_audit.py                 Deterministic preflight audit of TARGET sections
    alignment.py                    Protocol alignment / violation checks
    analyzer_support.py             Shared analyzer helpers

  runners/
    factory.py                      Constructs OpencodeRunner / CodexRunner / PiRunner from config.engine
    base_runner.py                  Shared runner base: full turn/session/step/archive surface;
                                    engines implement only their seams (_run_prompt, workspace prep)
    opencode_runner.py              Subprocess wrapper for the opencode CLI
    codex_runner.py                 Subprocess wrapper for the Codex CLI
    pi_runner.py                    Subprocess wrapper for the pi CLI
    opencode_support/               opencode specifics: command builder, locator, process,
                                    stream, session, inspection, result
    codex_support/                  codex specifics: command builder, locator, process, stream
    pi_support/                     pi specifics: command builder, locator, process, stream
    command_common.py               Shared command construction helpers
    process_common.py               Shared three-engine subprocess driver (stdin prompt, stream
                                    consumption, abort/loop/timeout handling, model fallback)
    locator_common.py               Shared executable discovery
    process_utils.py                Process lifecycle helpers
    stream_driver.py                Shared JSON-event stream reader (thread + queue, timeout-safe)
    test_runner.py                  Runs pytest / syntax check; structured results

  utils/
    config.py                       Frozen SupervisorConfig dataclass
    experience_tracker.py           Tracks successful patterns across runs
    text_utils.py                   Text processing utilities
    filesystem/                     file_ops, file_permissions, gitignore_utils, path_filters

  prompts/
    __init__.py                     Package exports for prompt templates
    templates.py                    All prompt templates (init and judge instructions)
    commands.py                     Behavioral command blocks (brevity mode, tool rules)

  monitoring/
    token_estimator.py              Token counting (tiktoken) and prompt truncation
    session_tracker.py              Context window usage with graduated warnings, session state and lifecycle tracking

  workspace/
    workspace_guard.py              Blocks out-of-workspace path references
    workspace_archiver.py           Preserves workspace versions in .archive/
    cleanup_candidates.py           Suggests backup/temp files for cleanup
    ignore_patterns.py              .opencodeignore parsing and pattern matching

vulnerability/
  python_scanner.py                 Python code vulnerability scanner (9-tool static analysis)

mcp_server/
  codehelp.py                       MCP server for code assistance and dependency research
  codehelp_support/                 Tool implementations (docstrings, packages, dependencies,
                                    docs, examples, http)

services/
  config/                           Settings persistence, opencode/codex config writers,
                                    connectivity tests, SupervisorConfig builder
  jobs/                             JobManager and StateStore (background jobs, persisted under .job_store/)
  runtime/                          App bootstrap (auto-upgrades) and workspace cleanup
  webui/                            NiceGUI shell: layout/theme, persisted app state, job-manager
                                    singleton, log formatting, startup tasks
    components/                     Task board, live job screen, log panel, progress widgets
    pages/                          wizard, run, evolve pages

checks/                             Integration and end-to-end check scripts
docs/                               Research notes

pyproject.toml                      Makes `supervisor` an installable package (core deps)
requirements.txt                    Alternative dependency list (core + scanner tooling)
```

---

## Protocol System

A `protocol.md` file is the core contract between you and the supervisor.
It must contain exactly three sections:

```markdown
## INPUT

Describe what already exists — files, directories, entry points, current state.

## TARGET

List numbered, testable deliverables the agent must produce.
Good: "All pytest tests in ./tests/ pass"
Bad: "the code should work"

Each TARGET must name a deliverable, acceptance evidence, finished state, and
failure/replan condition. Unmeasured "improve" or "enhance" targets are rejected.

## RESTRICTIONS

Hard rules the agent must never violate.
- Do not touch files outside ./src
- No system package installs
- Keep code under 300 lines
```

### Protocol Quality Analysis

The system analyzes protocol quality across three dimensions:
- **Clarity** — Avoids vague language, uses structured formatting
- **Testability** — Contains measurable acceptance criteria
- **Completeness** — Covers all necessary context and constraints

Quality ratings: `excellent` (≥90%) → `good` (≥75%) → `fair` (≥50%) → `poor`

---

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| API Key | — | Your API key (OpenAI or any compatible provider) |
| Base URL | *(blank = OpenAI)* | Override for local/proxy endpoints e.g. `http://localhost:11434/v1` |
| Workspace path | — | Absolute path to the project directory |
| Supervisor / wizard model | — | Any model string your provider accepts |
| Execution engine | opencode | Agent backend: `opencode`, `codex`, or `pi` |
| Agent model | *(engine default)* | Forwarded to the opencode/codex/pi CLI |
| Max retries | 3 | Consecutive failures before forced stop |
| Context threshold | 60% | Compaction fires at this fraction of estimated max |
| Max tokens | 150,000 | Model context window size |
| Timeout | 120 min | Silence before opencode is deemed unresponsive |
| Protected files | *(empty)* | User-defined files that opencode cannot modify |
| Full job logs (debug) | off | off = headline events (full bodies in the sidecar); on = verbatim events. `SOP_LOG_DETAIL=full` overrides |
| Max parallel jobs | 4 | Concurrent RUNNING jobs; extras queue as PENDING |
| Stored jobs (log retention) | 50 | Terminal jobs beyond this count lose `.logs`/`.full` payloads on the next enqueue; state summaries stay |

### Advanced Configuration (SupervisorConfig)

| Setting | Default | Description |
|---------|---------|-------------|
| truncation_enabled | True | Enable prompt truncation when approaching limits |
| max_history_turns | 40 | Maximum conversation history turns before compaction |
| compact_intermediate_steps | False | Compact intermediate step outputs |
| max_protected_files_for_suggestions | 5 | Max protected files shown in suggestions |
| read_external_feedback | False | Allow external feedback injection |
| log_level | "INFO" | Logging verbosity (DEBUG, INFO, WARNING, ERROR) |
| log_detail | "headline" | Job-log detail: headline digests + `.full` sidecar, or "full" for verbatim events. Errors are always inline in full |
| archive_backend | "git" | "git" = incremental snapshots in `.archive/snapshots.git`; "copy" = legacy full-copy snapshot dirs |
| plan_mode_rounds | 0 | Number of planning rounds before execution (0 = disabled) |
| enable_occam_razor | False | Run the post-success redundancy-reduction pass on an archive copy |

### Custom Models and Providers

The wizard's **Agent model** dropdown is populated by querying the installed
opencode CLI via **Fetch models** / **Refresh models**. Pick any entry from
the list, choose **(custom)** for a free-form model string, and set an
**Agent backup model** as a fallback.

To register a custom OpenAI-compatible provider, add it to opencode's own
configuration file (`~/.config/opencode/opencode.json` on Unix-like systems,
`%APPDATA%\opencode\opencode.json` on Windows) — either by hand or with the
`add_custom_provider_to_config` helper in
`services/config/opencode_config.py`. Once registered, its models are
fetched into the dropdown and referenced as `service-name/model-name`
(e.g., `my-custom-service/qwen3-max`).

---

## Workspace Protection

The supervisor enforces multiple layers of protection:

### System-Protected Directories
- `.opencode/` — Supervisor configuration (auto-created)
- `.checkpoints/` — System checkpoints
- `.archive/` — Version archives

### User-Protected Files
Mark specific files as read-only via the UI. These files are:
- Excluded from opencode's write operations
- Listed in every prompt sent to opencode
- Validated before any modification attempt

### .opencodeignore
Configure a `.opencodeignore` file in your workspace root to exclude files
from context retrieval. Supports:
- Exact filename matches: `debug.py`
- Prefix matches: `prefix*`
- Suffix matches: `*_test.py`
- Glob patterns: `**/*.pyc`
- Directory patterns: `build/`

---

## Context Monitoring

The supervisor tracks token usage with graduated warnings:

| Threshold | Action |
|-----------|--------|
| 50% | Context usage noted |
| 60% | Approaching compaction threshold |
| 70% | Context elevated — monitor closely |
| 80% | Warning — compaction recommended |
| 90% | Critical — immediate compaction required |

Token estimation uses `tiktoken` (o200k_base encoding) when available,
falling back to character-based estimation (4 chars/token).

Automatic compaction triggers at the configured `context_threshold`
(default 60%), prompting opencode to clean up unnecessary files.

---

## Workspace Archiving

Every run preserves workspace state in `.archive/`:

**Git backend (default, `archive_backend: "git"`)** — snapshots live in a
private bare repo at `.archive/snapshots.git`, driven with
`--git-dir/--work-tree` so the workspace's own `.git` is never touched:

- One snapshot = `git add -A` → `write-tree` → an **orphan commit**
  (no parent) → lightweight tag `snap/run_<ts>_<counter>_<slug>`; unchanged
  files are shared across snapshots by content hash, so a snapshot of an
  unchanged workspace costs nothing
- `keep_last` retention deletes the oldest tags and runs `git gc
  --prune=now`, which actually reclaims the dropped snapshots' unique
  objects (no branch pins history)
- Restore overlays the tagged files back (never deletes newer files) —
  the same semantics as the legacy restore; legacy `run_*` directories
  remain listed and restorable
- Exclusions regenerate into the repo's `info/exclude` per snapshot:
  canonical skip dirs, dot-entries, `.opencodeignore`, plus `*.log`
  (rotating prompt logs are not code state — one 64 MB log re-blobbed per
  snapshot is the bloat this backend exists to prevent)
- If git is missing or fails twice in a row, the archiver falls back to
  the full-copy backend for the rest of the process

**Copy backend (`archive_backend: "copy"`)** — the legacy behaviour:
timestamped `run_*` directories with `code/`, `results/`, `logs/`,
`other/` subdirectories and `archive_metadata.json`.

Both: the `.archive/` directory itself is protected from modification, and
version files are never deleted — only archived.

---

## Native File Editing

Super-Opencode uses the execution backend's native file tools. OpenCode offers
`read`, exact-match `edit`, `apply_patch`, and `write`; Codex uses
`apply_patch`; pi offers `read`, exact-match `edit`, `write`, and a `bash`
tool. The supervisor supplies protocol checks, test-gated evolution,
workspace archives, and rollback around those changes.

OpenCode configurations created by earlier versions are migrated on startup:
the managed Hashline MCP entry is removed and native `read` / `edit`
permissions are restored. Custom MCP servers and user-selected permissions are
left unchanged.


---

## Code Assistance MCP Server

The system ships one MCP server (`mcp_server/codehelp.py`, with tool
implementations in `mcp_server/codehelp_support/`) that provides tools for
code assistance and documentation lookup:

- **search_docstrings** — search local codebase for docstrings of packages,
  modules, classes, functions, or methods
- **search_package_version** — query PyPI/npm for latest published versions,
  release dates, and documentation URLs
- **analyze_dependency** — inspect declared and lockfile versions from Python and
  Node manifests, with source locations and compatibility-risk labels
- **fetch_official_docs** — read a short excerpt from public HTTPS documentation
  URL declared by PyPI/npm metadata, with registry/cache provenance
- **search_package_examples** — find Stack Overflow and Real Python usage
  examples and best practices; community fallback only

These tools are automatically configured for OpenCode and Codex on startup.
They provide research only; file editing stays with each backend's native
tools. The pi engine is not auto-configured with Codehelp — pi supervised
runs use its built-in tools only.

## Plan Mode

When `plan_mode_rounds` > 0, the supervisor runs a dedicated planning phase
before execution:

1. A read-only opencode instance analyzes the protocol and workspace
2. The LLM supervisor evaluates the plan (never marks targets as met during
   planning, so the phase always runs the configured number of rounds)
3. The final plan and supervisor feedback are stored and prepended to the
   build-mode initial prompt, giving opencode a clear roadmap before it
   starts writing code

Configure planning rounds in the Live Run launch form — it defaults to
1 round; set it to 0 to disable (`SupervisorConfig.plan_mode_rounds` itself
defaults to 0).

---

## Vulnerability Scanning

The `vulnerability/python_scanner.py` module performs comprehensive static
analysis on Python source files using 9 integrated tools: **Bandit**,
**Pylint**, **pyflakes**, **Semgrep**, **pip-audit**, **Ruff**, **Vulture**,
**deadcode**, and **pyscn**. It detects security issues, code quality problems,
dead code, dependency CVEs, and clone detection. Auto-fix is available via
autoflake, isort, autopep8, pyupgrade, and ruff. The scanner is invoked during
the self-evolution loop and after each supervisor judgement to flag risky
patterns before they are accepted into the codebase.

### Per-Edit Python Gate (global Codex hook)

`vulnerability/py_edit_gate.py` is a fast, standalone counterpart to that
full scan. It is registered as a Codex `PostToolUse` lifecycle hook, so every
Codex session on the machine checks Python the moment the agent edits it. It is
standalone (stdlib only, imports nothing from this repo) and is installed to
`~/.codex/scripts/py_edit_gate.py`, which is the single copy every registered
hook runs.

Per edited `.py` file, in order:

1. **Safe autofix, deliberately narrow** - `ruff check --fix` on
   `F401,F811,F841,I` (unused/duplicate imports, unused locals, import order)
   only, and the blank-file-header left behind by a removed import block is
   repaired. A step is reverted byte-for-byte if the result stops compiling, if
   the file's existing CRLF endings are lost, or if it changes more than
   `max(20 lines, 10%)` of the file.

   `E`/`W`/`UP`/`COM` are advisory-only **on purpose**, and `ruff format` is
   opt-in (`--format`, or `PY_EDIT_GATE_FORMAT=1`). They rewrite code anywhere
   in the file, and one reformatted line inside the agent's next patch context
   is enough to make that patch fail with `Failed to find expected lines` -
   which is how an agent ends up resubmitting a whole file, and how a long
   patch gets truncated with a missing `*** End Patch`. The gate must not
   invalidate the diff the agent is about to write.

   `E501` (line too long) is ignored outright, not even advised: a long line is
   not a defect, and fixing it means reflowing code the agent did not edit.
   Line length belongs to the project's own formatter.
2. **Auto-import** - for an undefined name in a curated stdlib/typing map, the
   import is inserted (after shebang/docstring/import block). Accepted only if
   the file still compiles *and* that `F821` is gone; otherwise rolled back.
   Names outside the map are advisory only, never guessed.
3. **Checks** - `compile()`, then `ruff`, `pyflakes`, and `bandit` (HIGH/MEDIUM)
   on the touched file. 6s per tool, 18s wall-clock cap.

Findings come back to the agent as a short `additionalContext` advisory
(auto-fix summary first, then missing imports, syntax errors, lint, security).
When the gate edits a file it says so explicitly and tells the agent to re-read
the file and **not** resubmit it whole. The gate never blocks a tool call,
dedupes a repeated verdict for 6 hours, and **fails open**: any internal error
means a silent exit 0.

Install / refresh (idempotent; backs up and re-validates `config.toml`, and
refuses to touch a config that does not parse):

```
python -m services.config.codex_edit_gate_install
python -m services.config.codex_edit_gate_install --remove
python -m services.config.codex_edit_gate_install --no-autofix   # advisory only
python -m services.config.codex_edit_gate_install --format        # re-enable ruff format
```

Supervised runs use a separate `CODEX_HOME`, so their config gets its own
`[[hooks.PostToolUse]]` block pointing at the same installed script, and
`codex-suprun.cmd` passes `--dangerously-bypass-hook-trust` (per-invocation,
vetted-source only; it does not affect approvals):

```
python -m services.config.codex_edit_gate_install --codex-home .codex_home_soprun --shared-script %USERPROFILE%\.codex\scripts\py_edit_gate.py
```

Notes:

- **Trust**: interactive sessions prompt once to accept a new hook; the hash
  lands in `[hooks.state]` keyed by that config path, so one acceptance covers
  the CLI, `codex exec`, and the Desktop app. Until accepted, the hook is
  skipped - `codex doctor` does not report hooks, so check the audit log.
- **Audit log**: `~/.codex/logs/py_edit_gate.jsonl` (rotated at 1 MB) records
  every fire: files, tool tier (`ruff+pyflakes+bandit` or degraded), duration.
  This is how you confirm the gate is alive.
- **Opt out**: `PY_EDIT_GATE=0` (env, off entirely); a `.edit-gate-disable`
  file in a project root (advisory only, no in-place edits); `--no-autofix`
  (in the hook command).
- Checker rules run `--isolated --no-cache`, so behaviour is identical in every
  repo and no `.ruff_cache` is littered into projects you merely open.
- Tool discovery tries `PATH`, then known venv/conda interpreters, and degrades
  to syntax-only if no checker is installed. Files over 400 KB, read-only
  files, and `.venv`/`site-packages`/`node_modules`/build dirs are skipped.
- Out of scope: shell-written `.py` files (agent writes via `python -c`),
  argument-count/type errors (needs a type checker), and opencode runs.

Tests: `checks/test_py_edit_gate.py`, `checks/test_codex_edit_gate_install.py`.

#### Per-Edit Rust Gate

`vulnerability/rs_edit_gate.py` is the Rust sibling, installed to
`~/.codex/scripts/rs_edit_gate.py` and registered as a **second**
`[[hooks.PostToolUse]]` block. Codex matches hooks on the tool name, not the
script path, so a new language costs one extra block - no new config surface.

Per edited `.rs` file: `rustfmt --edition <from Cargo.toml>` acts as the syntax
gate (rustfmt is a rustup shim here, so its toolchain noise is classified as a
note, never as a parse error), then `cargo check --offline
--message-format=json` supplies real type/borrow errors, mapped to
`src/lib.rs:line`. Checks are crate-scoped and cadenced by measured crate cost:
fast crates run on every edit (20 s floor), slow ones every 120 s, and a cold
crate with no `target/` is skipped with a warming hint rather than costing a
five-minute build. A `Blocking waiting for file lock` reply is swallowed.
`--stop` (registered on `[[hooks.Stop]]`) runs `cargo clippy` once per crate per
interval and replies with `systemMessage` only - `hookSpecificOutput` on a Stop
event makes Codex report `hook: Stop Failed`.

`rustfmt` as a formatter stays opt-in (`--format`): 29 of 51 `orderpipe` files
are not rustfmt-clean, so formatting on edit is churn, not hygiene. Measured
warm `cargo check`: 0.10 s (fixture crate), 2.5 s (`rust_ext`), 4.85 s
(`orderpipe`), 11 s (`snapshotpipe`).

```
python -m services.config.codex_edit_gate_install --rust
python -m services.config.codex_edit_gate_install --rust --codex-home .codex_home_soprun --shared-script %USERPROFILE%\.codex\scripts\rs_edit_gate.py
```

Kill switch `RS_EDIT_GATE=0`; audit log `~/.codex/logs/rs_edit_gate.jsonl`
(records `mode: edit|stop`, per-crate `policy`, seconds, error counts).
Tests: `checks/test_rs_edit_gate.py`.

#### Per-Edit Windows Script Gate (.ps1 / .psm1 / .psd1 / .cmd / .bat)

`vulnerability/win_edit_gate.py` is the third sibling on the same
`[[hooks.PostToolUse]]` event.

- **PowerShell** gets a real syntax check from PowerShell's own parser
  (`[System.Management.Automation.Language.Parser]::ParseFile`), which parses
  and never executes - asserted by a test that a script's side effect does not
  happen. `pwsh` is preferred, `powershell.exe` is the fallback, and the child
  console is forced to UTF-8 so a localized (zh-CN) parse message reaches the
  agent as text instead of CP936 mojibake.
- **Batch** has no parser, so the gate checks the two bugs that produce no error
  message at all - running `helper.bat` without `call` (control never returns)
  and `!var!` without `setlocal EnableDelayedExpansion` (expands to nothing) -
  and encoding traps: UTF-16 (cmd cannot read it), UTF-8 BOM (cmd eats the first
  line), LF-only endings (cmd mis-parses blocks), and BOM-less non-ASCII under
  PowerShell 5.1 (read as ANSI, garbled). LF-only `.ps1` is *not* a finding.
- Opt-in: `--ps-format` (`Invoke-Formatter`, churn-guarded, reverted if the
  result stops parsing) and `--ps-lint` (`Invoke-ScriptAnalyzer`, throttled 300 s
  per file). PSScriptAnalyzer is not installed on this box, and the gate says so
  as a note rather than failing silently.

Cost: 133 ms for a PowerShell parse, 2 ms for a batch scan.

```
python -m services.config.codex_edit_gate_install --windows
python -m services.config.codex_edit_gate_install --windows --ps-format --ps-lint   # opt-ins
python -m services.config.codex_edit_gate_install --windows --codex-home .codex_home_soprun --shared-script %USERPROFILE%\.codex\scripts\win_edit_gate.py
```

Kill switch `WIN_EDIT_GATE=0`; audit log `~/.codex/logs/win_edit_gate.jsonl`
carries per-section finding counts (`syntax`, `encoding`, `batch`, `lint`).
Out of scope: scripts the agent writes from a shell (`Set-Content`,
`Add-Content`) - the matcher sees edit-tool calls only, which is how the gate
missed a shell-written `.ps1` in the first live run.
Tests: `checks/test_win_edit_gate.py`.

Three gates means three advisories per edit; measured overhead is about half a
second for a Python edit and 0.2 s for a script edit. All three are advisory,
never block a tool call, and fail open.

---

## Safety Features

1. **Workspace boundary enforcement** — All path references are validated
   against the workspace root
2. **Protected path detection** — System directories and user-protected
   files cannot be modified or deleted
3. **Protocol alignment verification** — Each iteration is checked against
   the protocol for compliance
4. **Regression testing** — Self-evolution compares test results against
   the baseline before accepting changes
5. **Automatic rollback** — Bad changes are reverted to the last good
   checkpoint
6. **Heartbeat monitoring** — Detects stalled processes and extends
   timeouts when progress is being made
7. **Archive preservation** — Historical versions are preserved, never deleted

---

## TODO

- [ ] Get rid of `pip install -e . --force-reinstall` so that every self evolution will be auto applied
- [ ] Add multi agent cooperation/competition
- [ ] Replace keyword-based process cleanup (`taskkill`/`pkill` by process name) with PID-tracked child process management
