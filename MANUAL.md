# super-opencode — Deployment & Usage Manual

A local supervisor that drives a coding agent (opencode / codex / pi) against a
strict `protocol.jsonl`, enforces a hard filesystem access policy, and records
acceptance gates. Everything runs on `127.0.0.1`; no cloud control plane.

## 1. Requirements

- Windows 10/11, PowerShell 5.1+, Python 3.13 (the repo ships a `venv\`).
- An agent CLI reachable from the app: `opencode`, `codex`, or `pi`.
- An OpenAI-compatible model endpoint for the **supervisor** LLM and one for the
  **agent** engine (both configurable; a local endpoint is the intended setup).
- Optional: Node.js, if your agent CLI needs it.

## 2. First-time setup

```powershell
cd <repo>
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
```

No secrets belong in the repo. API keys live only in
`%USERPROFILE%\.opencode_supervisor_settings.json` (written by the UI).

## 3. Start, restart, stop

```powershell
.\start_suprun.ps1                      # proxy + worker + UI (delegates to Task Scheduler)
.\start_suprun.ps1 -Upstream http://127.0.0.1:8111   # agent model endpoint (default)
.\start_suprun.ps1 -Restart             # restart the UI only; jobs survive
.\start_suprun.ps1 -ProxyOnly           # proxy only
.\start_suprun.ps1 -AppOnly             # UI + job worker
```

The launcher is idempotent and **must own the services via Task Scheduler** —
never start them from an interactive shell long-term. It registers:

| Task | Purpose |
|---|---|
| `soprun_detached_launch` | on-demand start handle used by the launcher |
| `soprun_guardian` | one long-lived `pythonw` guardian (no console) |
| `soprun_guardian_backstop` | every 15 min: restart the guardian if its heartbeat is stale |

UI: <http://127.0.0.1:8501>. Proxy: `127.0.0.1:8321` (local-only).

The proxy is fully configurable: `-ProxyPort`, `-AppPort`, and `-Upstream` (or
`SOP_UPSTREAM`) — nothing is pinned in code; the effective ports are written to
`.job_store\ports.json` for the guardian, and the dedicated codex home's
`base_url` is kept in sync.

## 4. Configure

The **Protocol Wizard** page (`/`) holds the whole setup:

1. **Configuration** — workspace path, supervisor model/API key/base URL, agent
   engine + model + executable.
2. **Connectivity Tests** — required before Live Run unlocks. An app restart
   re-locks them (flags are session-only); just run the tests again.
3. **Advanced settings** — limits, retries, timeouts, log detail.
4. **Access & Protection** — one panel, one *Auto-generate* button (LLM):
   - read-only whitelist dirs (agent config/skill dirs are added automatically),
   - protected files, `.opencodeignore` patterns,
   - everything is saved to `access_protection.json` in the workspace and
     re-applied at launch; the policy drill runs automatically.
5. **Protocol** — the workspace protocol is loaded into one field per section
   (INPUT / TARGET / RESTRICTIONS), refined with AI, and saved as
   `protocol.jsonl` on **Accept & Save**.
6. **Feasibility gates** — drafted automatically after refine or on demand;
   every edit auto-saves `feasibility_facts.json`.

## 5. Workspace contract

Files super-opencode reads/writes in a workspace (all protected from agent edits):

| File | Meaning |
|---|---|
| `protocol.jsonl` | protocol sections (legacy `protocol.md` is still read) |
| `feasibility_facts.json` | acceptance gates + statuses |
| `access_protection.json` | whitelist, policy switches, protected files |
| `.opencodeignore` | workspace ignore patterns |

## 6. Running a task

1. Open **Live Run** (`/run`), confirm the protocol readiness banner.
2. **Launch** (optionally: plan rounds, Occam Razor, supervisor skills, parallel
   replicas with copy/protocol-only seeding).
3. Watch the task board / live screen; logs stream per job.
4. **Harvester** (`/harvest`) turns a finished run into one clean folder.

## 7. Access policy (hard rule)

- The workspace cwd is read/write.
- Whitelisted directories are **read-only**; every other path is denied for
  reads and writes, for every engine (native gates: opencode permission/plugin,
  pi extension, codex `PreToolUse` hook).
- Shell commands are parsed; dynamic constructs are denied; `strict` also denies
  inline interpreters.
- If the policy drill fails, the run refuses to start (fail closed).

## 8. Monitoring & recovery

The guardian is one quiet `pythonw` process (default 20 s health / 180 s queue):

- probes app and proxy `/healthz`, the worker lock, and queue claim progress;
- repairs via the launcher; an app that is still booting is left alone;
- **never kills a worker with RUNNING jobs**; kills only a wedged idle worker
  (identity-checked) or a claim-stalled idle worker;
- logs events only: `guardian.log`; heartbeat: `.job_store\guardian.heartbeat`.

Logs: `supervisor_app.out/err.log`, `job_worker.out/err.log`, `guardian.log`,
`app.fatal.log`; job records live in `.job_store\`.

## 9. Checks

```powershell
venv\Scripts\python.exe -m pytest checks\test_*.py -q
```

## 10. For AI agents

- Lifecycle: use `.\start_suprun.ps1` (`-Restart` for the UI). Do **not**
  long-start services from a session shell; Task Scheduler must own them.
- **Never kill the worker while jobs are RUNNING** (check job `state` in
  `.job_store\*.json` first). Kill only an idle, wedged worker.
- Read the contract files above before editing a workspace; they are protected.
- The guardian already repairs dead app/proxy/worker and stalls — check
  `guardian.log` before intervening.
- Ports/upstream: read `.job_store\ports.json`; override with CLI/env, never
  hard-code them. The proxy binds `127.0.0.1` by design.
- Verify changes with the checks above; keep secrets out of the repo and commit
  diff.

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| Live Run locked | run the connectivity tests on the wizard page |
| `protocol.jsonl` missing | create/refine a protocol and Accept & Save |
| Policy drill failed | fix the whitelist/switch shown in the drill output |
| Port already in use | `.\start_suprun.ps1 -Restart`, or wait for the guardian |
| Codex hook conflict | keep hooks in exactly one representation (`config.toml` **or** `hooks.json`) |
| Worker looks stuck | check `.job_store\worker.lock` age and RUNNING jobs before acting |
