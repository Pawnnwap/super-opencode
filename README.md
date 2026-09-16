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
- **Dual execution backend** — OpenCode (default) or Codex, selected via `config.engine`; the supervisor loop is backend-agnostic
- **LLM-based judge** — `LLMSupervisor` evaluates agent output against protocol targets at every step
- **Goal guard** — Evidence-gated completion: "all targets met" is a proposal, validated against deterministic evidence
- **Loop & stagnation detection** — Intra-turn tool-loop detection plus cross-turn workspace-fingerprint stagnation with a nudge ladder
- **Native file editing** — OpenCode and Codex use their maintained read, edit, patch, and write tools
- **Web UI** — Three-page management interface: Protocol Wizard, Live Run, and Self-Evolution
- **Plan mode** — Configurable pre-execution planning rounds with read-only opencode analysis
- **Occam Razor pass** — Optional post-success redundancy-reduction stage that runs on an archive copy, never the live workspace
- **Task-state journal** — Append-only `TASK_STATE.md` progress journal re-injected after context resets
- **Vulnerability scanning** — 9-tool static analysis pipeline (Bandit, Semgrep, Ruff, etc.)
- **Context management** — Token-aware monitoring with graduated warnings and auto-compaction

---

## Prerequisites

- Python 3.11 or higher
- An API key for OpenAI or any compatible provider (e.g., NVIDIA NIM, Ollama)
- opencode CLI installed (see installation below)

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
path. That path is what you enter in the UI's "opencode executable" field if you want
to override auto-detection — leaving it blank lets the supervisor locate it automatically.

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
`tiktoken`, `pytest`, `cryptography`, `rich`, `psutil`, and `mcp`. The
`requirements.txt` file additionally pins the scanner toolchain (Bandit,
Pylint, pyflakes, Semgrep, pip-audit, Ruff, Vulture, deadcode, pyscn) and the
auto-fix helpers (autoflake, isort, autopep8, pyupgrade).

> **MCP server:** The `mcp` package is required for the Codehelp MCP server
> (`mcp_server/codehelp.py`), which provides code-assistance tools to OpenCode and Codex.
---

## Running the Application

```bash
python app.py
```

The app will open in your browser at `http://localhost:8501`.

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
9. **Occam Razor pass** — After success, an optional stage lets the agent strip
   redundant code from an archived copy of the final workspace
10. **Task-state journal** — A `TASK_STATE.md` progress journal preserves
   "what was already tried" across context resets

---

## Goal Guard (Evidence-Gated Completion)

The judge's "all targets met" is treated as a *proposal*, not an acceptance.
Before a run may end, the goal guard validates it — following the
propose → validate → execute → audit discipline (工程本体论 ch. 8):

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

---

## Occam Razor Pass

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

## Headroom Support

When `enable_headroom` is on (default `True`), the supervisor starts (or
reuses) a local Headroom proxy and supplies routing only to its own child
OpenCode process via `OPENCODE_CONFIG_CONTENT`. The user's global OpenCode
configuration and other OpenCode sessions remain untouched.

---

## Web UI Pages

### ① Protocol Wizard
Fill in INPUT / TARGET / RESTRICTIONS in plain language → click
**Refine with AI** → review the generated `protocol.md` → Accept & Save.

The wizard includes:
- **Configuration panel** — Set API key, base URL, workspace path, models,
  max retries, context threshold, timeout, max tokens
- **Protected Files** — Mark files that opencode cannot modify or delete
- **.opencodeignore** — Configure ignore patterns for files excluded from
  context retrieval
- **Live quality analysis** — Real-time scoring of protocol clarity,
  testability, and completeness as you type

### ② Live Run
Start the supervisor loop against any project workspace. Live log streams
in real time. Stop between steps at any time.

