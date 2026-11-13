# opencode-supervisor

> **An autonomous coding agent — it supervises and judges every iteration against your protocol.**

---

[English](./README.md) | [中文](./README_zh.md)

A NiceGUI-driven supervised coding system. `SupervisorLoop` drives protocol-guided
task execution with an LLM-based judge (`LLMSupervisor`) that evaluates every iteration for
protocol alignment. File changes use each execution backend's native read, edit, patch,
and write tools.

Key capabilities:

- **Supervised loop** — `SupervisorLoop` runs one protocol against one workspace, judged every turn
- **Triple execution backend** — OpenCode (default), Codex, or pi, selected via `config.engine`; the supervisor loop is backend-agnostic
- **Evidence-grounded judge** — `LLMSupervisor` evaluates agent output against protocol targets at every step, grounded in deterministic workspace facts via the `JudgeHarness` evidence registry
- **Goal guard** — Evidence-gated completion: "all targets met" is a proposal, validated against deterministic evidence
- **Loop & stagnation detection** — Intra-turn tool-loop detection plus cross-turn workspace-fingerprint stagnation with a nudge ladder
- **Native file editing** — OpenCode, Codex, and pi use their maintained read, edit, patch, and write tools
- **Web UI** — NiceGUI dashboard: Protocol Wizard, Live Run, and Harvester pages, with a persisted background-job board and full-page live job screens
- **Plan mode** — Configurable pre-execution planning rounds with read-only opencode analysis
- **Occam's razor pass** — Optional post-success redundancy-reduction stage that runs on an archive copy, never the live workspace
- **Task-state journal** — Append-only `TASK_STATE.md` progress journal re-injected after context resets
- **Headline job logs** — every event persists as one key line; large blocks (prompts / outputs / verdicts / suggestions) keep their full bodies in a per-job sidecar (`.job_store/<job>.full`), and error events are always written in full
- **Parallel study batches** — launch N isolated instances of one protocol from the Run page (workspace clones or protocol-only seeds); the board groups them into one batch card with one-click cancel, under a configurable concurrency cap
- **Incremental workspace archiving** — `.archive/` snapshots are git object stores (one orphan commit per snapshot): unchanged files are shared by hash and retention actually frees space, with automatic full-copy fallback when git is unavailable
- **Context management** — Token-aware monitoring with graduated warnings and auto-compaction
- **Feasibility gate** - optional `feasibility_facts.json` (workspace root, outside the archived `logs/`) records known gate ceilings and blocked gates; an unreachable done-condition is surfaced up front instead of being rediscovered over many pre-registered attempts. The Live Run form offers **Generate draft**: the supervisor model transcribes gates from `protocol.md` and ceilings evidenced in prior-attempt artifacts (`status.json`, `REPORT.md`, …) into the form for review — it never invents numbers
- **Judge-context budget** - tiered history compaction plus identical-input verdict reuse: a byte-identical judge input replays the stored verdict without another LLM call

---

## Hard path policy (whitelist, read-only references)

Every supervised run now enforces a deterministic filesystem boundary that
overrides each backend's own permission defaults:

| Scope | Read | Write |
|-------|------|-------|
| Project cwd (the run workspace) | allowed | allowed |
| Whitelisted directories | allowed | **denied** (read-only, always) |
| Everything else | denied | denied |

Configure it on the Protocol Wizard under **Path Access Whitelist**:

- add absolute reference directories (fixtures, corpora, sibling repos),
- optionally deny inline interpreter code (`python -c`, `node -e`, `bash -c`),
- use **AI suggest whitelist** to have the supervisor model propose the
  directories a protocol refers to. Suggestions are re-validated locally:
  drive roots, OS/program directories, the user-profile root, UNC shares,
  missing paths and anything inside the workspace are rejected.

Enforcement is native per engine, not a prompt request:

- **opencode** - V1 `permission` path patterns (`external_directory` deny,
  per-entry read allow / edit deny) plus a project plugin that also blocks
  shell writes the permission scanner cannot classify. The V2 `permissions`
  array is not written: opencode 1.x rejects the file when that key is present.
- **pi** - a `tool_call` extension loaded with an explicit `-e`, returning
  `{ block: true }` before the tool runs.
- **codex** - a `PreToolUse` hook in `$CODEX_HOME/hooks.json` that runs the
  supervisor's policy gate.

The policy snapshot lives outside the workspace (under
`~/.opencode_supervisor/access/`) and is handed to the backend through
`SOP_ACCESS_POLICY`, so the agent cannot widen its own policy by editing a
file in the cwd. Blocked calls are written to a JSONL audit log and surfaced
as `ACCESS POLICY BLOCKED ...` warnings in the job log.

