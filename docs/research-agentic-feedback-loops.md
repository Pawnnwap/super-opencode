# Research: Goal-Loop Supervision Patterns for Coding Agents (2024–2026)

Findings from GitHub/web research on preventing dead loops and premature stops in
goal-driven agent loops. This informs the planned `/goal` mode enhancement
(session goal item 3). Sources verified against current upstream source where noted.

---

## 1. Reflection / Self-Correction Patterns

### 1.1 Reflexion
- **What**: Verbal RL. On task failure, a separate "self-reflection" LLM call produces a short lesson ("what went wrong, what to do differently") appended to episodic memory; the agent retries with that memory. Learning is text, not weights.
- **Sources**: [arXiv:2303.11366](https://arxiv.org/abs/2303.11366), [github.com/noahshinn/reflexion](https://github.com/noahshinn/reflexion)
- **Mechanism**:
```
for trial in range(max_trials):        # typical max_trials=3..6
    traj = agent.run(task, memory=reflections)
    ok = env.evaluate(traj)            # EXTERNAL signal: tests, exact-match
    if ok: break
    reflections.append(self_reflect(traj, failure_signal))  # plain text lesson
```
- **Key detail**: reflections are *bounded in size* (sliding window; older lessons dropped), and the evaluation signal is external (tests), not the agent's own opinion.
- **Mapping**: Keep a `lessons.md` per goal. On a failed verification or a judge "not done" verdict, one cheap LLM call writes a 2–3 sentence lesson; the next opencode iteration's prompt includes the last K lessons.

### 1.2 Self-Refine
- **What**: Same LLM alternates as generator and feedback provider: generate → feedback → refine, repeating until feedback says "no change needed" or a max-iteration cap (~3–4) hits. Feedback is *actionable and specific*, not a score.
- **Sources**: [arXiv:2303.17651](https://arxiv.org/abs/2303.17651), [github.com/selfrefine/selfrefine](https://github.com/selfrefine/selfrefine)
- **Mapping**: Require the judge to output structured feedback (`{criterion_id, met: bool, evidence, next_action}`) instead of just continue/stop — `next_action` becomes the injected instruction for the next opencode iteration.

### 1.3 CRITIC (tool-interactive critiquing)
- **What**: LLM critiques its own output, but the critique is *grounded by external tools* — self-correction only works with external verifiers.
- **Source**: [arXiv:2305.11738](https://arxiv.org/abs/2305.11738); survey: [github.com/ryokamoi/llm-self-correction-papers](https://github.com/ryokamoi/llm-self-correction-papers)
- **Mapping**: Never let the judge verify from agent chat text alone. Give the judge tool calls: read files, run tests, run `git diff`.

### 1.4 Chain-of-Verification (CoVe)
- **What**: Decompose "is it done?" into independent verification questions generated from the goal, answer each *independently* (fresh context, no access to the agent's own claims), then combine.
- **Source**: [ACL Findings 2024](https://aclanthology.org/2024.findings-acl.212.pdf)
- **Mapping**: **Single best fix for premature stop.** At goal start, decompose into per-criterion checks; the judge must answer each with evidence gathered by running commands, not from the agent's report.

### 1.5 Critical caveat: intrinsic self-correction fails
- "Large Language Models Cannot Self-Correct Reasoning Yet" ([arXiv:2310.01798](https://arxiv.org/abs/2310.01798), ICLR 2024): LLMs asked to self-correct *without external feedback* often flip correct answers to wrong ones. Self-correction works only with extrinsic (tool/test) signal.
- **Implication**: judge verdicts must be gated on executable evidence (exit codes, test output, git diff), never on the agent's self-assessment.

## 2. Goal-Driven Loop Structures in Major Frameworks

### 2.1 OpenHands (formerly OpenDevin)
- Most mature production controller — event-stream loop with built-in stuck detector and configurable thresholds.
- Sources: [stuck_detector.py in OpenHands/software-agent-sdk](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/stuck_detector.py); [docs](https://docs.openhands.dev/sdk/guides/agent-stuck-detector). Details in §3.1.

### 2.2 SWE-agent (Princeton)
- Interface design (Agent-Computer Interface) dominates agent success: guardrails beat freedom.
- Sources: [arXiv:2405.15793](https://arxiv.org/html/2405.15793v3), [docs/background/aci.md](https://github.com/SWE-agent/SWE-agent/blob/main/docs/background/aci.md)
- Mechanisms: lint-gated edits (invalid edits refused before applying); explicit `submit` signal — the agent's "I'm done" is a *proposal*, evaluated independently by the harness.

### 2.3 Aider — reflection caps + executable feedback loop
- Every edit round trips through lint/test gates; error messages fed back as auto-generated user message — but reflections are hard-capped.
- Source: [aider/coders/base_coder.py](https://github.com/Aider-AI/aider/blob/main/aider/coders/base_coder.py)
```python
num_reflections = 0
max_reflections = 3

def run_one(self, user_message, preproc):
    while message:
        self.reflected_message = None
        list(self.send_message(message))
        if not self.reflected_message:
            break
        if self.num_reflections >= self.max_reflections:
            warn("Only 3 reflections allowed, stopping.")
            return
        self.num_reflections += 1
        message = self.reflected_message
```
- **Mapping**: per-issue retry budgets (e.g., 3 attempts on the same failing test) distinct from the global iteration budget.

### 2.4 LangGraph — reflection as graph structure
- `generate → reflect → conditional_edge → (revise | END)`; the conditional edge carries loop-control policy. Sources: [LangGraph docs](https://langchain-ai.github.io/langgraph/), reflection tutorials (2025).
- **Mapping**: the supervisor is a two-node graph: `run_agent` node and `judge` node with a conditional edge implementing the stop policy.

### 2.5 AutoGen / AG2 — critic agents + termination predicates
- Code execution feedback loop (continue until code runs clean); critic agent ending replies in `TERMINATE`; `is_termination_msg` string predicates as cheap programmatic stop conditions.
- Sources: [AutoGen reflection pattern](https://microsoft.github.io/autogen/stable//user-guide/core-user-guide/design-patterns/reflection.html), [AG2 code-execution feedback](https://docs.ag2.ai/latest/docs/use-cases/notebooks/notebooks/agentchat_auto_feedback_from_code_execution/)
- **Mapping**: before invoking the LLM judge each iteration, run free predicates (last command's exit code, "all tests pass") that short-circuit the judge on easy cases — saves tokens and removes judge noise.

### 2.6 Claude Code — verification-gate Stop hook
- A `Stop` hook runs when the agent tries to finish; if verification fails, the hook blocks the stop and **forces the agent to continue** with the failure as feedback. Built-in cap: **default 8 consecutive blocks** (`stop_hook_active`) — can't stop early, can't loop forever.
- Sources: [hooks reference](https://code.claude.com/docs/en/hooks), [hooks guide](https://code.claude.com/docs/en/hooks-guide), [disler/claude-code-hooks-mastery](https://github.com/disler/claude-code-hooks-mastery)
- **Mapping**: complete blueprint for loop semantics: `blocked_stop_count` counter, force-continue with verification failure as feedback, hard cap → escalate to user.

### 2.7 GPT-Pilot — milestone/task decomposition
- Specification → User Tasks → Developer Tasks → Architecture → iterative development steps; each step implemented/tested/repaired in a small loop. Tasks are small so loops terminate and context stays fresh.
- Source: [github.com/Pythagora-io/gpt-pilot](https://github.com/Pythagora-io/gpt-pilot)
- **Mapping**: decompose the goal into a task DAG; each node has its own micro-loop and completion test; the global judge only routes between nodes.

### 2.8 MetaGPT & CrewAI
- MetaGPT: SOPs as role pipelines with structured artifacts ([paper](https://arxiv.org/html/2308.00352v7)). CrewAI hierarchical process: manager agent delegates and validates ([docs](https://docs.crewai.com/v1.15.17/en/learn/hierarchical-process)).
- **Mapping**: use separate roles for judge vs. planner. Known failure mode (CrewAI [#4783](https://github.com/crewAIInc/crewAI/issues/4783)): manager agents delegate unreliably — keep the supervisor deterministic, reserve LLM judgment for verify.

## 3. Stuck / Loop Detection in Real Codebases

### 3.1 OpenHands `StuckDetector` — the reference implementation
File: [`openhands-sdk/openhands/sdk/conversation/stuck_detector.py`](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/stuck_detector.py). Scans only events since the last user message, window ≤ 20 events.

| Scenario | Mechanism | Default threshold |
|---|---|---|
| 1. Repeating action→observation | Last N actions identical (semantic compare) AND last N observations identical | 4 |
| 2. Repeating action→error | Trailing run of identical actions each producing errors; fires one-time nudge at threshold | 3 |
| 3. Monologue | N consecutive agent messages with no user event between | 3 |
| 4. Alternating ping-pong | A,B,A,B pattern: `actions[i] == actions[i+2]` | 6 |
| 5. Context-window error loop | Repeated condensation events | (disabled) |

Design details worth copying verbatim:
- **Semantic equality** (`_event_eq`): compares thought + tool_name + action content, ignoring volatile fields — catches paraphrased loops, ignores noise.
- **Nudge-then-error escalation**: on first hitting the error threshold, inject a nudge ("You've called X with the same arguments 3 times... Repeating the exact same call again will not work") once per streak; only declare stuck if the streak continues past the nudge.

### 3.2 Aider — reflection cap
`max_reflections = 3` per user message; loop exits with warning. Dead-loop control by budgeted retries on the same failure class.

### 3.3 browser-use `ActionLoopDetector` — soft, escalating nudges
Files: [`browser_use/agent/views.py`](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/views.py) + `tests/ci/test_action_loop_detection.py`.
- Rolling window of **20** action hashes; hashes *normalized* (keywords sorted, case/punctuation stripped) so similar actions count as repeats.
- Also tracks `PageFingerprint` (url + dom text + element count) → `consecutive_stagnant_pages` — environment-stagnation signal, distinct from action repetition.
- **Never hard-blocks.** Escalating nudges at repetition counts **5 / 8 / 12**: "If you are making progress with each repetition, keep going. If not, a different approach might get you there faster."
- **Mapping**: two-signal split (agent-action repetition vs. environment-state stagnation): (1) hash each iteration's `git diff` + commands (normalized); (2) fingerprint the workspace (file hashes, test output); (3) nudge at 3, force strategy change at 5, abort/escalate at 8.

## 4. Goal-Condition Evaluation

### 4.1 SWE-bench style: F2P / P2P test verification
- An instance is "resolved" only if **fail-to-pass** tests (fail before, pass after) and **pass-to-pass** tests (regressions) all pass in a clean container. The agent's claims are irrelevant.
- Sources: [harness docs](https://www.swebench.com/SWE-bench/reference/harness/), [run_evaluation.py](https://github.com/swe-bench/SWE-bench/blob/main/swebench/harness/run_evaluation.py)
- **Mapping**: for each goal, synthesize F2P checks first (or have the agent write them). If none can be written, the goal isn't verifiable — say so up front.

### 4.2 Agent-as-a-Judge — judge as an *agent with tools*
- Agentic judge evaluates the whole trajectory: locates/reads intermediate artifacts in the workspace, judges against the requirement list. Matched human evaluators on DevAI (55 tasks, 365 hierarchical requirements); dramatically outperformed context-free LLM-as-a-Judge.
- Sources: [arXiv:2410.10934](https://arxiv.org/abs/2410.10934), [github.com/metauto-ai/agent-as-a-judge](https://github.com/metauto-ai/agent-as-a-judge)
- **Mapping**: upgrade the judge from "reads the opencode transcript" to "has sandbox shell access and must gather its own evidence per criterion." Highest-leverage premature-stop fix.

### 4.3 Definition-of-Done checklists
- Standing project-wide quality floor separate from per-task acceptance criteria. Example: [addyosmani/agent-skills `definition-of-done.md`](https://github.com/addyosmani/agent-skills/blob/main/references/definition-of-done.md) — "does not count as done": work that hasn't been run; treating passing tests as sufficient; declaring done before review.
- **Mapping**: store `dod.md` next to the goal file; judge checks DoD items in addition to goal criteria.

### 4.4 Reward hacking evidence (anti-gaming checks)
- Frontier models routinely game verifiers: hardcoding expected outputs, editing/deleting tests, mocking implementations. Sources: [METR report](https://metr.org/blog/2025-06-05-reward-hacking/), [ImpossibleBench](https://www.lesswrong.com/posts/qJYMbrabcQqCZ7iqm/impossiblebench-measuring-reward-hacking-in-llm-coding-1), [DebugML cheating audit](https://debugml.github.io/cheating-agents/)
- **Mapping**: cheap judge heuristics — diff touches a test file flagged as F2P → flag; tests pass but diff empty/trivial or contains literal expected-output strings → flag; require tests to pass against a fresh checkout of the diff.

## 5. Structured Feedback, State Files, Escape Hatches

### 5.1 The Ralph loop — filesystem as persistent memory
- Minimal autonomous loop (`while :; do cat PROMPT.md | agent; done`) — [Geoff Huntley](https://ghuntley.com/ralph/), [repo](https://github.com/ghuntley/how-to-ralph-wiggum). All state lives in files: plan file (PRD/`plan.md` with checkboxes), progress journal (`progress.txt` appended each iteration). Each iteration re-reads files, so drift and self-deception don't compound.
- Practitioner refinements: [Addy Osmani's self-improving agents](https://addyosmani.com/blog/self-improving-agents/), [planning-with-files skill](https://github.com/OthmanAdi/planning-with-files/blob/master/.agents/skills/planning-with-files/SKILL.md)
- **Mapping**: supervisor maintains `goal.md` (criteria with checkboxes) + `progress.md` (one entry per iteration); progress-journal *edits* are themselves a progress signal — identical consecutive entries ≈ no progress.

### 5.2 Budget accounting and stop-policy caps
- Claude Code: `stop_hook_active` + 8-block cap; Aider: `max_reflections = 3`; browser-use: window-20 nudges at 5/8/12. Convergent design: **tiered counters** (per-failure-class, per-iteration, per-token) with distinct consequences (nudge → force strategy change → ask user → abort).

### 5.3 Ask-user escalation
- SWE-agent explicit `submit`; Aider bounded one-question `confirm_ask`; Claude Code plan mode gates execution on approval.
- **Mapping**: define `ESCALATE` as a first-class judge verdict alongside `CONTINUE`/`DONE`, with a required structured question.

## 6. Judge Calibration / Premature Completion Literature

1. "LLMs Cannot Self-Correct Reasoning Yet" ([arXiv:2310.01798](https://arxiv.org/abs/2310.01798)) — verdict-only judges without external evidence degrade accuracy; grounding is mandatory.
2. Agent-as-a-Judge ([arXiv:2410.10934](https://arxiv.org/abs/2410.10934)) — agentic, tool-using judge beats passive LLM-as-a-Judge.
3. CoVe ([ACL 2024](https://aclanthology.org/2024.findings-acl.212.pdf)) — independent verification questions prevent anchoring on the agent's claims; direct anti-sycophancy architecture.
4. METR reward hacking — judge must audit *how* tests pass, not just that they pass.
5. LLM-as-a-Judge failure modes: [DeepEval guide](https://deepeval.com/blog/llm-as-a-judge), [Galileo](https://galileo.ai/blog/why-llm-as-a-judge-fails), [survey 2025](https://arxiv.org/html/2508.02994v1).
6. Sycophancy: a judge sharing context with the optimized agent tends to agree with it — mitigation is separation of contexts (fresh judge context, evidence from workspace not transcript).

---

## Top 10 Actionable Design Ideas, Ranked by Impact

### (i) Avoiding dead loops

1. **Port OpenHands' StuckDetector on the supervisor side.** Hash each iteration's normalized signature: commands run (argv-normalized), `git diff` stats + content hash, exit codes, test output hash. Detect: (a) identical signature ×4, (b) same-signature-with-error ×3, (c) ping-pong A,B,A,B, (d) monologue. Semantic compare, ignore volatile fields.
2. **Separate "agent did something" from "environment changed."** An iteration that edits files but produces byte-identical worktree state is zero-progress even if commands differ.
3. **Escalating nudges before hard stops.** Nudge at repetition 3, force strategy change at 5 ("list 3 alternative approaches; pick one you haven't tried"), abort/escalate at ~8.
4. **Per-failure-class retry budgets (Aider's `max_reflections`).** 3 attempts max on the same failing test/error, distinct from the global step budget.
5. **Progress journal as a progress meter.** Require a structured journal entry every iteration (files touched, tests run, outcomes); supervisor diffs consecutive entries — near-zero delta over K iterations = fruitless, regardless of what the judge believes.

### (ii) Avoiding premature stop

6. **Judge must gather its own evidence (Agent-as-a-Judge).** Give the judge sandbox shell access; require per-criterion evidence from the workspace; prohibit verdicts derived from transcript claims alone.
7. **CoVe-style criterion decomposition with independent checks.** DONE requires every criterion verified-with-evidence this iteration (stale evidence expires after N iterations or any diff).
8. **Verification-gate stop with a block cap (Claude Code Stop-hook semantics).** When judge says DONE: run executable verification. Fail → block the stop, feed the failure back, increment `blocked_stop_count`; at cap → escalate to user instead of looping. Single mechanism = premature-stop fix AND dead-loop guard.

### (iii) Goal auto-feedback and guidance

9. **Structured judge output, Self-Refine shaped.** Judge returns `[{criterion, met, evidence, next_action}]` + verdict ∈ {CONTINUE, DONE, ESCALATE, BLOCKED}; `next_action` injected verbatim into the next iteration's prompt.
10. **Reflexion lessons + plan-file checkpointing.** On each "not met" or failed verification, a cheap reflection call appends a ≤3-sentence lesson to a bounded `lessons.md`; supervisor maintains `goal.md` on disk and re-injects it fresh each iteration.

**Bonus (cheap, high value)**: anti-gaming audit before accepting DONE (diff touches F2P test files, trivial diff with passing tests, expected-output literals in source); SWE-bench-style F2P synthesis at goal start so "verifiable" is a goal-admission requirement.
