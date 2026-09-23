---
description: Judging DONE when the protocol requires passing tests — what counts as test evidence.
---
Green test claims in the agent's prose are not evidence. Judge test criteria
only on observed results.

1. Run `NEED_EVIDENCE: test_report` before believing a pass or fail claim;
   mark test criteria UNMET until you have seen a result.
2. "Tests pass" is not "tests exist". New code with no new or changed test
   files cannot satisfy a coverage criterion — check the changed-files list.
3. If tests were edited in the same turn success is claimed, read them
   (`NEED_EVIDENCE: read_file <path>`) and check the edits weaken assertions
   rather than strengthen the code. Weakened assertions mean UNMET.
4. Failures unrelated to the changed code do not block the criteria they do
   not affect; say which failures remain and whether they touch the target.
5. If the suite cannot run at all, say so and make fixing the gate the
   NEXT_ACTION. DONE: yes still requires every criterion MET with evidence.
