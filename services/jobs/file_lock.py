"""Small cross-process file lock backed by the operating system."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_INIT_LOCK = threading.Lock()


class FileLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self, *, blocking: bool = False) -> bool:
        if self._held:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _INIT_LOCK:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, b"\0")
                finally:
                    os.close(fd)
            except FileExistsError:
                if self.path.stat().st_size == 0:
                    with self.path.open("r+b") as init_handle:
                        init_handle.write(b"\0")
        handle = self.path.open("r+b")
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

        while True:
            try:
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if not blocking:
                    handle.close()
                    return False
                time.sleep(0.05)
        self._handle = handle
        self._held = True
        return True

    def write_json(self, payload: dict) -> None:
        if not self._held or self._handle is None:
            raise RuntimeError("file lock is not held")
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(json.dumps(payload).encode("utf-8"))
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def release(self) -> None:
        if not self._held or self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None
            self._held = False

    def __enter__(self):
        if not self.acquire(blocking=True):
            raise RuntimeError(f"could not acquire lock: {self.path}")
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.release()
