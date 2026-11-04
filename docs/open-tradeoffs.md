# Open trade-offs (deliberately deferred)

Decisions made consciously, with the cost written down so a later pass can
reconsider them without re-deriving the reasoning.

## Judge sees line counts, not diff hunks

`CodebaseSnapshot.deltas()` reports per-file `~+12/-3` (lines added/removed)
and never the changed lines themselves. The file-selection listing and the
`~+a/-d` markers therefore tell the judge *where* work landed, not *what*
changed inside a file.

**Why deferred (2026-11-04):** a hunk view would add a few KB per request on
every turn. The marker already fixes the measured failure — a rewritten
deliverable looked identical to a stale artifact in the alphabetical listing —
and `NEED_EVIDENCE: read_file <path>` lets the judge pull the body when it
actually needs the lines.

**When to add:** if verdicts start missing in-file regressions that a hunk
diff would have shown (for example a gate threshold quietly loosened inside an
otherwise-unchanged file). The cheapest shape is an optional `diff` argument on
the existing `read_file` resource, returning unified hunks with three lines of
context, bounded to a few hundred lines per request.
