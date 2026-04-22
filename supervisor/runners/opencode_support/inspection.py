from __future__ import annotations

import re

_FILE_PATTERNS = [
    re.compile(
        r"(?:^|\s)([a-zA-Z_][\w./\\-]*\.(?:py|js|ts|jsx|tsx|json|yaml|yml|toml|md|txt|rst|cfg|ini|sh|bat|ps1|html|css|xml))(?:\s|$)",
        re.MULTILINE,
    ),
    re.compile(r"(?:file|path):\s*([a-zA-Z_][\w./\\-]+)", re.IGNORECASE),
    re.compile(
        r"(?:reading|creating|writing|modifying|editing|updating|opening)\s+(?:file\s+)?([a-zA-Z_][\w./\\-]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"```[\w]*\s*\n\s*(?:#\s*)?([a-zA-Z_][\w./\\-]+\.(?:py|js|ts|json|yaml|yml|toml|md|txt))",
        re.MULTILINE,
    ),
]


def extract_file_refs(output: str) -> list[str]:
    """Extract probable file references from opencode output."""
    files: set[str] = set()
    for pattern in _FILE_PATTERNS:
        for match in pattern.finditer(output):
            file_path = match.group(1)
            if file_path and len(file_path) > 2:
                files.add(file_path)
    return sorted(files)
