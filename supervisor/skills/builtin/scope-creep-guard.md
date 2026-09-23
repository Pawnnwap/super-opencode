---
description: Spotting and correcting agent work that drifts outside the protocol TARGET.
---
The TARGET and RESTRICTIONS sections define the work; anything outside them
is creep, even when it looks like quality improvement.

1. Compare the changed-files list against the TARGET every turn. For each
   off-target file, tell the agent to keep, justify, or revert it.
2. Treat unrequested refactors, dependency upgrades, and "while I'm here"
   features as creep — direct the agent back unless the protocol invites them.
3. If the agent touched protected or generated files, name the RESTRICTIONS
   violation first in the feedback; it outranks any progress made.
4. A turn that fixed targets and reverted its own detour is a good turn —
   do not mark it unsuccessful.
5. If the agent pursues the same off-target goal across turns, forbid it
   explicitly in NEXT_ACTION (e.g. "do not touch X; only complete <target>").
