HASHLINE_SYSTEM_INSTRUCTIONS = """\
## File Editing Protocol – Hash-Anchored Lines

You have two MCP tools for all file read/write operations: `hashline_read` and `hashline_edit`.

---

### Reading files

**Always call `hashline_read` instead of the built-in `read` when you intend to edit \\
the file afterwards.**

`hashline_read` returns the file with every line prefixed by a LINE#ID:

    42#VK| def process(data):
    43#XJ|     return transform(data)

The LINE#ID format is  `LINE_NO#HASH`  where the 2-character hash encodes both \\
the line number and its content.  Any edit to the line — or any line above it — \\
changes the ID.  IDs are only valid against the current state of the file.

You may use the built-in `read` for purely exploratory reads where you will NOT \\
edit afterwards.

---

### Editing files

**Always call `hashline_edit` instead of the built-in `write`/`edit` when modifying \\
a hash-annotated file.**

`hashline_edit` takes:
- `path`  — path to the file
- `edits` — list of edit operations (see below)
- `dry_run` (optional) — if true, validates IDs without writing to disk

All LINE#IDs in the edit list are validated before a single byte is written.  \\
If every ID is valid the file is written atomically.  If any ID is stale the \\
entire operation is rejected and nothing is written.

#### Edit operations

| op        | required fields              | effect                                      |
|-----------|------------------------------|---------------------------------------------|
| `replace` | `pos`                        | replace the line at `pos` with `lines`      |
| `replace` | `pos` + `end_pos`            | replace the line range `pos`..`end_pos` with `lines` |
| `delete`  | `pos`                        | remove the line at `pos`                    |
| `append`  | `pos`                        | insert `lines` immediately AFTER `pos`      |
| `prepend` | `pos`                        | insert `lines` immediately BEFORE `pos`     |

Example call:
```json
{
  "path": "src/utils.py",
  "edits": [
    {
      "op": "replace",
      "pos": "42#VK",
      "lines": ["def process(data: dict):"]
    },
    {
      "op": "append",
      "pos": "43#XJ",
      "lines": ["    # validate after transform", "    assert result is not None"]
    }
  ]
}
```

---

### Rules

- **NEVER reproduce the full file** — always use targeted edits.
- **ALWAYS use LINE#IDs from the most recent `hashline_read`** — IDs from an earlier \\
read may already be stale if anything wrote to the file since then.
- **On a `HashlineMismatch` error** — the response includes a corrected snippet \\
marked with `>>>`.  Update your `pos`/`end_pos` values to the IDs shown and \\
call `hashline_edit` again.  Do not re-read the whole file unless the snippet \\
does not cover the lines you need.
- **Multiple edits in one call** — pass all edits for a file in a single \\
`hashline_edit` call.  Bottom-up ordering is handled automatically; you do not \\
need to sort them.
"""

INIT_PROMPT_TEMPLATE = """\
{hashline_instructions}

Here is your protocol:

{protocol_text}

{plan_section}\
{plan_output_section}\
Your project root (cwd) is: {workspace}
All files you create or modify MUST be inside this directory.
Use relative paths from this directory for all file operations.
A .opencode/ folder has been created there to mark this as your project root.
IMPORTANT: Never touch .checkpoints/ — that is reserved for the supervisor.
The .archive/ directory preserves historical versions — do not modify it.
{protected_files_desc}
Begin."""

SELF_EVOLUTION_INIT_PROMPT_TEMPLATE = """\
{hashline_instructions}

You are modifying the codebase you live in. Read the protocol carefully.

PROTOCOL:
{protocol_text}
{baseline_note}
Your project root (cwd) is: {workspace}
All files you create or modify MUST be inside this directory.
Use relative paths from this directory for all file operations.
Never touch .checkpoints/ — that is reserved for the supervisor.
All versions are automatically archived in the .archive/ directory.
Do NOT delete or manually manage version files — the archive system handles this.
Do NOT delete or modify the .opencode directory or its contents.
{protected_files_desc}
Make your changes surgical and minimal.
Run tests after every logical change. Begin."""

RESTART_PROMPT_TEMPLATE = """\
Context was reset due to token limits. Here is a summary of progress so far:

{summary}

PROTOCOL:
{protocol_text}

Your project root (cwd) is: {workspace}
Continue from where the summary left off. All files you create or modify MUST be inside this directory."""

JUDGE_STEP_PROMPT = """\
opencode just produced the following output. Evaluate it against the protocol.
If ALL targets are met say 'all targets met'.
Otherwise give clear, actionable feedback.

--- Step Context ---
Current step: {current_step}/{total_steps}
Current phase: {phase}
Completed phases: {completed_phases}

{feedback_context}\
{protected_context}\
--- opencode output ---
{opencode_output}
--- end ---

Focus your feedback on the current phase and remaining work."""

JUDGE_PLAN_PROMPT = """\
opencode is in PLAN MODE (round {plan_round}/{total_plan_rounds}). No code changes have been made yet — this is a planning-only phase.

Review the plan below and provide actionable feedback to refine it. Do NOT declare 'all targets met' during plan mode. Focus on:
  1. Correctness and completeness of the proposed approach
  2. Potential edge cases or missing steps
  3. Alignment with protocol requirements
  4. Sequencing and dependency issues

{context_info}\
{feedback_context}\
{protected_context}\
--- opencode plan output ---
{opencode_output}
--- end ---

Provide specific, concise feedback to improve the plan before implementation begins."""

FILE_SELECTION_PROMPT = """\
You are selecting the {top_k} most relevant source files to read before generating code-improvement suggestions.

## Protocol target
{protocol_target}

## Candidate files ({total_candidates} total)
{listing}

Return ONLY a JSON array of up to {top_k} file paths from the list above, ordered by relevance (most relevant first). Example: ["src/main.py", "tests/test_core.py"]
Do not include any explanation or markdown — just the JSON array."""

COMPACTION_INSTRUCTIONS_PROMPT = """\
The opencode agent's context window is nearly full. Generate instructions for it to:
1. Keep only the latest version of every file.
2. Retain any foundational/fallback code it may reference.
3. Write summary.md to the workspace with: current status, key decisions, remaining tasks and future directions.
Output ONLY the instruction text to send to opencode."""

DELETION_PERMISSION_PROMPT = """\
The opencode agent's context window is nearly full. Before compacting the context, you MUST address the following cleanup:

## DELETION PERMISSION

The supervisor has identified the following files as outdated, unused, or safe to delete:

{file_list}

You are granted permission to DELETE these files ONLY if:
1. The file is confirmed outdated (e.g., backup files, old versions, temp files)
2. The file is unused by any current code
3. The file is not a core module or essential configuration

Generate instructions for the agent to:
1. Review the file list above and DELETE only the confirmed outdated/unused files
2. Keep all core modules and essential files
3. Keep only the latest version of every file
4. Retain any foundational/fallback code it may reference
5. Write summary.md to the workspace with: current status, key decisions, remaining tasks and future directions

Output ONLY the instruction text to send to opencode, including explicit permission to delete the listed files."""

PROTOCOL_VIOLATION_TEMPLATE = """\

--- PROTOCOL VIOLATION DETECTED ---

The following protocol violations were detected in your output:

{violations}
Please review the protocol sections above and correct your approach.

Reminder:
  - INPUT section describes the task context you must understand
  - TARGET section lists the objectives you must achieve
  - RESTRICTIONS section defines boundaries you must not cross

Re-read these sections and adjust your actions accordingly.

--- END VIOLATION NOTICE ---
"""