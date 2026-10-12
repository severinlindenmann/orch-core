"""Process facts for the runner (D-P2-29): birth identity, enumeration and signals that never hit a reused PID.

A process is named by its **birth identity** ``(pid, birth)``: ``birth`` is the kernel's start time of that process,
an opaque integer comparable only on the same machine and boot (macOS: ``proc_pidinfo(PROC_PIDTBSDINFO)`` start time in
microseconds since the epoch; Linux: ``/proc/<pid>/stat`` field 22, clock ticks since boot). Every signal the runner
sends goes through :func:`signal_verified`, which re-reads the birth time right before ``kill`` and does nothing when
it differs: a PID that was reused by another program is never signalled.

* Linux: the check and the signal go through a pidfd (``pidfd_open``, then the birth time is re-read, then
  ``pidfd_send_signal``), so the PID cannot be reused between the check and the signal.
* macOS has no pidfd: the window between the re-read and ``kill`` is a few microseconds. The runner's own leader
  processes are its unreaped children, whose PIDs cannot be reused at all; for every other process the window is a
  stated best effort.

Enumeration (:func:`snapshot`) lists the processes of one UID with parent, process group and session. It is used by
the macOS sweep and the escape scan (:mod:`orch.runner.supervise`), never to kill by name or UID-wide.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys
from dataclasses import dataclass

__all__ = ["ProcInfo", "alive", "birth", "info", "signal_verified", "snapshot"]


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    pgid: int
    sid: int
    uid: int
    birth: int
    zombie: bool = False


# -- macOS: libproc --------------------------------------------------------------------------------------------------

_PROC_PIDTBSDINFO = 3
_BSDINFO_SIZE = 136
_SZOMB = 5
_lib = None


def _libproc():  # pragma: no cover - exercised on macOS only
    global _lib
    if _lib is None:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.proc_listallpids.restype = ctypes.c_int
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidinfo.restype = ctypes.c_int
        _lib = lib
    return _lib


def _darwin_info(pid: int) -> ProcInfo | None:  # pragma: no cover - macOS
    lib = _libproc()
    buf = ctypes.create_string_buffer(_BSDINFO_SIZE)
    n = lib.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, buf, _BSDINFO_SIZE)
    if n != _BSDINFO_SIZE:
        return None
    raw = buf.raw
    status, got_pid, ppid, uid = (int.from_bytes(raw[o : o + 4], sys.byteorder) for o in (4, 12, 16, 20))
    pgid = int.from_bytes(raw[100:104], sys.byteorder)
    sec = int.from_bytes(raw[120:128], sys.byteorder)
    usec = int.from_bytes(raw[128:136], sys.byteorder)
    if got_pid != pid:
        return None
    try:
        sid = os.getsid(pid)
    except OSError:
        sid = -1
    return ProcInfo(pid, ppid, pgid, sid, uid, sec * 1_000_000 + usec, status == _SZOMB)


def _darwin_pids() -> list[int]:  # pragma: no cover - macOS
    lib = _libproc()
    count = lib.proc_listallpids(None, 0)
    if count <= 0:
        raise OSError(ctypes.get_errno(), "proc_listallpids failed")
    for _ in range(4):
        size = count + 64
        arr = (ctypes.c_int * size)()
        got = lib.proc_listallpids(arr, ctypes.sizeof(arr))
        if got < 0:
            raise OSError(ctypes.get_errno(), "proc_listallpids failed")
        if got < size:
            return [p for p in arr[:got] if p > 0]
        count = got * 2
    raise OSError("the process table kept growing")


# -- Linux: /proc ----------------------------------------------------------------------------------------------------


def _linux_info(pid: int) -> ProcInfo | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read()
        uid = os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None
    end = raw.rfind(b")")
    if end < 0:
        return None
    fields = raw[end + 2 :].split()
    try:
        state = fields[0]
        ppid, pgid, sid = int(fields[1]), int(fields[2]), int(fields[3])
        start = int(fields[19])  # field 22: starttime
    except (IndexError, ValueError):
        return None
    return ProcInfo(pid, ppid, pgid, sid, uid, start, state in (b"Z", b"X"))


def _linux_pids() -> list[int]:
    return [int(name) for name in os.listdir("/proc") if name.isdigit()]


# -- public ----------------------------------------------------------------------------------------------------------


def info(pid: int) -> ProcInfo | None:
    """The process ``pid`` now, or ``None`` when it does not exist (or cannot be read)."""
    if pid <= 0:
        return None
    if sys.platform == "darwin":
        return _darwin_info(pid)
    if sys.platform.startswith("linux"):
        return _linux_info(pid)
    raise OSError("process facts are unsupported on this platform")


def birth(pid: int) -> int | None:
    found = info(pid)
    return None if found is None else found.birth


def snapshot(uid: int | None = None) -> dict[int, ProcInfo]:
    """Every readable process (of ``uid`` when given), by PID. A process that ends while it is read is left out."""
    pids = _darwin_pids() if sys.platform == "darwin" else _linux_pids()
    out: dict[int, ProcInfo] = {}
    for pid in pids:
        found = info(pid)
        if found is not None and (uid is None or found.uid == uid):
            out[pid] = found
    return out


def signal_verified(pid: int, expected_birth: int, sig: int) -> bool:
    """Send ``sig`` to ``pid`` only if it is still the process born at ``expected_birth``. Returns whether it was
    sent. Never signals a process group, never PID 0, 1 or a negative PID."""
    if pid <= 1:
        return False
    pidfd_open = getattr(os, "pidfd_open", None)
    send = getattr(signal, "pidfd_send_signal", None)
    if sys.platform.startswith("linux") and pidfd_open is not None and send is not None:
        try:
            fd = pidfd_open(pid)
        except OSError:
            return False
        try:
            if birth(pid) != expected_birth:  # the pidfd pins the process: this is the one it names
                return False
            try:
                send(fd, sig)
            except ProcessLookupError:
                return False
            return True
        finally:
            os.close(fd)
    if birth(pid) != expected_birth:
        return False
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def alive(pid: int, expected_birth: int) -> bool:
    """Whether the process born at ``expected_birth`` still exists and is not a zombie."""
    found = info(pid)
    return found is not None and found.birth == expected_birth and not found.zombie
