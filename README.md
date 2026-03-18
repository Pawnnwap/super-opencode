# opencode-supervisor

Streamlit UI + modular Python backend that runs an `opencode` agent in a
supervised feedback loop — and can turn that same loop on **itself** to
debug and evolve its own source code.

---

## Prerequisites

- Python 3.11 or higher
- An API key for OpenAI or any compatible provider (e.g., Ollama)

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

---

## Running the Application

```bash
streamlit run app.py
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
5. **Self-evolution** — The system can analyze and improve its own codebase,
   running tests before and after each change with automatic rollback on regression

---

## Pages

### ① Protocol Wizard
Fill in INPUT / TARGET / RESTRICTIONS in plain language → click
**Refine with AI** → review the generated `protocol.md` → Accept & Save.

### ② Live Run
Start the supervisor loop against any project workspace. Live log streams
in real time. Stop between steps at any time.

### ③ Report
Final supervisor report after a run, with download button.

### ④ Self-Evolution
Point the system at **its own source tree**.

1. Describe what you want debugged or improved
2. Optionally add extra restrictions
3. **Generate meta_protocol.md** — LLM reads the live source and writes
   a precise protocol with accurate INPUT and testable TARGETs
4. Review / edit, then **Launch Evolution**

Self-evolution extras:

| Feature | Detail |
|---------|--------|
| Test baseline | `pytest` (or syntax check) runs before opencode touches anything |
| Per-iteration tests | Tests re-run after every supervisor judgement |
| Regression guard | Tests worse → auto-rollback to last good checkpoint |
| Checkpointing | Every non-regressing iteration is snapshotted to `.checkpoints/` |
| Evolution report | `evolution_report.md` — changed files, test delta, best checkpoint |

---

## Architecture

```
app.py                          Streamlit UI  (4 pages)
supervisor/
  config.py                     Frozen config dataclass
  protocol.py                   Parse / validate protocol.md
  protocol_wizard.py            OpenAI-SDK protocol refiner
  llm_supervisor.py             OpenAI-SDK judge  (system prompt = protocol)
  opencode_runner.py            Subprocess wrapper for the opencode CLI
  context_monitor.py            Tracks context window usage
  workspace_guard.py            Blocks out-of-workspace path references
  loop.py                       Base state machine; yields Event dicts
  codebase_analyzer.py          Snapshots source tree for LLM context
  test_runner.py                Runs pytest / syntax check; structured results
  checkpoint.py                 File-copy snapshot / restore / diff
  meta_protocol_builder.py      Generates meta_protocol.md from goal + snapshot
  self_evolution_loop.py        Extends loop with test gating + rollback
pyproject.toml                  Makes `supervisor` an installable package
```

---

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| API Key | — | Your API key (OpenAI or any compatible provider) |
| Base URL | *(blank = OpenAI)* | Override for local/proxy endpoints e.g. `http://localhost:11434/v1` |
| Supervisor / wizard model | — | Any model string your provider accepts |
| opencode model | *(opencode default)* | Forwarded to the opencode CLI |
| Max retries | 3 | Consecutive failures before forced stop |
| Context threshold | 60 % | Compaction fires at this fraction of estimated max |
| Timeout | 300 s | Silence before opencode is deemed unresponsive |