Shell commands are parsed by one conservative analyzer shared by all three
backends (opencode/pi delegate to it from their gates). It handles POSIX,
cmd.exe and PowerShell quoting and escaped spaces, known environment
variables, redirect targets and wrapper commands (`cmd /c`, `pwsh -Command`,
`bash -c`/`-lc`) recursively. It **denies what it cannot resolve**:
command substitution, backticks, unknown variables, delayed expansion,
encoded commands, script blocks, heredocs, control flow, `find -exec`,
dangerous binaries (`wsl`, `ssh`, `certutil`, `mshta`, ...) and inline
interpreter code. An unknown command's path arguments are treated as writes,
so a custom `--out <path>` cannot write into a read-only whitelist. If the
analyzer fails or times out, the tool call is denied.

Before the first turn the supervisor runs a **preflight drill**: the policy
engine is exercised against cwd / whitelist / outside paths and `..` command
escapes, and for codex the installed hook is actually invoked with a synthetic
event. A gate that does not deny aborts the run instead of degrading silently.
The same drill is available on demand from **Run policy drill** in the UI.

This is a deterministic **rule gate**, not a kernel sandbox: it constrains the
tools and commands the agents use, and deliberately does not claim to survive
arbitrary native code. For a container-grade boundary (no network, read-only
root, disposable filesystem) run the agent inside Docker/WSL or a VM as the
`cage` project does, and keep this policy as the inner layer.

Set `access_policy_enabled: false` in `~/.opencode_supervisor_settings.json`
only when you deliberately want the previous unrestricted behaviour.

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
`requirements.txt` file additionally pins a static-analysis toolchain (Bandit,
Pylint, pyflakes, Semgrep, pip-audit, Ruff, Vulture, deadcode, pyscn) and the
auto-fix helpers (autoflake, isort, autopep8, pyupgrade) for manual use.

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
the tree changes, live jobs keep the code they started with and the queue keeps
filling up to `max_parallel_jobs`; the worker re-execs to load the new code at
the first idle poll — so a deployed fix applies without a manual worker restart,
and the concurrency cap stays the only limit on total parallelism.

---

## What the Supervisor Does

The supervisor system runs the opencode agent in a controlled feedback loop:

1. **Protocol-driven execution** — A `protocol.md` file defines INPUT, TARGET,
   and RESTRICTIONS that guide the agent's behavior
2. **Real-time monitoring** — Context window usage is tracked and compaction
   triggers automatically when needed
3. **Workspace safety** — The system blocks out-of-workspace path references to
   prevent unintended modifications
4. **Workspace archiving** — Workspace state is preserved in `.archive/` before
   each run and after each iteration
5. **Goal guard** — Completion claims are verified against deterministic
   evidence before the run ends (see below)
6. **Loop detection** — Repeated tool calls within one turn are detected and
   the run is killed and restarted with fixed context instead of burning tokens;
   cross-turn workspace stagnation escalates through a nudge ladder
7. **Occam's razor pass** — After success, an optional stage lets the agent strip
   redundant code from an archived copy of the final workspace
8. **Task-state journal** — A `TASK_STATE.md` progress journal preserves
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
When jev is enabled the *first* blocked verdict is held for one supervised
recovery attempt (see below); later blocked verdicts close immediately.
The durable state is written too: `closed` when the closure is supported (or
harness-measured), `blocked` when the agent asserted it but the facts hold
measured evidence of reachability (a passing gate or a ceiling that meets the
bar) — never left at `running`. An all-open facts file is neutral, not
evidence, and does not override a terminal ruling.

### Structured decision service (jev)

Text regexes cannot see every way a judge can say "this is over", nor every
shape a MET claim, a stagnant turn, a protocol flag or a DONE proposal can
take. Six of those bounded decisions can instead be arbitrated by a
structured-decision endpoint, opt-in in **Configuration → Jev decision
arbiter** (URL, optional API key, timeout, close threshold; disabled by
default).

