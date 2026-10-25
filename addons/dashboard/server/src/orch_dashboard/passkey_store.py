"""The passkey store ``<state>/dashboard/passkeys.json`` (host spec 0.8.6 10.3; #1198 R1-5).

Mode 0600, owned by the person, no secret in it. But writing it adds a login, so it is read like the pins file: opened
with ``O_NOFOLLOW``, a regular file owned by the person with no group or other bits, in a directory that nobody else can
write to, bounded in size, and parsed strictly. Anything else fails closed (``StoreError``): no login, no enrollment.
A missing file is an empty store.
"""

import contextlib
import json
import os
import stat
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from .passkeys import ALG_EDDSA, ALG_ES256, PasskeyError, b64u_decode, b64u_encode, parse_public_key

MAX_FILE = 262_144
MAX_PASSKEYS = 32
MAX_NAME = 64


class StoreError(Exception):
    """The store cannot be trusted (wrong owner, mode, shape) or written. The reason is for the log only."""


@dataclass(frozen=True)
class Passkey:
    credential_id: str  # base64url
    user_handle: str  # base64url
    public_key: str  # base64url of the COSE key as sent by the authenticator
    alg: int
    backup_eligible: bool
    name: str
    created: str  # ISO 8601 UTC
    last_used: str | None
    sign_count: int  # recorded and shown only, never enforced (synced passkeys report 0)

    @property
    def id_bytes(self) -> bytes:
        return b64u_decode(self.credential_id, 1023)

    @property
    def handle_bytes(self) -> bytes:
        return b64u_decode(self.user_handle, 64)

    @property
    def key_bytes(self) -> bytes:
        return b64u_decode(self.public_key, 1024)


def _check_record(row) -> Passkey:
    keys = set(Passkey.__dataclass_fields__)
    if not isinstance(row, dict) or set(row) != keys:
        raise StoreError("record shape")
    if not (isinstance(row["alg"], int) and not isinstance(row["alg"], bool) and row["alg"] in (ALG_ES256, ALG_EDDSA)):
        raise StoreError("alg")
    if not isinstance(row["backup_eligible"], bool):
        raise StoreError("backup_eligible")
    count = row["sign_count"]
    if not (isinstance(count, int) and not isinstance(count, bool) and 0 <= count < 2**32):
        raise StoreError("sign_count")
    for field in ("credential_id", "user_handle", "public_key", "name", "created"):
        if not isinstance(row[field], str):
            raise StoreError(field)
    if row["last_used"] is not None and not isinstance(row["last_used"], str):
        raise StoreError("last_used")
    if not 1 <= len(row["name"]) <= MAX_NAME or len(row["created"]) > 40 or len(row["last_used"] or "") > 40:
        raise StoreError("length")
    record = Passkey(**row)
    try:
        record.id_bytes, record.handle_bytes
        alg, _ = parse_public_key(record.key_bytes)
    except PasskeyError as exc:
        raise StoreError(f"record value: {exc.reason}") from None
    if alg != record.alg:
        raise StoreError("alg mismatch")
    return record


class PasskeyStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def _check_directory(self) -> None:
        try:
            st = os.lstat(self.path.parent)
        except OSError as exc:
            raise StoreError(f"directory: {exc.__class__.__name__}") from None
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise StoreError("directory must be your own, not writable by others")

    def read(self) -> list[Passkey]:
        """All passkeys, or ``[]`` when there is no file. Fails closed on anything unexpected."""
        self._check_directory()
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise StoreError(f"open: {exc.__class__.__name__}") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_size > MAX_FILE:
                raise StoreError("file must be a small regular file of yours, mode 0600")
            with os.fdopen(fd, "rb", closefd=False) as f:
                raw = f.read(MAX_FILE + 1)
        finally:
            os.close(fd)
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
        except (UnicodeDecodeError, ValueError):
            raise StoreError("not json") from None
        if not isinstance(data, dict) or set(data) != {"v", "passkeys"} or data["v"] != 1:
            raise StoreError("shape")
        rows = data["passkeys"]
        if not isinstance(rows, list) or len(rows) > MAX_PASSKEYS:
            raise StoreError("passkeys")
        records = [_check_record(r) for r in rows]
        if len({r.credential_id for r in records}) != len(records):
            raise StoreError("duplicate credential id")
        return records

    def _write(self, records: list[Passkey]) -> None:
        self._check_directory()
        body = json.dumps({"v": 1, "passkeys": [asdict(r) for r in records]}, separators=(",", ":")).encode()
        fd, tmp = tempfile.mkstemp(prefix=".passkeys-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def find(self, credential_id: bytes) -> Passkey | None:
        wanted = b64u_encode(credential_id)
        return next((r for r in self.read() if r.credential_id == wanted), None)

    def add(self, record: Passkey) -> None:
        with self.lock:
            records = self.read()
            if len(records) >= MAX_PASSKEYS:
                raise StoreError("too many passkeys")
            if any(r.credential_id == record.credential_id for r in records):
                raise StoreError("credential id exists")
            _check_record(asdict(record))
            self._write([*records, record])

    def touch(self, credential_id: bytes, *, when: str, sign_count: int) -> None:
        wanted = b64u_encode(credential_id)
        with self.lock:
            records = self.read()
            out = [
                Passkey(**{**asdict(r), "last_used": when, "sign_count": sign_count})
                if r.credential_id == wanted
                else r
                for r in records
            ]
            self._write(out)

    def remove(self, credential_id: bytes) -> bool:
        wanted = b64u_encode(credential_id)
        with self.lock:
            records = self.read()
            kept = [r for r in records if r.credential_id != wanted]
            if len(kept) == len(records):
                return False
            self._write(kept)
            return True


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)
