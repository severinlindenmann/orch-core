"""Files and blobs (§0.10, H4a2): ``blob.put``, ``blob.drop``, the blobs a request names in ``files``, and quotas.

**Uploads.** The first chunk has no ``blob`` and carries ``size`` and a client-chosen ``upload_key``: the host reserves
``size`` against the quotas and returns a new id (``b_`` plus 128 random bits, base32). Later chunks name the blob.
Offsets are contiguous from 0, each chunk decodes to at most 768 KiB, and a chunk at an offset already written is
accepted without effect when its bytes are identical (different bytes: ``blob.invalid``). ``last:true`` needs the total
to equal ``size`` and ``sha256`` to match; the digest is checked **before** the last bytes are written, so a wrong
digest leaves the blob as it was. After that the blob is immutable, and resending its last chunk with the same bytes
and digest returns the same answer. A lost first-chunk answer is recovered by resending the first chunk with the same
``upload_key`` (same principal, within the blob's lifetime, identical bytes at offset 0).

**Ownership (F9).** A blob belongs to one workspace (each workspace has its own :class:`BlobStore`) and one principal.
Every lookup is by ``(principal, id)``: another principal's blob, or one of another workspace, answers exactly as an
unknown id (``blob.invalid``).

**Lifetime.** A blob is consumed when the write that uses it commits (:meth:`BlobStore.consume`); after a refusal or a
dry run it stays. An unused or incomplete blob expires 10 minutes after its last chunk, unless a prepared human request
holds it (:meth:`BlobStore.hold`): it then lives as long as the request, and goes when the request is submitted or
cancelled (:meth:`BlobStore.release`). Blobs are kept in memory and on disk for the host process only: they survive a
reconnect, not a host restart (leftover files are removed at open; a prepared request keeps its own copy of the bytes).

**Quotas** (:class:`Quota`, one per host, shared by every workspace's store) count decoded bytes, reserved at the
first chunk: 64 MiB per blob, 256 MiB per **UID** across all its principals and workspaces (held blobs included), and
1 GiB per host, of which 256 MiB only person-class UIDs may use (the person reserve). Over a limit: ``quota.host``.

**Storage.** ``<state>/hosts/<ws>/blobs/<id>`` (0600) in a host-owned 0700 directory, opened once; every file
operation is relative to that directory's fd with ``O_NOFOLLOW``, and an id is checked against ``^b_[A-Z2-7]{26}$``
before it is used as a name. The directory is never a handler's file root, so a handler sees a blob's bytes only
through ``ctx.files`` of the op that consumes it.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import logging
import os
import re
import secrets
import stat
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .custody import ensure_private_dir
from .errors import HostError

__all__ = [
    "BLOB_ID",
    "BLOB_MAX",
    "CHUNK_MAX",
    "EXPIRE_SECONDS",
    "HOST_MAX",
    "INLINE_MAX",
    "PERSON_RESERVE",
    "UID_MAX",
    "BlobStore",
    "Quota",
    "new_blob_id",
]

MIB = 1024 * 1024
BLOB_MAX = 64 * MIB
UID_MAX = 256 * MIB
HOST_MAX = 1024 * MIB
PERSON_RESERVE = 256 * MIB
CHUNK_MAX = 768 * 1024
INLINE_MAX = 512 * 1024
EXPIRE_SECONDS = 600
BLOB_ID = re.compile(r"b_[A-Z2-7]{26}")
_HASH = r"^sha256:[0-9a-f]{64}$"
_B64 = r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"
LOG = logging.getLogger(__name__)

SCHEMAS: dict[str, dict[str, Any]] = {
    "blob.put": {
        "type": "object",
        "properties": {
            "blob": {"type": "string", "pattern": r"^b_[A-Z2-7]{26}$"},
            "upload_key": {"type": "string", "pattern": r"^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$"},
            "offset": {"type": "integer", "minimum": 0},
            "b64": {"type": "string", "pattern": _B64},
            "last": {"type": "boolean"},
            "size": {"type": "integer", "minimum": 0, "maximum": BLOB_MAX},
            "sha256": {"type": "string", "pattern": _HASH},
        },
        "required": ["offset", "b64", "last"],
        "additionalProperties": False,
    },
    "blob.drop": {
        "type": "object",
        "properties": {"blob": {"type": "string", "pattern": r"^b_[A-Z2-7]{26}$"}},
        "required": ["blob"],
        "additionalProperties": False,
    },
}
VALIDATORS = {op: Draft202012Validator(schema) for op, schema in SCHEMAS.items()}


def new_blob_id() -> str:
    """``b_`` plus 128 random bits in base32 (26 characters, no padding)."""
    return "b_" + base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=")


def decoded_len(b64: str) -> int:
    """The decoded length of canonical base64 text, without decoding it."""
    return len(b64) // 4 * 3 - (2 if b64.endswith("==") else 1 if b64.endswith("=") else 0)


# --- the host-wide quota ----------------------------------------------------------------------------------------------


@dataclass(eq=False)
class Blob:
    """One blob's bookkeeping. Its owning :class:`BlobStore` changes it on its workspace worker thread; the
    :class:`Quota` reads ``uid``, ``person``, ``size`` and :meth:`alive` from any thread, under its own lock."""

    id: str
    workspace: str
    principal: str
    uid: int
    person: bool
    size: int
    upload_key: str | None
    last_at: float
    received: int = 0
    digest: str | None = None
    hasher: Any = field(default_factory=hashlib.sha256)
    #: the request that holds it (``human.prepare``), and until when
    held_by: str | None = None
    held_until: float = 0.0
    gone: bool = False

    @property
    def complete(self) -> bool:
        return self.digest is not None

    def alive(self, now: float) -> bool:
        if self.gone:
            return False
        if self.held_by is not None:
            return now < self.held_until
        return now < self.last_at + EXPIRE_SECONDS


class Quota:
    """The host's blob quotas (§0.10, F9): per UID across every principal and workspace, and per host with a reserve
    for person-class UIDs. Thread-safe: every workspace's worker thread reserves through it."""

    def __init__(
        self,
        *,
        per_uid: int = UID_MAX,
        host: int = HOST_MAX,
        person_reserve: int = PERSON_RESERVE,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time

        self.per_uid, self.host, self.person_reserve = per_uid, host, person_reserve
        self.clock = clock or time.time
        self._lock = threading.Lock()
        self._blobs: set[Blob] = set()

    def usage(self) -> tuple[int, int, dict[int, int]]:
        """(host total, non-person total, per UID) of the live reservations."""
        with self._lock:
            return self._usage(self.clock())

    def _usage(self, now: float) -> tuple[int, int, dict[int, int]]:
        total, others, per_uid = 0, 0, {}
        for blob in list(self._blobs):
            if not blob.alive(now):
                self._blobs.discard(blob)  # expired: its store removes the file when it next looks
                continue
            total += blob.size
            if not blob.person:
                others += blob.size
            per_uid[blob.uid] = per_uid.get(blob.uid, 0) + blob.size
        return total, others, per_uid

    def reserve(self, blob: Blob) -> None:
        """Count ``blob.size`` against its UID and the host, or ``quota.host``."""
        with self._lock:
            total, others, per_uid = self._usage(self.clock())
            if per_uid.get(blob.uid, 0) + blob.size > self.per_uid:
                raise HostError("quota.host", "the blob quota of this user is used up; drop or use blobs first")
            if total + blob.size > self.host:
                raise HostError("quota.host", "the host's blob quota is used up; retry later")
            if not blob.person and others + blob.size > self.host - self.person_reserve:
                raise HostError("quota.host", "the host's blob quota is used up; retry later")
            self._blobs.add(blob)

    def release(self, blob: Blob) -> None:
        with self._lock:
            self._blobs.discard(blob)


# --- one workspace's blobs --------------------------------------------------------------------------------------------


class BlobStore:
    """The blobs of one workspace: ``<state>/hosts/<ws>/blobs/``. Call it on the workspace worker thread."""

    def __init__(
        self,
        state_dir: Path,
        workspace_id: str,
        host_uid: int,
        *,
        clock: Callable[[], float],
        quota: Quota | None = None,
    ) -> None:
        self.dir = Path(state_dir) / "hosts" / workspace_id / "blobs"
        self.workspace_id = workspace_id
        self.host_uid = host_uid
        self.clock = clock
        self.quota = quota or Quota(clock=clock)
        self._blobs: dict[str, Blob] = {}
        self._keys: dict[tuple[str, str], str] = {}
        self._dfd: int | None = None

    # -- the directory
    def open(self) -> None:
        """Create the 0700 directory, open it once, and remove what an earlier host process left (blobs live for one
        host process)."""
        ensure_private_dir(self.dir.parent.parent, self.host_uid)
        ensure_private_dir(self.dir.parent, self.host_uid)
        ensure_private_dir(self.dir, self.host_uid)
        self.close()
        self._dfd = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        for name in os.listdir(self._dfd):
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=self._dfd)

    def close(self) -> None:
        for blob in list(self._blobs.values()):
            self._forget(blob, unlink=False)
        if self._dfd is not None:
            os.close(self._dfd)
            self._dfd = None

    def _fd(self) -> int:
        if self._dfd is None:
            raise HostError("workspace.unavailable", "the blob store is not open")
        return self._dfd

    @staticmethod
    def _name(blob_id: str) -> str:
        if not isinstance(blob_id, str) or not BLOB_ID.fullmatch(blob_id):
            raise HostError("blob.invalid", "not a blob id")
        return blob_id

    def _open(self, blob_id: str, flags: int) -> int:
        fd = os.open(self._name(blob_id), flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self._fd())
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != self.host_uid:
                raise HostError("blob.invalid", "the blob's file is not the host's own")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _read(self, blob: Blob, offset: int, length: int) -> bytes:
        fd = self._open(blob.id, os.O_RDONLY)
        try:
            out = bytearray()
            while len(out) < length:
                chunk = os.pread(fd, length - len(out), offset + len(out))
                if not chunk:
                    break
                out += chunk
            return bytes(out)
        finally:
            os.close(fd)

    def _append(self, blob: Blob, data: bytes) -> None:
        fd = self._open(blob.id, os.O_WRONLY)
        try:
            view = memoryview(data)
            pos = blob.received
            while view:
                n = os.pwrite(fd, view, pos)
                view, pos = view[n:], pos + n
        finally:
            os.close(fd)

    # -- lookups
    def _forget(self, blob: Blob, *, unlink: bool = True) -> None:
        blob.gone = True
        self.quota.release(blob)
        self._blobs.pop(blob.id, None)
        if blob.upload_key is not None and self._keys.get((blob.principal, blob.upload_key)) == blob.id:
            del self._keys[(blob.principal, blob.upload_key)]
        if unlink and self._dfd is not None:
            with contextlib.suppress(OSError):
                os.unlink(blob.id, dir_fd=self._dfd)

    def sweep(self) -> int:
        """Remove every expired blob (also called by each lookup); returns how many went."""
        now = self.clock()
        gone = [b for b in self._blobs.values() if not b.alive(now)]
        for blob in gone:
            self._forget(blob)
        return len(gone)

    def _get(self, principal: str, blob_id: Any) -> Blob | None:
        """The live blob ``blob_id`` of ``principal`` in this workspace, else ``None`` (unknown, another principal's,
        expired: all the same)."""
        self.sweep()
        if not isinstance(blob_id, str) or not BLOB_ID.fullmatch(blob_id):
            return None
        blob = self._blobs.get(blob_id)
        if blob is None or blob.principal != principal:
            return None
        return blob

    def digest(self, principal: str, blob_id: Any) -> str | None:
        """The digest of a **complete** blob of ``principal`` (``files: {blob}``), else ``None`` (``blob.invalid``)."""
        blob = self._get(principal, blob_id)
        return blob.digest if blob is not None and blob.complete else None

    def read(self, principal: str, blob_id: str) -> bytes:
        """The bytes of a complete blob of ``principal``, checked against its digest once more."""
        blob = self._get(principal, blob_id)
        if blob is None or not blob.complete:
            raise HostError("blob.invalid", "no complete blob of yours with this id")
        data = self._read(blob, 0, blob.size)
        if len(data) != blob.size or "sha256:" + hashlib.sha256(data).hexdigest() != blob.digest:
            self._forget(blob)
            raise HostError("blob.invalid", "the blob's bytes changed; upload it again")
        return data

    def consume(self, principal: str, ids: Iterable[str]) -> None:
        """The write that used these blobs committed: they go."""
        for blob_id in ids:
            blob = self._get(principal, blob_id)
            if blob is not None:
                self._forget(blob)

    def hold(self, principal: str, ids: Iterable[str], request_id: str, until: float) -> None:
        """A prepared human request holds these blobs: they live as long as it does (``until``, its expiry)."""
        for blob_id in ids:
            blob = self._get(principal, blob_id)
            if blob is not None:
                blob.held_by, blob.held_until = request_id, until

    def release(self, request_id: str) -> None:
        """The request was submitted or cancelled: the blobs it held go."""
        for blob in [b for b in self._blobs.values() if b.held_by == request_id]:
            self._forget(blob)

    def drop(self, principal: str, blob_id: str) -> None:
        """``blob.drop``: an unknown or foreign id is not an error (and says nothing). A blob a prepared request holds
        is the request's until it ends: a drop leaves it."""
        blob = self._get(principal, blob_id)
        if blob is not None and blob.held_by is None:
            self._forget(blob)

    # -- blob.put
    def put(self, principal: str, uid: int, person: bool, args: Mapping[str, Any]) -> dict[str, Any]:
        """One chunk (§0.10). ``args`` passed the schema. Returns ``{blob, size, received}``."""
        b64 = args["b64"]
        if decoded_len(b64) > CHUNK_MAX:
            raise HostError("blob.invalid", f"a chunk decodes to at most {CHUNK_MAX} bytes")
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise HostError("blob.invalid", "b64 is not valid base64") from None
        offset, last, sha = int(args["offset"]), bool(args["last"]), args.get("sha256")
        if "blob" not in args:
            size, key = args.get("size"), args.get("upload_key")
            if size is None or key is None:
                raise HostError("blob.invalid", "the first chunk carries size and upload_key")
            if offset != 0:
                raise HostError("blob.invalid", "the first chunk starts at offset 0")
            known = self._keys.get((principal, key))
            blob = self._get(principal, known) if known is not None else None
            if blob is not None:  # allocation recovery: the same blob, if the bytes at 0 are the same
                if blob.size != size:
                    raise HostError("blob.invalid", "this upload_key names an upload of another size")
                return self._chunk(blob, offset, data, last, sha)
            blob = Blob(new_blob_id(), self.workspace_id, principal, uid, person, int(size), key, self.clock())
            self.quota.reserve(blob)
            try:
                os.close(self._create(blob.id))
                self._blobs[blob.id] = blob
                self._keys[(principal, key)] = blob.id
                return self._chunk(blob, offset, data, last, sha)
            except BaseException:
                self._forget(blob)  # a refused first chunk leaves nothing behind
                raise
        blob = self._get(principal, args["blob"])
        if blob is None:
            raise HostError("blob.invalid", "no blob of yours with this id")
        if "size" in args and args["size"] != blob.size:
            raise HostError("blob.invalid", "size differs from the size given at allocation")
        if "upload_key" in args and args["upload_key"] != blob.upload_key:
            raise HostError("blob.invalid", "upload_key differs from the one given at allocation")
        return self._chunk(blob, offset, data, last, sha)

    def _create(self, blob_id: str) -> int:
        return os.open(
            self._name(blob_id),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self._fd(),
        )

    def _chunk(self, blob: Blob, offset: int, data: bytes, last: bool, sha: str | None) -> dict[str, Any]:
        end = offset + len(data)
        if offset > blob.received:
            raise HostError("blob.invalid", f"offsets are contiguous: continue at {blob.received}")
        if end > blob.size:
            raise HostError("blob.invalid", "the chunk goes past the blob's size")
        overlap = min(end, blob.received) - offset
        if overlap > 0 and self._read(blob, offset, overlap) != data[:overlap]:
            raise HostError("blob.invalid", "these bytes differ from the ones already received at this offset")
        new = data[overlap:]
        if blob.complete:  # immutable: only an identical resend, and a final one must name the same digest
            if last and (end != blob.size or sha != blob.digest):
                raise HostError("blob.invalid", "the blob is complete with another final chunk or digest")
            blob.last_at = self.clock()
            return self._answer(blob)
        if last:
            if end != blob.size:
                raise HostError("blob.invalid", f"the last chunk must end at size ({blob.size})")
            if sha is None:
                raise HostError("blob.invalid", "the last chunk carries sha256")
            check = blob.hasher.copy()
            check.update(new)
            if "sha256:" + check.hexdigest() != sha:
                raise HostError("blob.invalid", "sha256 does not match the bytes received")
        if new:
            self._append(blob, new)
            blob.hasher.update(new)
            blob.received = end
        if last:
            blob.digest = sha
        blob.last_at = self.clock()
        return self._answer(blob)

    @staticmethod
    def _answer(blob: Blob) -> dict[str, Any]:
        return {"blob": blob.id, "size": blob.size, "received": blob.received}
