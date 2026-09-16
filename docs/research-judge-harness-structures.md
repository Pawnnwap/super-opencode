# Research: agentic judge / harness structures (2024–2026) — focused sweep

Companion to `research-agentic-feedback-loops.md`. Focused on judge architectures,
harnesses, and open resource registries. Key sources verified on GitHub/Docs.

## Structures surveyed

1. **Agent-as-a-Judge** (ICML 2025, [metauto-ai/agent-as-a-judge](https://github.com/metauto-ai/agent-as-a-judge), arXiv:2410.10934). Judge agent explores the workspace per requirement: locate files → read (2k-token caps) → ask binary; hierarchical requirement DAG with prerequisite short-circuit. Ablation: ask alone 65% human-alignment; +read 82%; +locate+read+ask 90.4% — while memory/planning/embedding-search modules *hurt*. The winning judge is the simple one.
2. **CRITIC** (ICLR 2024, arXiv:2305.11738): critique must cite tool output; self-correction without tools barely works or degrades.
3. **Chain-of-Verification** (Meta, arXiv:2309.11495): decompose into per-claim checks in isolated contexts; prevents error contagion.
4. **AgentRewardBench** (2025, arXiv:2504.08942): no single judge excels everywhere; evaluate your evaluators (precision/recall vs hand labels).
5. **promptfoo**: uniform grader contract `{reason, pass, score}`; deterministic assertions layered BEFORE model graders; `trajectory:*` assertions; agent-rubric (grader inspects workspace evidence).
6. **DeepEval**: GEval (CoT evaluation steps then score); DAGMetric gates (deterministic TaskNode feeding BinaryJudgementNodes).
7. **Ragas faithfulness**: decompose into atomic statements, verify each against context, score = supported/total. Cheapest evidence-grounded scoring trick.
8. **MCP resources**: open capability registry (`resources/list` + `read(uri)` + `listChanged` notifications) — the canonical open/auto resource protocol.
9. **OpenHands microagents / Claude Code rules**: folder-driven, path-gated auto context (drop a file, new context appears) — the open-registry exemplar.
10. **SE-Jury / SWE-Judge** (arXiv:2505.20854): 5 prompt-strategy judges, dynamic team selection on ~20 samples, soft voting; +30–140% correlation over single judges.
11. **CodeMonkeys** (Stanford, arXiv:2501.14723): regression + LLM-generated tests as deterministic filter; diff-similarity clustering + majority vote.
12. **AlphaCodium**: AI-generated tests as anchors; iterate on code, never tests.
13. **Cognition evaluator pipeline**: deterministic checks first; two-pass evaluation; environment state as critique signal; "evaluate the evaluators".
14. **AgentBoard** (arXiv:2401.13178): per-subgoal progress rate 0→1 across turns.
15. **Reward hacking** (METR 2025): agents edit tests to pass; the judge must flag verification-file edits.

## Adopted here (Occam filter: simple + effective)

- Deterministic "Workspace Evidence" block auto-injected into every judge prompt (changed files, tree, audit tail) — ranked #1/#2.
- Per-criterion MET/UNMET verdicts with mandatory evidence + deterministic contradiction validator (MET with negative evidence → auto-UNMET) — #3.
- NEED_EVIDENCE protocol: judge requests on-demand resources (`test_report`, `read_file`) resolved once per turn then re-judged — bounded Agent-as-a-Judge (#9).
- Open registry (`JudgeHarness.register` / `register_context_provider`) — #7; MCP-style openness without the protocol weight.
- Test-file-edit red flag on DONE acceptance — reward-hacking guard.

## Deferred (kept out by Occam's razor)

- Self-consistency voting (n=3) gated on verdict flips — cost/latency.
- Checklist-as-DAG prerequisite propagation — needs dependency metadata.
- Folder-driven auto-discovery of resources (microagents-style) — the registry is the extension point; folder scanning adds surface without a current need.
- Judge precision/recall measurement against hand labels — the audit log already records everything needed; do it when labels exist.
