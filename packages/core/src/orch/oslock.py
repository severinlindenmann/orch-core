"""One advisory file-lock layer: ``fcntl.flock`` on POSIX, ``LockFileEx`` (ctypes) on Windows.

``flock(fd, op)`` has the POSIX call shape: ``op`` is ``LOCK_SH`` or ``LOCK_EX`` (optionally ``| LOCK_NB``) or
``LOCK_UN``. A non-blocking request that cannot be granted raises :class:`BlockingIOError` on both platforms.
On POSIX this is ``fcntl.flock`` itself, unchanged.
"""

from __future__ import annotations

import os

__all__ = ["LOCK_EX", "LOCK_NB", "LOCK_SH", "LOCK_UN", "flock"]

if os.name != "nt":
    import fcntl as _fcntl

    LOCK_SH = _fcntl.LOCK_SH
    LOCK_EX = _fcntl.LOCK_EX
    LOCK_NB = _fcntl.LOCK_NB
    LOCK_UN = _fcntl.LOCK_UN
    flock = _fcntl.flock
else:  # pragma: no cover - exercised by the Windows smoke job
    import ctypes
    import msvcrt
    from ctypes import wintypes

    LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8
    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_IO_PENDING = 997

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _k32.LockFileEx.restype = wintypes.BOOL
    _k32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _k32.UnlockFileEx.restype = wintypes.BOOL

    def flock(fd: int, op: int) -> None:
        handle = msvcrt.get_osfhandle(fd)
        ov = _Overlapped()
        if op & LOCK_UN:
            if not _k32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(ov)):
                raise ctypes.WinError(ctypes.get_last_error())
            return
        flags = _LOCKFILE_EXCLUSIVE_LOCK if op & LOCK_EX else 0
        if op & LOCK_NB:
            flags |= _LOCKFILE_FAIL_IMMEDIATELY
        if not _k32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(ov)):
            err = ctypes.get_last_error()
            if err in (_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING):
                raise BlockingIOError(err, "the file is locked by another handle")
            raise ctypes.WinError(err)
