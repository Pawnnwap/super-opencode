HASHLINE_SYSTEM_INSTRUCTIONS = """\
## File Editing Protocol – Hash-Anchored Lines

You have two MCP tools for all file read/write operations:
`hashline_read` and `hashline_edit`.

---

### Reading files

**Always call `hashline_read` instead of the built-in `read` when you intend
to edit the file afterwards.**

`hashline_read` returns the file with every line prefixed by a LINE#ID:

    42#VKB| def process(data):
    43#XJZ|     return transform(data)

The LINE#ID format is `LINE_NO#HASH` where the 3-character hash encodes both
the line number and its content. IDs go stale the moment anything writes to
the file — always use IDs from the most recent `hashline_read`.

You may use the built-in `read` for purely exploratory reads where you will
NOT edit afterwards.

---

### Editing files

**Always call `hashline_edit` instead of the built-in `write`/`edit` when
modifying a hash-annotated file.**

All LINE#IDs are validated in a single pass before a single byte is written.
If every ID is valid the file is written atomically and you receive a unified
diff confirming the change. If any ID is stale the entire operation is
rejected and nothing is written.

#### Edit operations

| op        | required fields   | effect                                            |
|-----------|-------------------|---------------------------------------------------|
| `replace` | `pos`             | replace the line at `pos` with `lines`            |
| `replace` | `pos` + `end_pos` | replace lines `pos` through `end_pos` with `lines`|
| `delete`  | `pos`             | remove the line at `pos`                          |
| `append`  | `pos`             | insert `lines` immediately AFTER `pos`            |
| `prepend` | `pos`             | insert `lines` immediately BEFORE `pos`           |

Example call:
```json
{
  "path": "src/utils.py",
  "edits": [
    {
      "op": "replace",
      "pos": "42#VKB",
      "lines": ["def process(data: dict):"]
    },
    {
      "op": "append",
      "pos": "43#XJZ",
      "lines": ["    # validate after transform", "    assert result is not None"]
    }
  ]
}
```

---

### Rules

1. **Never reproduce the full file** — always use targeted edits.
2. **Always use IDs from the most recent `hashline_read`** — earlier IDs may
   be stale if anything wrote to the file since then.
3. **On a stale-ID error** — the JSON response contains `retry_edits`: your
   original edits with all refs already corrected. Call `hashline_edit` again
   with `retry_edits` as the `edits` value. Only re-read the file if you need
   lines not covered by the returned `snippet`.
4. **Batch edits per file** — pass all edits for a file in a single
   `hashline_edit` call. Bottom-up ordering is handled automatically.
5. **Overlapping edits are rejected** — if two edits target the same line
   range, split them into sequential calls (re-reading between calls).

> **dry_run** — pass `"dry_run": true` to validate IDs without writing to
> disk. Useful for preflight checks on large or sensitive edits.
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

{experience_context}\
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


# --------------------------------------------------------------------------- #
# Parameterized builder functions – eliminate repeated string interpolation #
# --------------------------------------------------------------------------- #


def build_workspace_footer(
    workspace: str,
    extra_instructions: str = "",
    protected_files_desc: str = "",
) -> str:
    """Build the common workspace footer used in all init prompts."""
    parts = [
        f"Your project root (cwd) is: {workspace}",
        "All files you create or modify MUST be inside this directory.",
        "Use relative paths from this directory for all file operations.",
    ]
    if extra_instructions:
        parts.append(extra_instructions)
    if protected_files_desc:
        parts.append(protected_files_desc)
    return "\n".join(parts)


def build_context_blocks(
    feedback_context: str = "",
    protected_context: str = "",
    experience_context: str = "",
) -> str:
    """Build optional context blocks (experience, feedback, protected)."""
    parts = []
    if experience_context:
        parts.append(experience_context)
    if feedback_context:
        parts.append(feedback_context)
    if protected_context:
        parts.append(protected_context)
    return "\n".join(parts)


def build_step_context(
    current_step: int,
    total_steps: int,
    phase: str,
    completed_phases: str,
) -> str:
    """Build the standard step context block for judge prompts."""
    return (
        "--- Step Context ---\n"
        f"Current step: {current_step}/{total_steps}\n"
        f"Current phase: {phase}\n"
        f"Completed phases: {completed_phases}\n"
    )


def build_init_prompt(
    protocol_text: str,
    workspace: str,
    header: str = "Here is your protocol:",
    plan_section: str = "",
    plan_output_section: str = "",
    extra_instructions: str = "",
    protected_files_desc: str = "",
) -> str:
    """Build a standard initialization prompt."""
    parts = [
        HASHLINE_SYSTEM_INSTRUCTIONS,
        "",
        header,
        "",
        protocol_text,
        "",
    ]
    if plan_section:
        parts.append(plan_section)
    if plan_output_section:
        parts.append(plan_output_section)
    parts.append(
        build_workspace_footer(workspace, extra_instructions, protected_files_desc)
    )
    return "\n".join(parts)


def build_judge_prompt(
    opencode_output: str,
    step_context: str = "",
    context_blocks: str = "",
    preamble: str = (
        "opencode just produced the following output. Evaluate it against the protocol.\n"
        "If ALL targets are met say 'all targets met'.\n"
        "Otherwise give clear, actionable feedback.\n"
    ),
    postscript: str = (
        "Focus your feedback on the current phase and remaining work."
    ),
) -> str:
    """Build a judge-style evaluation prompt."""
    parts = [preamble, ""]
    if step_context:
        parts.append(step_context)
    if context_blocks:
        parts.append(context_blocks)
    parts += [
        "--- opencode output ---",
        opencode_output,
        "--- end ---",
        "",
        postscript,
    ]
    return "\n".join(parts)
