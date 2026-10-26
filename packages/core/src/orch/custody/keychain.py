"""macOS traditional (file-based) keychain store for opaque secret bytes, via ``ctypes``.

This talks to ``Security.framework`` and ``CoreFoundation.framework`` directly; it never shells out to ``security``
and never puts secret bytes in a command line, a log line or an exception message.

What this is, and is not:

* It is the *traditional file-based* keychain (by default ``~/Library/Keychains/login.keychain-db``), opened by path
  with ``SecKeychainOpen``; never the default keychain and never the search list. Items are encrypted at rest under
  the keychain password.
* The file MAY appear in Time Machine backups and in migrations to another Mac. No ``ThisDeviceOnly`` semantics are
  claimed.
* It is NOT the data-protection keychain: that needs entitlements an unsigned interpreter lacks, so
  ``kSecUseDataProtectionKeychain``, ``kSecAttrAccessControl`` and ``kSecAttrAccessible`` are never used.
* The item's ACL is the default one ``SecItemAdd`` creates for a legacy keychain: it trusts the creating application,
  i.e. the interpreter that ran this code. If the interpreter changes (a new Python build, a different venv), macOS
  may ask for access. Every read here runs with user interaction disabled, so that case (and a locked keychain)
  surfaces as ``custody.unavailable`` (:class:`~orch.custody.base.BackendUnavailable`) instead of a dialog.

Not imported by ``orch.custody``; import lazily and only on darwin. The ``create_throwaway_keychain`` family at the
end is for tests only.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import sys
from collections.abc import Iterator
from ctypes import POINTER, byref, c_char_p, c_int32, c_long, c_uint8, c_uint32, c_ulong, c_void_p
from pathlib import Path

from orch.custody.base import BackendUnavailable, CustodyError, KeyExists, KeyNotFound

__all__ = [
    "SERVICE",
    "KeychainStore",
    "create_throwaway_keychain",
    "delete_keychain",
    "lock_keychain",
    "unlock_keychain",
]

SERVICE = "io.orch.device-key"

_ACCOUNT = re.compile(r"[0-9a-f]{32}/d_[0-9a-f]{32}")
_ACCOUNT_PREFIX = re.compile(r"[0-9a-f/_d]{0,70}")

_OK = 0
_ITEM_NOT_FOUND = -25300
_DUPLICATE = -25299
_INTERACTION_NOT_ALLOWED = -25308
_AUTH_FAILED = -25293
_MISSING_ENTITLEMENT = -34018
_UNAVAILABLE_CODES = {
    _INTERACTION_NOT_ALLOWED: "interaction not allowed",
    _AUTH_FAILED: "authorization failed",
    _MISSING_ENTITLEMENT: "missing entitlement",
    -25294: "keychain not found",
    -25295: "keychain invalid",
    -128: "user cancelled",
}
_UTF8 = 0x08000100

_LOCK_HINT = "unlock the login keychain or re-allow access for this interpreter, then retry"

_libs: tuple[ctypes.CDLL, ctypes.CDLL] | None = None


def _load() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    global _libs
    if sys.platform != "darwin":
        raise BackendUnavailable("macOS keychain is only available on macOS")
    if _libs is None:
        try:
            cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
            _declare(cf, sec)
        except (OSError, AttributeError) as exc:
            raise BackendUnavailable("cannot load Security.framework") from exc
        _libs = (cf, sec)
    return _libs


def _declare(cf: ctypes.CDLL, sec: ctypes.CDLL) -> None:
    def d(lib: ctypes.CDLL, name: str, restype: object, *argtypes: object) -> None:
        fn = getattr(lib, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)

    p = c_void_p
    d(cf, "CFRelease", None, p)
    d(cf, "CFGetTypeID", c_ulong, p)
    d(cf, "CFDataGetTypeID", c_ulong)
    d(cf, "CFArrayGetTypeID", c_ulong)
    d(cf, "CFDictionaryGetTypeID", c_ulong)
    d(cf, "CFStringGetTypeID", c_ulong)
    d(cf, "CFDataCreate", p, p, c_char_p, c_long)
    d(cf, "CFDataGetLength", c_long, p)
    d(cf, "CFDataGetBytePtr", POINTER(c_uint8), p)
    d(cf, "CFStringCreateWithCString", p, p, c_char_p, c_uint32)
    d(cf, "CFStringGetLength", c_long, p)
    d(cf, "CFStringGetMaximumSizeForEncoding", c_long, c_long, c_uint32)
    d(cf, "CFStringGetCString", ctypes.c_ubyte, p, ctypes.c_char_p, c_long, c_uint32)
    d(cf, "CFArrayCreate", p, p, POINTER(p), c_long, p)
    d(cf, "CFArrayGetCount", c_long, p)
    d(cf, "CFArrayGetValueAtIndex", p, p, c_long)
    d(cf, "CFDictionaryCreate", p, p, POINTER(p), POINTER(p), c_long, p, p)
    d(cf, "CFDictionaryGetValue", p, p, p)
    d(sec, "SecKeychainOpen", c_int32, c_char_p, POINTER(p))
    d(sec, "SecKeychainCreate", c_int32, c_char_p, c_uint32, c_char_p, ctypes.c_ubyte, p, POINTER(p))
    d(sec, "SecKeychainDelete", c_int32, p)
    d(sec, "SecKeychainUnlock", c_int32, p, c_uint32, c_char_p, ctypes.c_ubyte)
    d(sec, "SecKeychainLock", c_int32, p)
    d(sec, "SecKeychainGetPath", c_int32, p, POINTER(c_uint32), ctypes.c_char_p)
    d(sec, "SecKeychainSetUserInteractionAllowed", c_int32, ctypes.c_ubyte)
    d(sec, "SecKeychainGetUserInteractionAllowed", c_int32, POINTER(ctypes.c_ubyte))
    d(sec, "SecItemAdd", c_int32, p, POINTER(p))
    d(sec, "SecItemCopyMatching", c_int32, p, POINTER(p))
    d(sec, "SecItemDelete", c_int32, p)


class _CF:
    """Constants and small helpers; every Create-rule object is released by its caller or the ``owned`` context."""

    def __init__(self) -> None:
        self.cf, self.sec = _load()

    def const(self, name: str) -> int:
        for lib in (self.sec, self.cf):
            try:
                value = c_void_p.in_dll(lib, name).value
            except ValueError:
                continue
            if value:
                return value
        raise BackendUnavailable(f"missing framework constant {name}")

    def callbacks(self, name: str) -> int:
        for lib in (self.cf, self.sec):
            try:
                return ctypes.addressof(ctypes.c_char.in_dll(lib, name))
            except ValueError:
                continue
        raise BackendUnavailable(f"missing framework constant {name}")

    def string(self, text: str) -> int:
        ref = self.cf.CFStringCreateWithCString(None, text.encode("utf-8"), _UTF8)
        if not ref:
            raise CustodyError("keychain error: cannot build string")
        return ref

    def data(self, raw: bytes) -> int:
        ref = self.cf.CFDataCreate(None, raw, len(raw))
        if not ref:
            raise CustodyError("keychain error: cannot build data")
        return ref

    def read_string(self, ref: int) -> str | None:
        if not ref or self.cf.CFGetTypeID(ref) != self.cf.CFStringGetTypeID():
            return None
        size = self.cf.CFStringGetMaximumSizeForEncoding(self.cf.CFStringGetLength(ref), _UTF8) + 1
        buf = ctypes.create_string_buffer(size)
        if not self.cf.CFStringGetCString(ref, buf, size, _UTF8):
            return None
        return buf.value.decode("utf-8")

    def read_data(self, ref: int) -> bytes:
        if not ref or self.cf.CFGetTypeID(ref) != self.cf.CFDataGetTypeID():
            raise CustodyError("keychain error: unexpected result type")
        n = self.cf.CFDataGetLength(ref)
        ptr = self.cf.CFDataGetBytePtr(ref)
        buf = bytearray(n)
        if n:
            ctypes.memmove((ctypes.c_char * n).from_buffer(buf), ptr, n)
        out = bytes(buf)
        for i in range(n):  # best-effort scrub of the intermediate copy
            buf[i] = 0
        return out

    def dictionary(self, items: list[tuple[int, int]]) -> int:
        n = len(items)
        keys = (c_void_p * n)(*[k for k, _ in items])
        vals = (c_void_p * n)(*[v for _, v in items])
        ref = self.cf.CFDictionaryCreate(
            None,
            keys,
            vals,
            n,
            self.callbacks("kCFTypeDictionaryKeyCallBacks"),
            self.callbacks("kCFTypeDictionaryValueCallBacks"),
        )
        if not ref:
            raise CustodyError("keychain error: cannot build query")
        return ref

    def array(self, values: list[int]) -> int:
        n = len(values)
        arr = (c_void_p * n)(*values)
        ref = self.cf.CFArrayCreate(None, arr, n, self.callbacks("kCFTypeArrayCallBacks"))
        if not ref:
            raise CustodyError("keychain error: cannot build array")
        return ref


@contextlib.contextmanager
def _owned() -> Iterator[list[int]]:
    """Collect Create-rule refs; release them all at exit."""
    refs: list[int] = []
    try:
        yield refs
    finally:
        cf, _ = _load()
        for ref in reversed(refs):
            cf.CFRelease(ref)


def _raise_status(status: int, *, where: str = "") -> None:
    if status == _OK:
        return
    if status == _DUPLICATE:
        raise KeyExists("keychain item already exists")
    if status == _ITEM_NOT_FOUND:
        raise KeyNotFound("keychain item not found")
    if status in _UNAVAILABLE_CODES:
        raise BackendUnavailable(f"keychain unavailable ({_UNAVAILABLE_CODES[status]}, {status}): {_LOCK_HINT}")
    raise CustodyError(f"keychain error {status}{where}")


def _check_match_count(count: int) -> None:
    """Fail closed: exactly one item may match an account."""
    if count == 0:
        raise KeyNotFound("keychain item not found")
    if count > 1:
        raise CustodyError("ambiguous keychain items for this account; refusing to pick one")


def _check_text(name: str, value: str) -> str:
    if type(value) is not str or not value or len(value) > 200 or not value.isprintable():
        raise CustodyError(f"bad keychain {name}")
    return value


def _check_account(account: str) -> str:
    if type(account) is not str or not _ACCOUNT.fullmatch(account):
        raise CustodyError("bad keychain account")
    return account


@contextlib.contextmanager
def _no_interaction() -> Iterator[None]:
    _, sec = _load()
    prev = ctypes.c_ubyte(1)
    sec.SecKeychainGetUserInteractionAllowed(byref(prev))
    sec.SecKeychainSetUserInteractionAllowed(0)
    try:
        yield
    finally:
        sec.SecKeychainSetUserInteractionAllowed(prev.value)


def _open(path: str | os.PathLike[str]) -> int:
    _, sec = _load()
    ref = c_void_p()
    status = sec.SecKeychainOpen(os.fsencode(os.fspath(path)), byref(ref))
    if status != _OK or not ref.value:
        raise BackendUnavailable(f"cannot open keychain ({status})")
    return ref.value


class KeychainStore:
    def __init__(self, keychain_path: str | os.PathLike[str] | None = None) -> None:
        self._cf = _CF()  # raises BackendUnavailable off macOS
        if keychain_path is None:
            keychain_path = Path.home() / "Library" / "Keychains" / "login.keychain-db"
        self._path = os.fspath(keychain_path)
        self._ref: int | None = _open(self._path)

    # --- helpers -------------------------------------------------------------------------------------------
    @property
    def path(self) -> str:
        """The path the opened keychain reports (``SecKeychainGetPath``), for tests and diagnostics."""
        ref = self._need()
        n = c_uint32(2048)
        buf = ctypes.create_string_buffer(2048)
        _raise_status(self._cf.sec.SecKeychainGetPath(ref, byref(n), buf))
        return buf.raw[: n.value].decode("utf-8")

    def _need(self) -> int:
        if self._ref is None:
            raise CustodyError("keychain store is closed")
        return self._ref

    def _base(self, owned: list[int], account: str | None) -> list[tuple[int, int]]:
        c = self._cf
        items = [
            (c.const("kSecClass"), c.const("kSecClassGenericPassword")),
            (c.const("kSecAttrService"), self._own(owned, c.string(SERVICE))),
        ]
        if account is not None:
            items.append((c.const("kSecAttrAccount"), self._own(owned, c.string(account))))
        return items

    @staticmethod
    def _own(owned: list[int], ref: int) -> int:
        owned.append(ref)
        return ref

    def _query(self, owned: list[int], account: str | None, extra: list[tuple[int, int]]) -> int:
        c = self._cf
        ref = self._need()
        search = self._own(owned, c.array([ref]))
        items = self._base(owned, account) + [(c.const("kSecMatchSearchList"), search)] + extra
        return self._own(owned, c.dictionary(items))

    def _copy(self, query: int) -> int:
        out = c_void_p()
        status = self._cf.sec.SecItemCopyMatching(query, byref(out))
        if status != _OK:
            _raise_status(status)
        if not out.value:
            raise CustodyError("keychain error: empty result")
        return out.value

    def _count(self, account: str) -> int:
        c = self._cf
        with _owned() as owned, _no_interaction():
            query = self._query(
                owned,
                account,
                [
                    (c.const("kSecMatchLimit"), c.const("kSecMatchLimitAll")),
                    (c.const("kSecReturnAttributes"), c.const("kCFBooleanTrue")),
                ],
            )
            out = c_void_p()
            status = c.sec.SecItemCopyMatching(query, byref(out))
            if status == _ITEM_NOT_FOUND:
                return 0
            _raise_status(status)
            if not out.value:
                return 0
            owned.append(out.value)
            if c.cf.CFGetTypeID(out.value) != c.cf.CFArrayGetTypeID():
                raise CustodyError("keychain error: unexpected result type")
            return int(c.cf.CFArrayGetCount(out.value))

    # --- API -----------------------------------------------------------------------------------------------
    def put(self, account: str, secret: bytes, *, label: str, description: str) -> None:
        _check_account(account)
        _check_text("label", label)
        _check_text("description", description)
        if not isinstance(secret, (bytes, bytearray)) or not secret:
            raise CustodyError("bad keychain secret")
        c = self._cf
        ref = self._need()
        with _owned() as owned, _no_interaction():
            items = self._base(owned, account) + [
                (c.const("kSecAttrLabel"), self._own(owned, c.string(label))),
                (c.const("kSecAttrDescription"), self._own(owned, c.string(description))),
                (c.const("kSecAttrSynchronizable"), c.const("kCFBooleanFalse")),
                (c.const("kSecUseKeychain"), ref),
                (c.const("kSecValueData"), self._own(owned, c.data(bytes(secret)))),
            ]
            attrs = self._own(owned, c.dictionary(items))
            _raise_status(c.sec.SecItemAdd(attrs, None))

    def get(self, account: str) -> bytes:
        _check_account(account)
        _check_match_count(self._count(account))
        c = self._cf
        with _owned() as owned, _no_interaction():
            query = self._query(
                owned,
                account,
                [
                    (c.const("kSecMatchLimit"), c.const("kSecMatchLimitOne")),
                    (c.const("kSecReturnData"), c.const("kCFBooleanTrue")),
                ],
            )
            out = self._copy(query)
            owned.append(out)
            return c.read_data(out)

    def exists(self, account: str) -> bool:
        _check_account(account)
        return self._count(account) > 0

    def delete(self, account: str) -> None:
        _check_account(account)
        _check_match_count(self._count(account))
        c = self._cf
        with _owned() as owned, _no_interaction():
            query = self._query(owned, account, [])
            _raise_status(c.sec.SecItemDelete(query))

    def accounts(self, prefix: str) -> list[str]:
        if type(prefix) is not str or not _ACCOUNT_PREFIX.fullmatch(prefix):
            raise CustodyError("bad keychain account prefix")
        c = self._cf
        with _owned() as owned, _no_interaction():
            query = self._query(
                owned,
                None,
                [
                    (c.const("kSecMatchLimit"), c.const("kSecMatchLimitAll")),
                    (c.const("kSecReturnAttributes"), c.const("kCFBooleanTrue")),
                ],
            )
            out = c_void_p()
            status = c.sec.SecItemCopyMatching(query, byref(out))
            if status == _ITEM_NOT_FOUND:
                return []
            _raise_status(status)
            if not out.value:
                return []
            owned.append(out.value)
            if c.cf.CFGetTypeID(out.value) != c.cf.CFArrayGetTypeID():
                raise CustodyError("keychain error: unexpected result type")
            key = c.const("kSecAttrAccount")
            found: list[str] = []
            for i in range(c.cf.CFArrayGetCount(out.value)):
                item = c.cf.CFArrayGetValueAtIndex(out.value, i)  # Get rule: not released
                if not item or c.cf.CFGetTypeID(item) != c.cf.CFDictionaryGetTypeID():
                    continue
                name = c.read_string(c.cf.CFDictionaryGetValue(item, key))
                if name is not None and name.startswith(prefix):
                    found.append(name)
            return sorted(found)

    def close(self) -> None:
        if self._ref is not None:
            self._cf.cf.CFRelease(self._ref)
            self._ref = None

    def __enter__(self) -> KeychainStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --- test helpers (used only by tests) -------------------------------------------------------------------------


def _refuse_real_keychains(path: str | os.PathLike[str]) -> None:
    """The test helpers never create, unlock, lock or delete a keychain of the user's own: any path under the account's
    real ``~/Library/Keychains`` (from the password database, not ``$HOME``) is refused."""
    import pwd

    real = os.path.realpath(os.path.join(pwd.getpwuid(os.getuid()).pw_dir, "Library", "Keychains"))
    target = os.path.realpath(os.fspath(path))
    if target == real or target.startswith(real + os.sep):
        raise CustodyError("test helpers never touch the user's own keychains")


def create_throwaway_keychain(path: str | os.PathLike[str], password: str) -> None:
    """TEST HELPER: create a new keychain file at ``path``. Does not touch the search list or the default keychain."""
    _refuse_real_keychains(path)
    cf, sec = _load()
    pw = password.encode("utf-8")
    ref = c_void_p()
    status = sec.SecKeychainCreate(os.fsencode(os.fspath(path)), len(pw), pw, 0, None, byref(ref))
    if ref.value:
        cf.CFRelease(ref.value)
    _raise_status(status)


def _with_open(path: str | os.PathLike[str]) -> Iterator[int]:
    cf, _ = _load()
    ref = _open(path)
    try:
        yield ref
    finally:
        cf.CFRelease(ref)


def unlock_keychain(path: str | os.PathLike[str], password: str) -> None:
    """TEST HELPER: unlock the keychain at ``path`` with ``password``."""
    _refuse_real_keychains(path)
    _, sec = _load()
    pw = password.encode("utf-8")
    for ref in _with_open(path):
        _raise_status(sec.SecKeychainUnlock(ref, len(pw), pw, 1))


def lock_keychain(path: str | os.PathLike[str]) -> None:
    """TEST HELPER: lock the keychain at ``path``."""
    _refuse_real_keychains(path)
    _, sec = _load()
    for ref in _with_open(path):
        _raise_status(sec.SecKeychainLock(ref))


def delete_keychain(path: str | os.PathLike[str]) -> None:
    """TEST HELPER: delete the keychain file at ``path``."""
    _refuse_real_keychains(path)
    _, sec = _load()
    for ref in _with_open(path):
        _raise_status(sec.SecKeychainDelete(ref))
