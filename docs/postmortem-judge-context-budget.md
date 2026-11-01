# Postmortem: chronic judge-turn truncation (2026-10-03 runs, fixed 2026-10-06, commit `ed38f4c`)

Every judge turn on the `academic_study_1003_*` runs estimated ~220k tokens against a 96k ceiling and was being silently amputated. Four independent defects compounded; each one alone was survivable, together they made the exit gate rule on ~16% of the evidence.

## Defects, in causal order

1. **Unbounded workspace tree in the judge's system prompt.** `digest_for_prompt` (`supervisor/analyzers/codebase_analyzer.py`) capped file *contents* at 15 files but rendered the **entire** tree: 5,720 files ~= 68k tokens of listing on *every* turn, growing as the run produced artifacts. Measured system prompt: 332k chars ~= 119k tokens, mostly tree.
2. **Hardcoded model limit.** `_get_model_token_limit` (`supervisor/core/llm_support/models.py`) returned 128,000 for every model, silently clamping the configured 320k budget; x0.75 response margin produced the 96k ceiling.
3. **Silent evidence amputation (the trust defect).** With system > available, `fit_request_to_budget` (`supervisor/core/llm_support/chat.py`) fell back to `max_tokens // 8` = 16k of evidence and cut the agent's 98k-token output, leaving only a buried `[...TRUNCATED...]` marker. The judge then issued `[MET]`/`[UNMET]`/`DONE` verdicts on text it had largely never seen - an exit gate ruling on amputated input, with no "no-record" semantics. A judge that does not know its evidence is partial will confidently assert `[MET]`.
4. **Unbounded side effect.** `log_prompt` (`supervisor/core/llm_support/history.py`) appended every full prompt for three days: 543 MB and 315 MB prompt logs in the two workspaces.

## Fixes (one per defect, all in `ed38f4c`)

| # | Change | File |
|---|---|---|
| 1 | Bound the tree listing and give the digest a char budget; skim caps | `supervisor/analyzers/codebase_analyzer.py` |
| 2 | Honor the configured token budget; drop the 128k clamp | `supervisor/core/llm_support/models.py`, `supervisor/core/llm_supervisor.py` |
| 3 | Judge-facing truncation receipt (partial-evidence record the verdict is allowed to cite) + higher evidence floor + explicit warning format | `supervisor/core/llm_support/chat.py` |
| 4 | Rotate the prompt log; demote the full-prompt stderr dump | `supervisor/core/llm_support/history.py` |

Regression coverage: `checks/test_judge_context_budget.py` (13 tests) on top of the existing suite (209 total green at the time).

## Design rules this established

- **The gate must be told when its input is partial.** Any budget shrink that touches judge evidence is a contract change, not a cost optimization: emit a receipt the verdict can cite, and give the judge a "no-record" answer for evidence it never received.
- **No silent clamp of a user-configured budget.** Config wins over any table lookup; the lookup is a floor for unknown models, not a ceiling.
- **Prompt context has to have a cap, including structural context.** File trees and directory listings are the cheapest way to lose a 100%-of-the-time budget war, and they grow with the run.
- **Per-prompt logging needs rotation from day one**, and the workspace is not a log sink.
- Measure from the artifact, not from the code: the 543 MB prompt log is what made the 119k-token system prompt observable.
