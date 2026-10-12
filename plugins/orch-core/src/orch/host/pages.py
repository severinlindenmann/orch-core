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
``sub_cursor`` and ``as_of``). At most :data:`PER_PRINCIPAL` per principal and :data:`MAX_SNAPSHOTS` per workspace; the
oldest is dropped first, and its tokens answer ``page.expired``. A host restart drops them all (and the key), which is
the same ``page.expired``.

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

__all__ = ["MAX_SNAPSHOTS", "PER_PRINCIPAL", "TTL_SECONDS", "Pages", "Snapshot", "args_digest"]

TTL_SECONDS = 600
MAX_SNAPSHOTS = 256
PER_PRINCIPAL = 32
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


class Pages:
    """The page tokens and snapshots of one workspace. Thread-safe (the worker thread is the only user today)."""

    def __init__(self, workspace_id: str, *, clock: Callable[[], float] = time.time, key: bytes | None = None) -> None:
        self.workspace_id = workspace_id
        self.clock = clock
        self._key = key or secrets.token_bytes(32)
        self._snaps: OrderedDict[str, Snapshot] = OrderedDict()
        self._lock = threading.Lock()

    # -- snapshots
    def open(self, op: str, principal: str, epoch: str, person: str | None, data: dict[str, Any]) -> Snapshot:
        """A new snapshot (the first page). Evicts expired ones, then the oldest beyond the limits."""
        now = self.clock()
        snap = Snapshot(secrets.token_urlsafe(12), op, principal, epoch, person, data, now)
        with self._lock:
            for sid in [s for s, v in self._snaps.items() if now - v.used > TTL_SECONDS]:
                del self._snaps[sid]
            mine = [s for s, v in self._snaps.items() if v.principal == principal]
            while len(mine) >= PER_PRINCIPAL:
                del self._snaps[mine.pop(0)]
            while len(self._snaps) >= MAX_SNAPSHOTS:
                self._snaps.popitem(last=False)
            self._snaps[snap.id] = snap
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
        if not body or not mac or not hmac.compare_digest(self._mac(body), mac):  # 1. authenticity
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
            snap = self._snaps.get(str(fields.get("s")))
            if snap is None or now - snap.used > TTL_SECONDS or snap.principal != principal:
                raise expired
            if fields.get("e") != epoch or snap.epoch != epoch:
                raise expired
            if snap.person is not None and person is not None and snap.person != person:
                raise expired  # the same principal now maps to another person: nothing of the first one carries over
            if fields.get("a") != args_digest(args):  # 4. arguments
                raise HostError("invalid.input", "the page token belongs to a request with other arguments")
            snap.used = now
            self._snaps.move_to_end(snap.id)
        return snap, fields.get("i")
