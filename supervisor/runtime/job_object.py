"""Kernel-backed containment scope: a Windows Job Object, a POSIX session.

A job object is the only mechanism on Windows that gives a *structural*
guarantee: processes assigned to it create their children inside it by
default (breakaway is off unless a child explicitly asks for it and the job
allows it), and ``TerminateJobObject`` can only ever hit job members. A
monitor that holds no handle to the job cannot touch it, and no unrelated
process on the machine can be reached through it.

The handle is created non-inheritable on purpose: if children inherited it,
the job would outlive this process (handles are refcounted) and the
``kill_on_close`` backstop would never fire.

``kill_on_close`` defaults to False. The worker daemon re-execs itself
(``os.execv``) to load new code, and Windows implements exec by starting a
new image and terminating the old one - a ``kill_on_close`` job would then
take a long-running MATLAB/rustc child down with it. Long compute must
survive a supervisor restart; only throwaway work (connectivity probes)
opts in.

On POSIX the equivalent scope is a session/process group: the child is
spawned with ``start_new_session=True`` so its pgid equals its pid, and
``terminate`` sends SIGKILL to that group only.
"""

from __future__ import annotations

import logging
import os
import signal
from typing import Self

logger = logging.getLogger(__name__)

_WINDOWS = os.name == "nt"

# Win32 constants (processthreadapi.h / winnt.h)
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_JOB_OBJECT_ASSIGN_PROCESS = 0x0001
_JOB_OBJECT_QUERY = 0x0004
_JOB_OBJECT_TERMINATE = 0x0008


class JobScope:
    """One containment scope; owns at most one job handle."""

    def __init__(
        self,
        handle: int | None = None,
        *,
        kill_on_close: bool = False,
        job_id: str = "",
    ):
        self._handle = handle
        self.kill_on_close = bool(kill_on_close)
        self.job_id = job_id
        self._closed = False
        # POSIX fallback: the session/pgid of every child assigned here. Each
        # child is spawned with start_new_session=True, so it leads its own
        # group; a run scope therefore tracks one pgid per assigned child.
        self._pgids: set[int] = set()

    # -- construction ---------------------------------------------------- #

    @classmethod
    def create(cls, *, kill_on_close: bool = False) -> JobScope:
        """Create an unnamed job (Windows) or an empty session scope (POSIX)."""
        if not _WINDOWS:
            return cls(None, kill_on_close=kill_on_close)
        api = _kernel32()
        if api is None:
            return cls(None, kill_on_close=kill_on_close)
        handle = api.CreateJobObjectW(None, None)
        if not handle:
            logger.warning(
                "CreateJobObject failed (err %s); containment falls back to "
                "per-pid identity checks",
                api.GetLastError(),
            )
            return cls(None, kill_on_close=kill_on_close)
        scope = cls(handle, kill_on_close=kill_on_close, job_id=f"job-{handle:x}")
        if kill_on_close and not scope._set_kill_on_close():
            logger.warning("could not set KILL_ON_JOB_CLOSE on %s", scope.job_id)
        return scope

    def _set_kill_on_close(self) -> bool:
        api = _kernel32()
        if api is None or not self._handle:
            return False
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_STRUCT()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = api.SetInformationJobObject(
            self._handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            _byref(info),
            _sizeof(info),
        )
        return bool(ok)

    # -- membership ------------------------------------------------------ #

    @property
    def active(self) -> bool:
        """Whether this scope can still contain or terminate anything.

        On Windows that means a live job handle. On POSIX the scope is a set
        of session ids, so it is usable as soon as it exists (a child is
        assigned right after spawn).
        """
        if _WINDOWS:
            return bool(self._handle) and not self._closed
        return not self._closed

    def assign(self, pid: int) -> bool:
        """Put *pid* (and, automatically, everything it later spawns) in scope."""
        if not _WINDOWS:
            # start_new_session=True made the child its own group leader, so
            # its pgid equals its pid; the group is what terminate() signals.
            if self._closed:
                return False
            self._pgids.add(int(pid))
            return True
        if not self.active:
            return False
        api = _kernel32()
        if api is None:
            return False
        process_handle = api.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_SET_QUOTA,
            False,
            int(pid),
        )
        if not process_handle:
            logger.warning("OpenProcess(%s) failed while assigning job", pid)
            return False
        try:
            return bool(api.AssignProcessToJobObject(self._handle, process_handle))
        finally:
            api.CloseHandle(process_handle)

    def pids(self) -> list[int]:
        """Current member pids (empty when the scope is unavailable)."""
        if not _WINDOWS:
            members: list[int] = []
            for pgid in sorted(self._pgids):
                members.extend(_posix_group_members(pgid))
            return members
        if not self.active:
            return []
        api = _kernel32()
        if api is None:
            return []
        # The list grows between the first query and the fill query, so ask
        # twice and keep whatever the second call reports.
        size = 4096
        for _ in range(3):
            buffer = _create_string_buffer(size)
            returned = _dword()
            ok = api.QueryInformationJobObject(
                self._handle,
                _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                buffer,
                size,
                _byref(returned),
            )
            if not ok:
                return []
            count = _process_id_count(buffer)
            if count + 16 <= (size - 8) // 8:
                return _process_ids(buffer, count)
            size *= 2
        return []

    def contains(self, pid: int) -> bool:
        return int(pid) in self.pids()

    # -- termination ----------------------------------------------------- #

    def terminate(self) -> bool:
        """Terminate every member of this scope (nothing outside it)."""
        if not _WINDOWS:
            if not self._pgids:
                return False
            killed = False
            for pgid in sorted(self._pgids):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                    killed = True
                except OSError:
                    continue
            return killed
        if not self.active:
            return False
        api = _kernel32()
        if api is None:
            return False
        return bool(api.TerminateJobObject(self._handle, 1))

    # -- lifetime -------------------------------------------------------- #

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if _WINDOWS and self._handle:
            api = _kernel32()
            if api is not None:
                api.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"JobScope(job_id={self.job_id!r}, active={self.active})"


