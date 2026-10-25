"""Checked Unix paths and ACLs. No shell commands or pathname-based ACL reads."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import struct
import sys
from pathlib import Path

_RESOURCE_ERRORS = {errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM, errno.EINTR}


class UnsafePath(ValueError):
    """A transport path cannot be trusted."""


def _acl_failure(message: str, code: int | None = None) -> None:
    code = ctypes.get_errno() if code is None else code
    if code in _RESOURCE_ERRORS:
        raise OSError(code, message)
    raise UnsafePath(message)


def _linux_acl(data: bytes, trusted: set[int], *, empty: bool) -> None:
    if len(data) < 4 or struct.unpack_from("<I", data)[0] != 2 or (len(data) - 4) % 8:
        raise UnsafePath("Malformed POSIX ACL")
    entries = list(struct.iter_unpack("<HHI", data[4:]))
    if empty and entries:
        raise UnsafePath("Setup directory has an ACL")
    mask = next((perm for tag, perm, _ in entries if tag == 16), 7)
    for tag, perm, uid in entries:
        if tag not in {1, 2, 4, 8, 16, 32} or perm & ~7:
            raise UnsafePath("Unknown POSIX ACL entry")
        if tag in {1, 16} or (tag == 2 and uid in trusted):
            continue
        if (perm & (mask if tag in {2, 4, 8} else 7)) & 2:
            raise UnsafePath("ACL grants write to another principal")


def check_acl(fd: int, trusted: set[int], *, empty: bool = False) -> None:
    """Inspect the opened inode; unsupported inspection fails closed.

    Linux access AND default ACLs are checked. Darwin's acl_get_fd_np is the
    descriptor equivalent of acl_get_link_np after our O_NOFOLLOW open.
    """
    if sys.platform == "linux":
        for name in ("system.posix_acl_access", "system.posix_acl_default"):
            try:
                data = os.getxattr(fd, name)
            except OSError as exc:
                if exc.errno in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
                    continue
                if exc.errno in _RESOURCE_ERRORS:
                    raise
                raise UnsafePath("Cannot inspect POSIX ACL") from exc
            _linux_acl(data, trusted, empty=empty)
        return
    if sys.platform != "darwin":
        raise UnsafePath("ACL inspection is unsupported on this platform")
    lib = ctypes.CDLL(None, use_errno=True)
    pointer = ctypes.c_void_p
    signatures = {
        "acl_get_fd_np": ([ctypes.c_int, ctypes.c_int], pointer),
        "acl_get_entry": ([pointer, ctypes.c_int, ctypes.POINTER(pointer)], ctypes.c_int),
        "acl_get_tag_type": ([pointer, ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
        "acl_get_permset_mask_np": ([pointer, ctypes.POINTER(ctypes.c_uint64)], ctypes.c_int),
        "acl_get_qualifier": ([pointer], pointer),
        "acl_free": ([pointer], ctypes.c_int),
        "mbr_uid_to_uuid": ([ctypes.c_uint, pointer], ctypes.c_int),
    }
    try:
        for name, (args, result) in signatures.items():
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = args, result
    except AttributeError as exc:
        raise UnsafePath("Cannot inspect Darwin ACL") from exc
    trusted_uuids = set()
    for uid in trusted:
        uuid = ctypes.create_string_buffer(16)
        status = lib.mbr_uid_to_uuid(uid, uuid)
        if status != 0:
            _acl_failure("Cannot resolve ACL principal", status)
        trusted_uuids.add(uuid.raw)
    acl = lib.acl_get_fd_np(fd, 0x100)  # ACL_TYPE_EXTENDED
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:  # Darwin: inode has no extended ACL.
            return
        _acl_failure("Cannot inspect Darwin ACL")
    try:
        entry = pointer()
        which = 0  # ACL_FIRST_ENTRY; ACL_NEXT_ENTRY is -1 on Darwin.
        while True:
            status = lib.acl_get_entry(acl, which, ctypes.byref(entry))
            if status != 0:
                # Darwin returns -1/EINVAL at the end (unlike POSIX's 0).
                if status == -1 and ctypes.get_errno() == errno.EINVAL:
                    break
                _acl_failure("Cannot enumerate Darwin ACL")
            which = -1
            if empty:
                raise UnsafePath("Setup directory has an ACL")
            tag, mask = ctypes.c_int(), ctypes.c_uint64()
            if lib.acl_get_tag_type(entry, ctypes.byref(tag)) or lib.acl_get_permset_mask_np(entry, ctypes.byref(mask)):
                _acl_failure("Cannot inspect Darwin ACL entry")
            if tag.value == 2:  # ACL_EXTENDED_DENY
                continue
            if tag.value != 1:
                raise UnsafePath("Unknown Darwin ACL entry")
            # Include metadata/ACL ownership writes, which could enable data writes.
            if mask.value & sum(1 << bit for bit in (2, 4, 5, 6, 8, 10, 12, 13)):
                qualifier = lib.acl_get_qualifier(entry)
                if not qualifier:
                    _acl_failure("Cannot inspect Darwin ACL principal")
                try:
                    if ctypes.string_at(qualifier, 16) not in trusted_uuids:
                        raise UnsafePath("ACL grants write to another principal")
                finally:
                    lib.acl_free(qualifier)
    finally:
        lib.acl_free(acl)


_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_HELPER_PATHS = {"/Users/Shared/orch", "/Users/Shared/orch/run", "/run/orch"}


def check_directory(fd: int, path: Path, host_uid: int, *, helper: bool = False) -> bool:
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid not in {0, host_uid}:
        raise UnsafePath(f"{path}: owner {info.st_uid}, expected root or {host_uid}")
    if helper or str(path) in _HELPER_PATHS:
        if info.st_uid != 0 or mode != 0o755:
            raise UnsafePath(f"{path}: setup directory owner {info.st_uid}, mode {mode:o}; expected root 0755")
        check_acl(fd, {0}, empty=True)
        return False
    sticky = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
    if mode & 0o022 and not sticky:
        raise UnsafePath(f"{path}: directory writable by another principal")
    check_acl(fd, {0, host_uid})
    return sticky


class CheckedDirectory:
    """Retain every ancestor fd and identity; detect replacement as well as chmod.

    Only the final directory may be created. Root-owned setup directories are
    provisioned separately by H3c; the host never repairs foreign ownership.
    """

    def __init__(
        self,
        path: Path,
        host_uid: int,
        *,
        mode: int,
        gid: int | None = None,
        create: bool = True,
        strict_acl: bool = False,
    ):
        """``strict_acl``: the final directory carries no ACL at all (tier B, spec 13.3: the group is the only share).
        A setgid ``mode`` (``0o2750``) must come out exactly: a directory made inside a setgid parent inherits the
        bit, and it is never "repaired" by a chmod that a non-member would silently lose it in."""
        self.path = Path(path)
        if not self.path.is_absolute() or ".." in self.path.parts:
            raise UnsafePath("Expected an absolute path without parent traversal")
        self.host_uid, self.mode, self.gid, self.strict_acl = host_uid, mode, gid, strict_acl
        self.fds: list[int] = []
        self.identities: list[tuple[int, int]] = []
        try:
            fd = os.open("/", _FLAGS)
            self.fds.append(fd)
            parent_sticky = check_directory(fd, Path("/"), host_uid)
            current = Path("/")
            for index, part in enumerate(self.path.parts[1:]):
                current /= part
                final = index == len(self.path.parts) - 2
                if final and create:
                    try:
                        os.mkdir(part, mode, dir_fd=fd)
                    except FileExistsError:
                        pass
                    else:
                        child = os.open(part, _FLAGS, dir_fd=fd)
                        try:
                            if gid is not None:
                                os.fchown(child, -1, gid)
                            if stat.S_IMODE(os.fstat(child).st_mode) != mode:
                                os.fchmod(child, mode)
                        finally:
                            os.close(child)
                fd = os.open(part, _FLAGS, dir_fd=fd)
                self.fds.append(fd)
                parent_sticky = check_directory(fd, current, host_uid, helper=parent_sticky)
            if parent_sticky:
                raise UnsafePath("Run directory cannot itself be a sticky ancestor")
            info = os.fstat(fd)
            if info.st_uid != host_uid or stat.S_IMODE(info.st_mode) != mode:
                raise UnsafePath(f"{path}: expected owner {host_uid} and mode {mode:o}, got {info.st_uid}")
            if gid is not None and info.st_gid != gid:
                raise UnsafePath(f"{path}: unexpected group {info.st_gid}")
            if strict_acl:
                check_acl(fd, {0}, empty=True)
            self.identities = [(os.fstat(item).st_dev, os.fstat(item).st_ino) for item in self.fds]
        except BaseException:
            self.close()
            raise

    @property
    def fd(self) -> int:
        return self.fds[-1]

    def recheck(self) -> None:
        other = CheckedDirectory(
            self.path, self.host_uid, mode=self.mode, gid=self.gid, create=False, strict_acl=self.strict_acl
        )
        try:
            if other.identities != self.identities:
                raise UnsafePath(f"{self.path}: ancestor was replaced")
        finally:
            other.close()

    def close(self) -> None:
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()