Features:
- Step-by-step progress tracking with phase detection
- Plan mode — configurable planning rounds before execution (set `plan_mode_rounds` in sidebar)
- Token usage warnings with graduated thresholds (50%, 60%, 70%, 80%, 90%)
- Verbose/compact log toggle
- Context compaction with file cleanup suggestions
- Heartbeat monitoring to detect stalled processes
- Final supervisor report with download button (available after run completes)

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
  __init__.py                       Package exports
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
    factory.py                      Constructs OpencodeRunner or CodexRunner from config.engine
    base_runner.py                  Common workspace management and lifecycle state
    opencode_runner.py              Subprocess wrapper for the opencode CLI
    codex_runner.py                 Subprocess wrapper for the Codex CLI
    opencode_support/               opencode specifics: command builder, locator, process,
                                    stream, session, inspection, result, headroom proxy
    codex_support/                  codex specifics: command builder, locator, process, stream
    command_common.py               Shared command construction helpers
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
  jobs/                             JobManager and StateStore (background jobs, persistence)
  runtime/                          App bootstrap (auto-upgrades) and workspace cleanup
  ui/                               App shell, sidebar, log UI, task board, wizard/run/evolve pages

tests/                              Test suite (pytest): streams, goal guard, task state,
                                    loop detection, compaction, UI helpers
checks/                             Integration and e2e check scripts
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
| opencode model | *(opencode default)* | Forwarded to the opencode CLI |
| Max retries | 3 | Consecutive failures before forced stop |
| Context threshold | 60% | Compaction fires at this fraction of estimated max |
| Max tokens | 128,000 | Model context window size |
| Timeout | 120 min | Silence before opencode is deemed unresponsive |
| Protected files | *(empty)* | User-defined files that opencode cannot modify |

### Advanced Configuration (SupervisorConfig)

| Setting | Default | Description |
|---------|---------|-------------|
| truncation_enabled | True | Enable prompt truncation when approaching limits |
| max_history_turns | 40 | Maximum conversation history turns before compaction |
| compact_intermediate_steps | False | Compact intermediate step outputs |
| max_protected_files_for_suggestions | 5 | Max protected files shown in suggestions |
| read_external_feedback | False | Allow external feedback injection |
| log_level | "INFO" | Logging verbosity (DEBUG, INFO, WARNING, ERROR) |
| plan_mode_rounds | 0 | Number of planning rounds before execution (0 = disabled) |
| enable_headroom | True | Route the child OpenCode process through a local Headroom proxy |
| enable_occam_razor | False | Run the post-success redundancy-reduction pass on an archive copy |

### Adding a Custom Model

The Web UI provides a built-in form to configure custom models without manual file editing:

1. In the **Protocol Wizard** page, scroll down in the sidebar to find **"Add Custom Model for Opencode"**
2. Click **"➕ Add Custom Model for Opencode"** to open the configuration form
3. Fill in:
   - **Service name**: A unique identifier for your provider (e.g., "my-custom-service")
   - **Base URL**: The API endpoint for your custom provider (e.g., "https://api.example.com/v1")
   - **API key**: Your authentication key for the provider
   - **Model names**: One model name per line (e.g., "qwen3-coder-plus", "qwen3-max")
4. Click **"💾 Save Service"** to automatically configure opencode

The system will automatically create and manage the opencode configuration file in the appropriate location (`~/.config/opencode/opencode.json` on Unix-like systems or `%APPDATA%\opencode\opencode.json` on Windows).

Once saved, you can select your custom models directly from the dropdown menu in the Protocol Wizard configuration panel, or reference them using the format `service-name/model-name` (e.g., `my-custom-service/qwen3-max`).

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
- Archives are organized into `code/`, `results/`, `logs/`, `other/` subdirectories
- Metadata is stored in `archive_metadata.json`
- Archives are numbered with timestamps and a counter
- The `.archive/` directory itself is protected from modification
- Version files are never deleted — only archived

---

## Native File Editing

Super-Opencode uses the execution backend's native file tools. OpenCode offers
`read`, exact-match `edit`, `apply_patch`, and `write`; Codex uses
`apply_patch`. The supervisor supplies protocol checks, test-gated evolution,
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
They provide research only; file editing stays with each backend's native tools.

## Plan Mode

When `plan_mode_rounds` > 0, the supervisor runs a dedicated planning phase
before execution:

1. A read-only opencode instance analyzes the protocol and workspace
2. The LLM supervisor evaluates the plan (never marks targets as met during
   planning, so the phase always runs the configured number of rounds)
3. The final plan and supervisor feedback are stored and prepended to the
   build-mode initial prompt, giving opencode a clear roadmap before it
   starts writing code

Configure planning rounds in the Live Run sidebar. Default is 0 (disabled).

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
- [ ] Better timeout handling and process tracking
