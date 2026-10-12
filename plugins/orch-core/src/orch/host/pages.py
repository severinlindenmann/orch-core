"""Page tokens and snapshots of the paged reads (§0.8 ``ticket.query``, ``activity.list``; §0.8.5
``workspace.settings``).

**A page token** is opaque and authenticated by the host: ``p1.<body>.<mac>``, the body a base64url JSON object, the
MAC an HMAC-SHA256 (truncated to 128 bits) under a key that lives only in this process. It binds:

* ``o``: the op, ``w``: the workspace, ``p``: the principal hash, ``e``: the history epoch;
* ``s``: the snapshot (a host-side record, below) and ``i``: the paging position inside it;
* ``a``: a digest of the first page's other arguments (everything but ``page`` and ``limit``);
* ``t``: when the token was issued.

The checks run in the order of §0.8: **authenticity** (a token this host did not issue, or altered), then the
**principal** (another principal's token), then **expiry** (older than 10 min, its snapshot gone, or another epoch), all
``page.expired``; then the **arguments**: a valid token of the caller with other arguments is ``invalid.input``.

**A snapshot** is a host-side record, kept in memory for :data:`TTL_SECONDS` after its last use: whatever the op needs
to serve its later pages at the first page's state (rows, a position bound, the visibility of each ticket then, the
``sub_cursor`` and ``as_of``). Only an op with a next page opens one. At most :data:`PER_PRINCIPAL` per principal and
:data:`MAX_SNAPSHOTS` per workspace, and at most :data:`PRINCIPAL_BYTES` per principal and :data:`MAX_BYTES` per
workspace (the size is ``len(json.dumps(data))``). Over a limit the oldest snapshot is dropped first, and its tokens
answer ``page.expired``: a principal over its own share loses its own oldest, and over the workspace limit the
principal holding the most loses its oldest, so one principal cannot push out the snapshots of others beyond its
own share (ties go to the holder with the most recent opening, normally the opener). A single snapshot larger than
one principal's byte share is refused with ``quota.host`` (the op narrows the query).
A host restart drops them all (and the key), which is the same ``page.expired``.

Every page is still **filtered for the viewer of its own request** by the op (§0.8): a snapshot never widens what the
current viewer may see.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import HostError

__all__ = [
    "MAX_BYTES",
    "MAX_SNAPSHOTS",
    "PRINCIPAL_BYTES",
    "PER_PRINCIPAL",
    "TTL_SECONDS",
    "PER_UID",
    "STORE",
    "UID_BYTES",
    "HOST_BYTES",
    "HOST_SNAPSHOTS",
    "Pages",
    "Snapshot",
    "SnapshotStore",
    "args_digest",
]

TTL_SECONDS = 600
MAX_SNAPSHOTS = 256
PER_PRINCIPAL = 32
MAX_BYTES = 64 * 1024 * 1024
PRINCIPAL_BYTES = 8 * 1024 * 1024
#: per kernel UID, across its principals (grants) and workspaces
PER_UID = 64
UID_BYTES = 16 * 1024 * 1024
#: host-wide, across every workspace
HOST_SNAPSHOTS = 1024
HOST_BYTES = 256 * 1024 * 1024
PREFIX = "p1."


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def principal_hash(principal: str) -> str:
    return hashlib.sha256(b"orch/v2/page-principal|" + principal.encode()).hexdigest()[:24]


def args_digest(args: Mapping[str, Any]) -> str:
    """The digest of a request's arguments other than ``page`` and ``limit`` (canonical JSON)."""
    rest = {k: v for k, v in args.items() if k not in ("page", "limit")}
    return hashlib.sha256(json.dumps(rest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


@dataclass
class Snapshot:
    id: str
    op: str
    principal: str
    epoch: str
    #: the first page's viewer (a person id or ``None``): a later page whose viewer is another person is page.expired
    person: str | None
    data: dict[str, Any] = field(default_factory=dict)
    used: float = 0.0
    size: int = 0
    workspace: str = ""


def uid_of(principal: str) -> str:
    """The kernel UID part of a principal (``uid:<n>`` or ``g:<grant>@<n>``): one UID's grants share its caps."""
    return principal.rpartition("@")[2] if principal.startswith("g:") else principal.removeprefix("uid:")


class SnapshotStore:
    """**The one choke point** for page snapshots, shared by every workspace of the host (one per process): every
    paged read (``ticket.query``, ``activity.list``, ``workspace.settings``) stores its snapshot only through
    :meth:`Pages.open`, which admits it here. Caps hold per principal, per kernel UID (all its grants), per workspace
    and host-wide, in count and in bytes. A principal cannot widen its share by varying arguments, sessions or
    connections (none of them is part of the key) or by issuing grants (the UID cap). When a cap is exceeded, the
    heaviest holder **within that scope** (the opener included) loses its oldest snapshot; a principal within its
    share is never evicted while another holds more. A snapshot larger than one principal's byte share is refused
    (``quota.host``)."""

    def __init__(self) -> None:
        self.snaps: OrderedDict[str, Snapshot] = OrderedDict()
        self.lock = threading.RLock()

    def scopes(self, snap: Snapshot) -> list[tuple[Callable[[Snapshot], bool], int, int, Callable[[Snapshot], str]]]:
        """(member test, count cap, byte cap, holder) per scope; holder groups the scope's snapshots for eviction."""
        uid = uid_of(snap.principal)
        return [
            (lambda v: v.principal == snap.principal, PER_PRINCIPAL, PRINCIPAL_BYTES, lambda v: v.principal),
            (lambda v: uid_of(v.principal) == uid, PER_UID, UID_BYTES, lambda v: v.principal),
            (lambda v: v.workspace == snap.workspace, MAX_SNAPSHOTS, MAX_BYTES, lambda v: uid_of(v.principal)),
            (lambda v: True, HOST_SNAPSHOTS, HOST_BYTES, lambda v: uid_of(v.principal)),
        ]

    def admit(self, snap: Snapshot, now: float) -> None:
        if snap.size > PRINCIPAL_BYTES:
            raise HostError("quota.host", "the result is too large to page: narrow the query")
        with self.lock:
            for sid in [k for k, v in self.snaps.items() if now - v.used > TTL_SECONDS]:
                del self.snaps[sid]
            self.snaps[snap.id] = snap
            for member, max_count, max_bytes, holder in self.scopes(snap):
                while True:
                    inside = [v for v in self.snaps.values() if member(v)]
                    if len(inside) <= max_count and sum(v.size for v in inside) <= max_bytes:
                        break
                    use: dict[str, float] = {}
                    last: dict[str, int] = {}
                    for n, v in enumerate(inside):  # each holder's share of this scope's caps, newest opening
                        use[holder(v)] = use.get(holder(v), 0.0) + v.size / max_bytes + 1 / max_count
                        last[holder(v)] = n
                    # ties: the holder with the most recent opening (not the oldest), so equal holders never push
                    # out an earlier one
                    # (the snapshot being admitted is never its own victim while another can go)
                    old = [v for v in inside if v.id != snap.id] or inside
                    top = max({holder(v) for v in old}, key=lambda h: (round(use[h], 9), last[h]))
                    victim = next(v for v in old if holder(v) == top)  # its oldest (insertion order)
                    del self.snaps[victim.id]
            if snap.id not in self.snaps:
                raise HostError("quota.host", "the host holds too many page snapshots now: retry shortly")

    def get(self, sid: str) -> Snapshot | None:
        return self.snaps.get(sid)

    def touch(self, snap: Snapshot, now: float) -> None:
        snap.used = now
        if snap.id in self.snaps:
            self.snaps.move_to_end(snap.id)


#: the host-wide store (one per process)
STORE = SnapshotStore()


class Pages:
    """The page tokens of one workspace, over the host-wide :data:`STORE`. Thread-safe."""

    def __init__(
        self,
        workspace_id: str,
        *,
        clock: Callable[[], float] = time.time,
        key: bytes | None = None,
        store: SnapshotStore | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.clock = clock
        self._key = key or secrets.token_bytes(32)
        self.store = store or STORE
        self._lock = self.store.lock

    @property
    def _snaps(self) -> dict[str, Snapshot]:
        """This workspace's snapshots (a view, for tests and diagnostics)."""
        return {k: v for k, v in self.store.snaps.items() if v.workspace == self.workspace_id}

    # -- snapshots
    def open(self, op: str, principal: str, epoch: str, person: str | None, data: dict[str, Any]) -> Snapshot:
        """A new snapshot (a first page with a next page), admitted through the store's caps."""
        now = self.clock()
        size = len(json.dumps(data, default=str, separators=(",", ":")))
        snap = Snapshot(secrets.token_urlsafe(12), op, principal, epoch, person, data, now, size, self.workspace_id)
        self.store.admit(snap, now)
        return snap

    # -- tokens
    def _mac(self, body: str) -> str:
        return _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest()[:16])

    def token(self, snap: Snapshot, position: Any, args: Mapping[str, Any]) -> str:
        """The ``next_page`` token for ``position`` in ``snap`` (any JSON value the op chooses)."""
        body = _b64(
            json.dumps(
                {
                    "o": snap.op,
                    "w": self.workspace_id,
                    "p": principal_hash(snap.principal),
                    "e": snap.epoch,
                    "s": snap.id,
                    "i": position,
                    "a": args_digest(args),
                    "t": int(self.clock()),
                },
                separators=(",", ":"),
            ).encode()
        )
        out = f"{PREFIX}{body}.{self._mac(body)}"
        if len(out) > 512:  # the contract's page:str{1..512}
            raise HostError("internal", "a page token would be longer than 512 characters")
        return out

    def resume(
        self, op: str, token: Any, principal: str, epoch: str, person: str | None, args: Mapping[str, Any]
    ) -> tuple[Snapshot, Any]:
        """``(snapshot, position)`` of a later page, or ``page.expired`` / ``invalid.input`` in the §0.8 order."""
        expired = HostError("page.expired", "the page token is not valid any more: restart from the first page")
        if not isinstance(token, str) or not token.startswith(PREFIX):
            raise expired
        body, _, mac = token[len(PREFIX) :].partition(".")
        if (
            not body
            or not mac
            # compared as bytes: a non-ASCII token is a mismatch, never a TypeError
            or not hmac.compare_digest(mac.encode("utf-8", "surrogatepass"), self._mac(body).encode("ascii"))
        ):  # 1. authenticity
            raise expired
        try:
            fields = json.loads(_unb64(body))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            raise expired from None
        if not isinstance(fields, dict) or fields.get("o") != op or fields.get("w") != self.workspace_id:
            raise expired
        if fields.get("p") != principal_hash(principal):  # 2. principal
            raise expired
        now = self.clock()
        t = fields.get("t")
        if not isinstance(t, int) or now - t > TTL_SECONDS or t > now + 5:  # 3. expiry
            raise expired
        with self._lock:
            snap = self.store.get(str(fields.get("s")))
            if (
                snap is None
                or snap.workspace != self.workspace_id
                or now - snap.used > TTL_SECONDS
                or snap.principal != principal
            ):
                raise expired
            if fields.get("e") != epoch or snap.epoch != epoch:
                raise expired
            if snap.person != person:
                # the same principal now maps to another person, or to none, or from none to one: nothing of the
                # first viewer carries over (stricter than "filtered as the new viewer", §0.8: restart from page 1)
                raise expired
            if fields.get("a") != args_digest(args):  # 4. arguments
                raise HostError("invalid.input", "the page token belongs to a request with other arguments")
            self.store.touch(snap, now)
        return snap, fields.get("i")
