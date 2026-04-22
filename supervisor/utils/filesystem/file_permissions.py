"""supervisor/utils/file_permissions.py — cross-platform read-only bit helpers.

Extracted from WorkspaceGuard so both workspace_guard.py and
workspace_archiver.py can share the same implementation without duplication.
"""

from __future__ import annotations

import os
import stat
import subprocess


def set_file_readonly(path_str: str) -> None:
    """Set the read-only attribute on a single file path."""
    if os.name == "nt":
        subprocess.run(["attrib", "+r", path_str], check=False, capture_output=True)
    else:
        current = os.stat(path_str).st_mode
        os.chmod(path_str, current & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)


def remove_file_readonly(path_str: str) -> None:
    """Remove the read-only attribute from a single file path."""
    if os.name == "nt":
        subprocess.run(["attrib", "-r", path_str], check=False, capture_output=True)
    else:
        current = os.stat(path_str).st_mode
        os.chmod(path_str, current | stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
