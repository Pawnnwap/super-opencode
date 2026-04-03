# opencode-supervisor

> **A self-evolving autonomous coding agent — it supervises, judges, and improves itself.**

---

[English](./README.md) | [中文](./README_zh.md)

A Streamlit-driven, dual-loop autonomous coding system. `SupervisorLoop` drives protocol-guided
task execution with an LLM-based judge (`LLMSupervisor`) that evaluates every iteration for
protocol alignment. `SelfEvolutionLoop` turns the same machinery on the codebase itself —
running test-gated self-improvement with automatic regression rollback. All file edits flow
through a `hashline` MCP server that validates every change with content-addressed hashes,
eliminating stale-reference errors.

Key capabilities:

- **Dual-loop architecture** — `SupervisorLoop` for external tasks, `SelfEvolutionLoop` for self-improvement
- **LLM-based judge** — `LLMSupervisor` evaluates opencode output against protocol targets at every step
- **Hash-anchored editing** — `hashline` MCP server provides `hashline_read` / `hashline_edit` with stale-ID rejection and atomic writes
- **Streamlit UI** — Three-page management interface: Protocol Wizard, Live Run, and Self-Evolution
- **Plan mode** — Configurable pre-execution planning rounds with read-only opencode analysis
- **Vulnerability scanning** — 9-tool static analysis pipeline (Bandit, Semgrep, Ruff, etc.)
- **Context management** — Token-aware monitoring with graduated warnings and auto-compaction

---

## Prerequisites

- Python 3.11 or higher
- An API key for OpenAI or any compatible provider (e.g., NVIDIA NIM, Ollama)
- opencode CLI installed (see installation below)

---

## Windows Installation

### Installing Chocolatey

If you don't have Chocolatey installed, run the following command in an **Administrator** PowerShell:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
```

For more details, see the [Chocolatey installation page](https://chocolatey.org/install).

### Installing opencode

To install opencode on Windows using Chocolatey:

```bash
choco install opencode
```

The executable is installed to `C:\ProgramData\chocolatey\bin\opencode.exe`.

For UI configuration, use the path `C:\ProgramData\chocolatey\bin\opencode.exe`.

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
> importable from anywhere — including when Streamlit launches `app.py` from
> a different working directory.

Alternatively, you can install from `requirements.txt` directly:

```bash
pip install -r requirements.txt
```

All dependencies are defined in `pyproject.toml`: `openai`, `streamlit`,
`tiktoken`, `pytest`, `cryptography`, `rich`, and `psutil`.

---

## Running the Application

```bash
streamlit run app.py
```

If the above command does not work, try:

```bash
python -m streamlit run app.py
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

---

## Streamlit UI Pages

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
| Evolution report | `evolution_report.md` — changed files, test delta, best checkpoint |

---

## Architecture

```
app.py                              Streamlit UI  (3 pages: Wizard, Live Run, Self-Evolution)
supervisor/
  __init__.py                       Package exports

  core/
    loop.py                         SupervisorLoop — main supervised agent loop
    loop_base.py                    BaseLoop — common state machine, event yielding
    self_evolution_loop.py          SelfEvolutionLoop — self-modification with test gating
    llm_supervisor.py               LLM judge that evaluates opencode output

  analyzers/
    codebase_analyzer.py            Snapshots source tree for LLM context
    opencode_step_detector.py       Detects step progress in opencode output

  protocols/
    protocol.py                     Parse / validate protocol.md (INPUT, TARGET, RESTRICTIONS)
    protocol_wizard.py              OpenAI-SDK protocol refiner
    protocol_analyzer.py            Quality scoring (clarity, testability, completeness)
    meta_protocol_builder.py        Generates meta_protocol.md from evolution goal + snapshot

  runners/
    opencode_runner.py              Subprocess wrapper for the opencode CLI
    test_runner.py                  Runs pytest / syntax check; structured results

  utils/
    config.py                       Frozen SupervisorConfig dataclass
    file_ops.py                     File operations utilities
    credentials_manager.py          Credential storage helpers
    gitignore_utils.py              Automatic .gitignore update helpers

  prompts/
    __init__.py                     Package exports for prompt templates
    templates.py                    All prompt templates (init, judge, hashline instructions)

  monitoring/
    context_monitor.py              Tracks context window usage with graduated warnings
    token_estimator.py              Token counting (tiktoken) and prompt truncation

  workspace/
    workspace_guard.py              Blocks out-of-workspace path references
    workspace_archiver.py           Preserves workspace versions in .archive/
    opencodeignore_handler.py       .opencodeignore file management
    ignore_patterns.py              .opencodeignore parsing and pattern matching

  vulnerability/
    python_scanner.py               Python code vulnerability scanner (static analysis)

  tests/                            Test suite (pytest)
    runners/
      test_opencode_runner.py       Tests for OpencodeRunner

pyproject.toml                      Makes `supervisor` an installable package (also defines all deps)
requirements.txt                    Alternative dependency list for pip install -r
mcp_server/
  hashline.py                       MCP server for hash-anchored file editing (hashline_read, hashline_edit)
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

### Adding a Custom Model

The Streamlit UI provides a built-in form to configure custom models without manual file editing:

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

## Hash-Anchored File Editing

The system includes an MCP server (`mcp_server/hashline.py`) that provides
hash-anchored file editing tools. Every line read from a file is annotated with
a `LINE#ID` hash (e.g., `42#VK| def process(data):`) that encodes both the
line number and content. This enables:

- **Safe concurrent edits** — the MCP server validates all LINE#IDs before
  writing; if any ID is stale (because another edit changed the file), the
  entire operation is rejected and corrected IDs are returned
- **Atomic writes** — edits are applied via temp file + `os.replace` so the
  file is never left in a partial state
- **Edit operations**: `replace`, `replace_range`, `delete`, `append`,
  `prepend` — all addressed by hash-anchored positions
- **Dry-run mode** — validate IDs without writing to disk

The hashline MCP server is automatically configured in opencode's
`opencode.json` on startup, so opencode receives hash-anchored editing
instructions as part of its system prompt.

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
