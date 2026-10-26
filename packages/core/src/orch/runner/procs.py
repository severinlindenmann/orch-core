"""Process facts for the runner (D-P2-29): birth identity, enumeration and signals that never hit a reused PID.

A process is named by its **birth identity** ``(pid, birth)``: ``birth`` is the kernel's start time of that process,
an opaque integer comparable only on the same machine and boot (macOS: ``sysctl(KERN_PROC)`` ``p_starttime`` in
microseconds since the epoch, readable for every UID; Linux: ``/proc/<pid>/stat`` field 22, clock ticks since boot).
Every signal the runner sends goes through :func:`signal_verified`, which re-reads the birth time right before
``kill`` and does nothing when it differs: a PID that was reused by another program is never signalled.

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
import struct
import sys
from dataclasses import dataclass

__all__ = ["ProcInfo", "alive", "birth", "boot_id", "info", "signal_verified", "snapshot"]


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    pgid: int
    sid: int
    uid: int
    birth: int
    zombie: bool = False


# -- macOS: sysctl KERN_PROC (what ps uses; it sees processes of every UID, unlike proc_pidinfo) -------------------

_CTL_KERN, _KERN_PROC, _KERN_PROC_ALL, _KERN_PROC_PID = 1, 14, 0, 1
_KINFO_SIZE = 648  # struct kinfo_proc (LP64)
_SZOMB = 5
_libc = None


def _libsys():  # pragma: no cover - macOS
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    return _libc


def _sysctl(mib: list[int]) -> bytes:  # pragma: no cover - macOS
    lib = _libsys()
    arr = (ctypes.c_int * len(mib))(*mib)
    for _ in range(4):
        size = ctypes.c_size_t(0)
        if lib.sysctl(arr, len(mib), None, ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
            raise OSError(ctypes.get_errno(), "sysctl KERN_PROC")
        want = size.value + 64 * _KINFO_SIZE
        buf = ctypes.create_string_buffer(want)
        size = ctypes.c_size_t(want)
        if lib.sysctl(arr, len(mib), buf, ctypes.byref(size), None, ctypes.c_size_t(0)) == 0:
            return buf.raw[: size.value]
        if ctypes.get_errno() != 12:  # ENOMEM: the table grew, try again
            raise OSError(ctypes.get_errno(), "sysctl KERN_PROC")
    raise OSError("the process table kept growing")


def _kinfo(raw: bytes, off: int) -> ProcInfo | None:  # pragma: no cover - macOS
    sec, usec = struct.unpack_from("=qi", raw, off)  # kp_proc.p_starttime
    stat = raw[off + 36]  # kp_proc.p_stat
    pid = struct.unpack_from("=i", raw, off + 40)[0]  # kp_proc.p_pid
    ruid = struct.unpack_from("=I", raw, off + 392)[0]  # kp_eproc.e_pcred.p_ruid
    ppid, pgid = struct.unpack_from("=ii", raw, off + 560)  # kp_eproc.e_ppid, e_pgid
    if pid <= 0:
        return None
    try:
        sid = os.getsid(pid)
    except OSError:
        sid = -1
    return ProcInfo(pid, ppid, pgid, sid, ruid, sec * 1_000_000 + usec, stat == _SZOMB)


def _darwin_info(pid: int) -> ProcInfo | None:  # pragma: no cover - macOS
    raw = _sysctl([_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, pid])
    if len(raw) < _KINFO_SIZE:
        return None
    found = _kinfo(raw, 0)
    return found if found is not None and found.pid == pid else None


def _darwin_snapshot() -> dict[int, ProcInfo]:  # pragma: no cover - macOS
    raw = _sysctl([_CTL_KERN, _KERN_PROC, _KERN_PROC_ALL, 0])
    out = {}
    for off in range(0, len(raw) - _KINFO_SIZE + 1, _KINFO_SIZE):
        found = _kinfo(raw, off)
        if found is not None:
            out[found.pid] = found
    return out


# -- Linux: /proc ----------------------------------------------------------------------------------------------------


def _linux_uid(pid: int) -> int:
    """The real UID from ``/proc/<pid>/status`` (the directory's owner is root for a non-dumpable process)."""
    with open(f"/proc/{pid}/status", "rb") as f:
        for line in f:
            if line.startswith(b"Uid:"):
                return int(line.split()[1])
    raise OSError("no Uid line")


def _linux_info(pid: int) -> ProcInfo | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read()
        uid = _linux_uid(pid)
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
    if sys.platform == "darwin":
        every = _darwin_snapshot()
        return {p: i for p, i in every.items() if uid is None or i.uid == uid}
    pids = _linux_pids()
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
            except OSError:  # gone, or no longer ours to signal (a setuid exec): not sent
                return False
            return True
        finally:
            os.close(fd)
    if birth(pid) != expected_birth:
        return False
    try:
        os.kill(pid, sig)
    except OSError:  # gone, or not ours to signal
        return False
    return True


_BOOT: str | None = None


def boot_id() -> str:
    """This boot of the machine (Linux ``boot_id``; macOS ``kern.boottime``): birth times compare only within one."""
    global _BOOT
    if _BOOT is None:
        if sys.platform.startswith("linux"):
            with open("/proc/sys/kernel/random/boot_id") as f:
                _BOOT = f.read().strip()
        else:  # pragma: no cover - macOS: kern.bootsessionuuid (kern.boottime moves when the clock is stepped)
            lib = ctypes.CDLL(None, use_errno=True)
            buf = ctypes.create_string_buffer(64)
            size = ctypes.c_size_t(64)
            if lib.sysctlbyname(b"kern.bootsessionuuid", buf, ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
                raise OSError(ctypes.get_errno(), "kern.bootsessionuuid")
            _BOOT = buf.value.decode()
    return _BOOT


def alive(pid: int, expected_birth: int) -> bool:
    """Whether the process born at ``expected_birth`` still exists and is not a zombie."""
    found = info(pid)
    return found is not None and found.birth == expected_birth and not found.zombie
