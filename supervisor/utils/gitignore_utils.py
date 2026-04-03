"""Gitignore utilities for supervisor startup."""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# The exact lines to append (including the newline characters)
GITIGNORE_LINES = """# super-opencode
.archive
.opencode
.ruff_cache
nul
test_*.py
.job_store
"""


def update_gitignore_files(workspace_root: Path) -> list[Path]:
    """Find all .gitignore files in the workspace tree and append the required lines
    if they don't already exist. Returns a list of modified files.

    Args:
        workspace_root: The root directory to search for .gitignore files

    Returns:
        List of Path objects for .gitignore files that were modified

    """
    # Find all .gitignore files in the workspace
    gitignore_files = list(workspace_root.rglob(".gitignore"))

    modified_files = []

    for gitignore_path in gitignore_files:
        try:
            # Read the current content
            if gitignore_path.exists():
                current_content = gitignore_path.read_text(encoding="utf-8")
            else:
                current_content = ""

            # Check if the required lines are already present
            # We check for the exact block to ensure idempotency
            if GITIGNORE_LINES.strip() in current_content:
                continue

            # Append the required lines
            new_content = current_content.rstrip() + "\n" + GITIGNORE_LINES
            gitignore_path.write_text(new_content, encoding="utf-8")
            modified_files.append(gitignore_path)
            logger.info(f"Updated .gitignore file: {gitignore_path}")

        except Exception as e:
            logger.warning(f"Failed to update .gitignore file {gitignore_path}: {e}")
            continue

    return modified_files