# --------------------------------------------------------------------------- #
# POSIX helpers
# --------------------------------------------------------------------------- #


def _posix_group_members(pgid: int) -> list[int]:
    try:
        import psutil
    except Exception:  # noqa: BLE001 - optional dependency
        return [pgid]
    members: list[int] = []
    for proc in psutil.process_iter(attrs=["pid"], ad_value=None):
        try:
            if os.getpgid(proc.pid) == pgid:
                members.append(proc.pid)
        except (OSError, psutil.Error):
            continue
    return members


# --------------------------------------------------------------------------- #
# ctypes plumbing (loaded once, lazily)
# --------------------------------------------------------------------------- #

_kernel32_cache: object = None
_structs_cache: dict | None = None


def _kernel32():
    global _kernel32_cache
    if not _WINDOWS:
        return None
    if _kernel32_cache is not None:
        return _kernel32_cache
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateJobObject.restype = wintypes.BOOL
    api.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    api.SetInformationJobObject.restype = wintypes.BOOL
    api.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    api.QueryInformationJobObject.restype = wintypes.BOOL
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.GetLastError.restype = wintypes.DWORD
    _kernel32_cache = api
    return api


def _structures():
    global _structs_cache
    if _structs_cache is not None:
        return _structs_cache
    import ctypes
    from ctypes import wintypes

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _structs_cache = {
        "extended": JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    }
    return _structs_cache


def _JOBOBJECT_EXTENDED_LIMIT_INFORMATION_STRUCT():
    return _structures()["extended"]()


def _create_string_buffer(size: int):
    import ctypes

    return ctypes.create_string_buffer(size)


def _dword():
    from ctypes import wintypes

    return wintypes.DWORD()


def _byref(obj):
    import ctypes

    return ctypes.byref(obj)


def _sizeof(obj) -> int:
    import ctypes

    return ctypes.sizeof(obj)


def _process_id_count(buffer) -> int:
    """JOBOBJECT_BASIC_PROCESS_ID_LIST.NumberOfProcessIdsInList (offset 4)."""
    import ctypes

    return int(ctypes.c_uint32.from_buffer(buffer, 4).value)


def _process_ids(buffer, count: int) -> list[int]:
    """ProcessIdList starts at offset 8 (ULONG_PTR array)."""
    import ctypes

    array = (ctypes.c_size_t * count).from_buffer(buffer, 8)
    return [int(pid) for pid in array]


__all__ = ["JobScope"]
