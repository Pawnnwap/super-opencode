---
description: Recognizing churn and no-progress turns, and redirecting the agent with a falsifiable step.
---
Repeated effort without observable change is thrashing, not work.

1. Watch for symptoms: the same error across turns, many tool calls with no
   movement in the changed-files list, edits toggling the same file between
   states while the criterion never moves.
2. Require diagnosis before another fix attempt: the smallest experiment
   that explains the failure, before any new code change.
3. Shrink NEXT_ACTION to one falsifiable step with a checkable result
   ("run X, expect Y") — never "fix the bug" or "keep going".
4. If two approaches already failed, name both and require a different
   category of approach, not a variation of the last one.
5. Do not soften verdicts to be encouraging; a precise UNMET with a concrete
   redirect outperforms a vague pass.
