# hashline MCP — opencode instructions

## Tool preference

Always use `hashline_read` instead of the built-in `read` when you intend to
edit a file afterwards. Using the built-in `read` before a `hashline_edit` call
will cause every operation to fail with a validation error — the LINE#IDs will
be missing.

Always use `hashline_edit` instead of the built-in `write` or `edit`. It
validates all line references atomically before touching disk, so a partial
failure never corrupts the file.

## Workflow

1. `hashline_read(path, start_line, end_line)` — read only the lines you need.
   Every line comes back annotated: `42#VKB| def process(data):`
2. `hashline_edit(path, edits)` — reference lines by their LINE#ID.
   `auto_retry` is true by default: stale refs are corrected and re-applied
   server-side automatically. You do not need to re-read or retry manually.

## Reading large files

Prefer ranged reads over full-file reads. If you need to locate a section
first, read a small window (e.g. 1–50), then narrow with a second read once
you know the relevant line numbers. This keeps context small.

## After a write

LINE#IDs go stale the moment the file changes. Always re-read the file (or the
affected range) before issuing a second round of edits.

## Operations

| op            | effect                                      |
|---------------|---------------------------------------------|
| replace       | replace single line at `pos`                |
| replace_range | replace `pos`..`end_pos` (inclusive)        |
| delete        | remove line at `pos`                        |
| append        | insert lines immediately after `pos`        |
| prepend       | insert lines immediately before `pos`       |

Multiple edits in one call are applied atomically in a single write.
Overlapping edits are detected and rejected before anything is written.

## auto_retry behaviour

`auto_retry=true` (default): if LINE#IDs are stale the server corrects them
and completes the write. The response includes `"status": "auto_retried"` and
`"patches": N` so you know corrections were made. Verify the diff.

`auto_retry=false`: stale refs cause a `HashlineMismatch` error with
`retry_edits` (pre-corrected edits) and a `snippet` showing the affected lines.
Re-submit `retry_edits` directly without re-reading.
