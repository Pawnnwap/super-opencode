"""File operation utilities."""

import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)


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

    # A directory is not a file: opening it as text raises PermissionError on
    # Windows and the retry ladder logged the same warning four times per call.
    # The judge harness used to hit this on every `read_file` request whose
    # path argument was dropped (2026-11-02..06). Callers that genuinely need
    # a listing must render it themselves; this helper stays file-only.
    if path.is_dir():
        logger.debug("Refusing to read a directory as text: %s", path)
        return default

    for attempt in range(retries + 1):
        try:
            if not path.exists():
                return default

            # Check file size to prevent memory issues
            stat = path.stat()
            if stat.st_size > max_size:
                logger.warning(f"File {path} exceeds max_size ({max_size} bytes), returning default")
                return default

            # Try each encoding in sequence
            for i, encoding in enumerate(encodings):
                try:
                    content = path.read_text(encoding=encoding)
                    if i > 0:
                        logger.info(f"Used fallback encoding {encoding} for file {path}")
                    return content
                except UnicodeDecodeError:
                    if i == len(encodings) - 1:
                        # Last encoding failed, log and return default
                        logger.warning(f"All encodings failed for file {path}, returning default")
                        return default
                    # Continue to next encoding
                    continue

        except PermissionError as e:
            logger.warning(f"Permission denied reading {path}: {e}")
            if attempt == retries:
                return default
            time.sleep(0.1 * (attempt + 1))  # Exponential backoff
        except OSError as e:
            logger.warning(f"OS error reading {path}: {e}")
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
