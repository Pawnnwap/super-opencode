"""File operation utilities."""

import logging
import shutil
import time
from pathlib import Path
from typing import Optional
from pathlib import Path


import logging
import shutil
from pathlib import Path
from typing import Optional


def safe_read_text(
    path: Path,
    default: str = "",
    max_size: int = 10 * 1024 * 1024,  # 10MB default limit
    encodings: list[str] | None = None,
    retries: int = 3,
) -> str:
    """Read text from a file with robust error handling.

    Parameters
    ----------
    path:
        Filesystem path to read.
    default:
        Value returned when the file does not exist or cannot be read.
    max_size:
        Maximum file size in bytes to read (prevents memory issues).
    encodings:
        List of encodings to try (default: ["utf-8", "latin-1"]).
    retries:
        Number of retry attempts for transient errors.

    Returns
    -------
    str
        File contents or *default*.

    """
    if encodings is None:
        encodings = ["utf-8", "latin-1"]

    for attempt in range(retries + 1):
        try:
            if not path.exists():
                return default

            # Check file size to prevent memory issues
            stat = path.stat()
            if stat.st_size > max_size:
                logging.warning(f"File {path} exceeds max_size ({max_size} bytes), returning default")
                return default

            # Try each encoding in sequence
            for i, encoding in enumerate(encodings):
                try:
                    content = path.read_text(encoding=encoding)
                    if i > 0:
                        logging.info(f"Used fallback encoding {encoding} for file {path}")
                    return content
                except UnicodeDecodeError:
                    if i == len(encodings) - 1:
                        # Last encoding failed, log and return default
                        logging.warning(f"All encodings failed for file {path}, returning default")
                        return default
                    # Continue to next encoding
                    continue

        except PermissionError as e:
            logging.warning(f"Permission denied reading {path}: {e}")
            if attempt == retries:
                return default
            time.sleep(0.1 * (attempt + 1))  # Exponential backoff
        except OSError as e:
            logging.warning(f"OS error reading {path}: {e}")
            if attempt == retries:
                return default
            time.sleep(0.1 * (attempt + 1))

    return default


def copy_tree_to_workspace(src_dir: Path, workspace_dir: Path) -> list[str]:
    """Copies all files from src_dir to workspace_dir preserving directory structure.
    Returns a list of relative paths that were restored/copied.
    """
    restored: list[str] = []
    for src in sorted(src_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(src_dir)
        dst = workspace_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored.append(str(rel))
    return restored