When enabled and the judge's `NEXT_ACTION` is a closure/stand-down candidate,
the loop sends a compact evidence block (mandate, durable state, judge
criteria/next action/feedback, agent's last output) and asks for calibrated
probabilities over:

- `close_now` / `terminal` (noul), and
- `action`: `close_run`, `finish_closeout_then_close`, `continue_design_work`,
  `escalate_to_operator` (choice).

Policy: close at `P(close) ≥ threshold` with `close_run`; give
`finish_closeout_then_close` one grace turn, then close; a `continue` or
`escalate` verdict suppresses the text triggers while the deterministic
attainment/unreachable checks still apply. Any timeout or malformed response
falls back to the existing text heuristics — the decision service being down
never keeps a run alive or blocks a closure. Agent-declared closure
(`goal_terminal`, closure documents) and all measured evidence are never
delegated.

The same client arbitrates four more textual judgment calls, each with the
same outage rule (any failure falls back to the deterministic path):

- **Verdict contradiction** — after the regex pass, every MET criterion whose
  evidence is non-empty is paired up and asked "does this evidence contradict
  the MET claim?". A confident contradiction downgrades the criterion and
  clears `all_targets_met`, with a note in the job log.
- **Stagnation action** — when the workspace-stagnation detector fires, the
  arbiter picks `continue` (suppress the nudge), `nudge`, `strategy_change`
  (escalated guidance), or `restart` (the loop restarts the agent session).
- **Protocol alignment** — the deterministic scanner is intentionally broad;
  the arbiter is the final yes/no on flagged output. A confident
  `no_violation` suppresses the reinforcement warning; anything else keeps it.
- **DONE proposal** — after every deterministic veto passes, a confident
  `reject`/`need_evidence` blocks the proposal; `accept` passes with a note.
  Choice decisions act at `P ≥ 0.5`; binary ones use the configured threshold.

**First-blocked rescue.** Jev also arbitrates a sixth decision, but only after
the deterministic blocked assessment has already fired. On the first blocked
verdict (`enable_blocked_rescue`, default on; only active with jev), the loop
holds the close and forces the judge to:

1. translate the blocker into up to 2 English web queries;
2. run those queries in-process (Bing + Marginalia, the same endpoint the
   `web_tools` MCP server uses) and inject the snippets as untrusted,
   explicitly-noisy evidence (never proof);
3. audit the executor record — last output, `summary.md`, and the recent judge
   journal — for factual errors, arbitrary scope/acceptance changes, and
   laziness / premature give-up (unrun checks, placeholders, deleted
   validation, unsupported "good enough" claims);
4. write a concrete recovery plan (failure audit, root cause, a *different*
   mechanism, executable steps, acceptance evidence, stop conditions, and a
   statement of what the web evidence does and does not establish).

Jev then rules `feasible` or `not_feasible` on that plan. A confident
`feasible` cancels the blocked exit, resets the attainment/stagnation
counters, saves the plan to `.opencode/unblock_plan.md`, and sends it to the
executor as the next prompt. Any rejection, judge outage, empty plan, or jev
outage closes the run exactly as before; `max_blocked_rescues` defaults to 1
(backend-only — the UI neither binds nor persists it), so later blocked
verdicts are final.

**Bounded-decision audit.** Same client (`noul`/`choice`/`score`) can arbitrate.
Ranked by **sureness × frequency**, where sureness (1–5) is how completely the
occasion can be reduced to a well-posed question with text-ready evidence and
a clear answer space, and frequency (1–5) is how often it arises (5 = every
judged turn, 1 = rare). The ranking is not implementation difficulty.

| Rank | Decision point | Sureness | Frequency | Score | Status |
|---|---|---|---|---|---|
| 1 | Verdict MET-vs-negative evidence | 4 | 5 | 20 | **implemented** — second-pass contradiction check |
| 2 | Stagnation action | 4 | 4 | 16 | **implemented** — nudge / strategy change / restart |
| 3 | Protocol alignment | 3 | 5 | 15 | **implemented** — final yes/no on scanner flags |
| 4 | Run closure / BLOCKED exit | 5 | 2 | 10 | **implemented** — closure/stand-down arbitration |
| 5 | DONE proposal accept/reject | 4 | 2 | 8 | **implemented** — post-veto ambiguity only |
| 6 | Direction novelty | 4 | 2 | 8 | not implemented — near-threshold similarity cases |
| 7 | Plan-mode approval | 3 | 2 | 6 | not implemented — accept plan vs another round |
| 8 | Occam accept/rollback | 4 | 1 | 4 | not implemented — final accept/rollback |
| 9 | Loop restart after repeated tools | 3 | 1 | 3 | not implemented — ambiguous repetition cases |

The closing decision ranks fourth by frequency-weighted score but **first on
sureness** (and it is the one whose failure costs hours), which is why it was
implemented first; ranks 1–3 and 5 followed because each audits a judgment the
deterministic code could only approximate with patterns. Verdict contradiction
is frequent and sure, but it audits the judge itself; it only downgrades what
a confident arbiter reads as contradiction. Protocol alignment is as frequent
as a verdict but needs the full restriction set in the evidence, so it stays a
second opinion rather than the sole decider.

Never delegated to jev: gate counts, test results, file existence, context
thresholds, worker code freshness, retry counts, protected paths, and cleanup
deletion safety. These are measurements or safety rules, not judgments.

The first-blocked rescue above is the sixth implemented decision; it is
derived from the blocked-exit judgment rather than a separate audited
candidate, so it has no row in the original ranking table.

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
and `/harvest`. Launching a task enqueues a
background job via the `JobManager` — job state and logs persist under
`.job_store/`, so runs continue independently of the browser. The left drawer
shows live queue stats and locks Live Run until the connectivity tests pass.

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
  registry for CLI upgrades, and switches for the Occam's razor pass,
  supervisor skills, jev decisions, and blocked-run rescue. A
  **Clean artifacts** button removes run
  leftovers from the workspace.
- **Protected Files** — Mark files that the agent cannot modify or delete
- **.opencodeignore** — Configure ignore patterns for files excluded from
  context retrieval
- **Connectivity Tests** — One-click agent and supervisor connectivity
  checks; Live Run stays locked until both pass
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
- Codex reasoning items stream into the log as `💭 AGENT THOUGHT` events and
  share the **Agent speech** toggle with actual prose; they are live-log
  context only and never enter the supervisor's judged output
- Log search, noise filter, and download
- Stop a running job at any time
- Final report with download button (available after the run completes)

### ③ Harvester
Collect finished runs into one clean folder. Pick runs with checkboxes, or
type a string — matched against job id, workspace, batch id, and the final
report — and matching runs are selected automatically. Set a custom harvest
prompt and output location; every launch creates a fresh
`harvest_<timestamp>/` folder containing:

- `<workspace>__<job>/report.md` — the run's stored final report (always)
- `<workspace>__<job>/run.json` — provenance and collection status
- `<workspace>__<job>/harvest.md` — the agent's distilled result, or a fallback
- `INDEX.md` — every selected run, its state, and collection status
- `HARVEST.md` — a batch-level summary across the per-run notes

The configured agent (codex / opencode / pi) applies the harvest prompt to
each run. A run whose agent fails or writes nothing still gets its report,
metadata, and a fallback note, so the collection is never incomplete. Harvest
jobs have their own board and live screen.

---

## Architecture

```
app.py                              Web UI  (3 pages: Wizard, Live Run, Harvester)
supervisor/
  __init__.py                       Package marker
  memory_policy_evaluator.py        Fixed-suite gate for durable-memory policies

  core/
    loop.py                         SupervisorLoop — main supervised agent loop
    loop_base.py                    BaseLoop — common state machine, event yielding
    goal_guard.py                   Evidence-gated completion (propose → validate → execute → audit)
    goal_progress.py                Progress signal (passing gates, streaks) feeding stagnation nudges
    decision_service.py             Optional external judge (jev) for DONE / alignment / rescue calls
    unblock_rescue.py               One-shot rescue when a run is blocked (optional)
    skill_bank.py                   Supervisor skill library (optional)
    occam_razor.py                  Post-success redundancy-reduction pass on an archive copy
    task_state.py                   Append-only TASK_STATE.md journal for restart continuity
    target_state.py                 Persistent TARGET evidence and direction tracking
    llm_supervisor.py               LLM judge that evaluates agent output
    judge_harness.py                Evidence registry grounding the judge (auto + NEED_EVIDENCE resources)
    llm_support/                    Judge internals: chat, context, history,
                                    history_compaction, judgement, models

  analyzers/
    codebase_analyzer.py            Snapshots source tree for LLM context
    opencode_step_detector.py       Detects step progress in agent output
    loop_detector.py                Intra-turn loop detection (repeated tool calls)
    stagnation_detector.py          Cross-turn workspace stagnation with nudge ladder

  protocols/
    protocol.py                     Parse / validate protocol.md / protocol.jsonl
                                    (INPUT, TARGET, RESTRICTIONS)
    protocol_wizard.py              OpenAI-SDK protocol refiner
    protocol_analyzer.py            Quality scoring (clarity, testability, completeness)
    target_audit.py                 Deterministic preflight audit of TARGET sections
    feasibility_gate.py             feasibility_facts.json gates + status tracking
    feasibility_wizard.py           LLM-assisted drafting of feasibility gates
    gate_rubric.py                  Closed-list gate scorecard for the judge
    alignment.py                    Protocol alignment / violation checks
    analyzer_support.py             Shared analyzer helpers

  security/
    access_policy.py                Deterministic read/write path policy (whitelist)
    access_profile.py               Policy snapshot persisted outside the workspace
    engine_guards.py                Native gate install per engine (opencode/pi/codex)
    shell_scan.py                   Conservative shell-command parser for the policy
    preflight.py                    Pre-run policy drill (fail closed)
    guard_cli.py                    Gate CLI invoked by the codex hook
    whitelist_advisor.py            AI-assisted whitelist suggestions (re-validated)

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
    llm_stream.py                   Shared streaming helpers for LLM calls
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
    git_archiver.py                 Git-object-store backend for .archive/ snapshots
    cleanup_candidates.py           Suggests backup/temp files for cleanup
    ignore_patterns.py              .opencodeignore parsing and pattern matching

mcp_server/
  codehelp.py                       MCP server for code assistance and dependency research
  web_tools.py                      MCP web search/fetch tools
  codehelp_support/                 Tool implementations (docstrings, packages, dependencies,
                                    docs, examples, http)

services/
  config/                           Settings persistence, opencode/codex config writers,
                                    agent-model listing, connectivity tests,
                                    SupervisorConfig builder
  jobs/                             JobManager, StateStore + store client, job worker process,
                                    harvest loop, hownow sweep, event recorder
                                    (background jobs, persisted under .job_store/)
  runtime/                          App bootstrap (auto-upgrades), guardian watchdog,
                                    fatal-crash capture, workspace cleanup
  webui/                            NiceGUI shell: layout/theme, persisted app state, job-manager
                                    singleton, batch launch, launch/live wiring, log formatting,
                                    startup tasks
    components/                     Task board, live job screen, log panel, progress widgets,
                                    feasibility panel, hownow panel, busy indicator
    pages/                          wizard, run, harvest pages

codex_compat_proxy.py               Local OpenAI-compatible proxy for the Codex CLI
start_suprun.ps1                    Idempotent launcher (proxy + app + worker + guardian)

checks/                             Integration and end-to-end check scripts
docs/                               Research notes

pyproject.toml                      Makes `supervisor` an installable package (core deps)
requirements.txt                    Alternative dependency list (core + static-analysis toolchain)
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
tool. The supervisor supplies protocol checks,
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

## Safety Features

1. **Workspace boundary enforcement** — All path references are validated
   against the workspace root
2. **Protected path detection** — System directories and user-protected
   files cannot be modified or deleted
3. **Protocol alignment verification** — Each iteration is checked against
   the protocol for compliance
4. **Automatic rollback** — Bad changes are reverted to the last good
   checkpoint
5. **Heartbeat monitoring** — Detects stalled processes and extends
   timeouts when progress is being made
6. **Archive preservation** — Historical versions are preserved, never deleted
7. **Process ownership** — A kill can only reach the run's own process tree
   (see below)

### Process ownership (hard rule)

Every process a run starts is registered at spawn in
`.job_store/<run_id>.procs.jsonl` as `(pid, create_time)` and contained in
that run's Windows Job Object (POSIX: its own session/process group). All
termination goes through `supervisor/runtime/process_control.py`, which
re-proves ownership immediately before signalling and refuses (with a line in
`<run_id>.kill_refusals.jsonl`) anything else. Name, keyword, wildcard and
parent-tree kills have no code path left.

- **MATLAB** must be started through the owned wrapper, because
  `bin\matlab.exe` forks `MATLAB.exe` and only the pair can be cleaned up
  safely:

  ```powershell
  python -m supervisor.tools.run_child matlab --script run_factor --timeout 3600
  ```

- **Python / Rust** children use the same wrapper (or the `PythonProcess` /
  `RustProcess` classes): `run_child python --module pytest -- -q tests`,
  `run_child cargo --sub test -- --release`. `rustc` and test binaries are
  descendants of the job root, so they are in scope automatically.
- **Agent shell kills** are limited to a literal PID the run's ledger proves
  it owns (`taskkill /PID <n>`, `Stop-Process -Id <n>`, `kill <n>`); `/IM`,
  `-Name`, `/T`, `-f` and variable PIDs are denied by the shell gate.
- **Crash recovery never kills**: a restart reconciles the ledger and reports
  leftovers; the guardian asks the worker to exit via `.job_store/worker.stop`
  instead of tree-killing a process it does not own.

---

## TODO

- [ ] Get rid of `pip install -e . --force-reinstall`
- [ ] Add multi agent cooperation/competition
- [x] Replace keyword-based process cleanup (`taskkill`/`pkill` by process name) with PID-tracked child process management
