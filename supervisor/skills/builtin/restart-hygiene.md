---
description: Judging fairly after a session restart or context compaction, when the agent has no conversation memory.
---
After a restart the agent sees only the protocol, summary.md, the progress
journal, and your latest feedback. Judge against that reality.

1. Do not penalize re-doing discovery (re-reading files, re-running setup);
   it is expected without conversation memory. Count progress only if the
   agent gets further than before.
2. Treat the progress journal as ground truth for prior work. Re-verify
   previously MET criteria cheaply (changed files, read_file) instead of
   asking the agent to redo them.
3. Repeating a failed approach after a restart is a loop signal: name the
   failed approach and require a concretely different NEXT_ACTION.
4. If the agent contradicts the journal (e.g. deletes completed work), flag
   it immediately as a regression.
5. Make early post-restart feedback self-contained: restate the missing
   target in full — the agent may not remember it.
