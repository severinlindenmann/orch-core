"""R2's view of R6 (contract 11A.1, 11A.2): ``BridgeKeys`` (the only user of the epoch keys ``WK_e`` in the bridge) and
the read-only ``R6View`` of the accepted member list.

R6 is a security dependency of R2, not a fixture (11A.1): R2 is tested against a fixture ``R6View``, and ``RelayWorker``
(R5) does not start the poll thread until R6 is merged. Nothing here writes R6 state.

* :class:`BridgeKeys` opens a request body and seals a response chunk; it hands out plaintext or envelopes, never a key
  (11A.2 "Epoch keys are used only through BridgeKeys"). It is also the :class:`orch.wsproto.sign.EpochKeys` the signer
  asks before it signs a chunk (burned keys count as held there, proto §8.5 G2b).
* **G1, the seal gate** (orch-52 / R6 design #473): every chunk is sealed inside :meth:`BridgeKeys.sealing`, which
  holds the seal lock and takes ONE snapshot of the seal state (:class:`SealState`: the committed epoch, the rotation
  gate, the epoch a ``stale_epoch`` names). The caller seals AND commits the replayable bytes (T2 or the refusal
  record) inside that span; R6 raises the gate or moves the committed epoch in memory under the same lock
  (:meth:`BridgeKeys.serialised`), so a gate is never raised between the check and the commit. **R6a:** the seal
  lock is the source's own (``EpochKeySource.seal_snapshot``/``serialised``, R6's ``MembershipKeys``), not this
  module's, so R6's store hook (``R6Gate.raise_for``) and every seal take ONE lock (11A.15.2).
  :meth:`_Sealer.seal_response` (data) refuses any epoch but the snapshot's committed one, and any epoch while the
  gate is up. :meth:`_Sealer.seal_refusal` is the only path under an old, burned or gated epoch: it builds the
  plaintext ITSELF with :func:`orch.relay.framing.refusal_meta` (the closed field sets), so no caller can seal
  arbitrary plaintext there; under a burned epoch (above the committed one, :meth:`EpochKeySource.refusal_key`, or
  below it, :meth:`EpochKeySource.is_burned`, R6a round 2) only ``stale_epoch``. The sealer class is private
  and its constructor takes a module-private token: only :meth:`BridgeKeys.sealing` builds one, with the snapshot
  it took itself (a test pins that by AST; review D2-7).
* **Lock order** (11A.5, review D2-4): the workspace store lock first, then the seal lock, then the RidStore lock
  (innermost; it never calls out). Inside a :meth:`BridgeKeys.sealing` span the bridge takes only the RidStore lock
  and SQLite; it never takes the store lock and never calls ``job()``. No :class:`EpochKeySource` method
  (``current_epoch``, ``wk``, ``seal_state``, ``refusal_key``) may take the store lock or wait for the workspace
  worker: R6 commits a rotation under the store lock inside :meth:`BridgeKeys.serialised` (store lock, then seal
  lock), so a source that took the store lock under the seal lock would deadlock with it.
* :class:`R6View` returns the current accepted member list: per device its person, its scopes and its certificate, and
  the owner-verified person key (``pk_pub``) the certificate is checked under.
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from orch.crypto import push as cpush
from orch.identity.epochs import SealState
from orch.wsproto import wire
from orch.wsproto.sign import RelaySigner

from . import framing

__all__ = [
    "BridgeKeys", "EpochKeySource", "KeyMissing", "Member", "MemberList", "R6View", "SealRefused", "SealState",
]  # fmt: skip

_TOKEN = object()  # only BridgeKeys.sealing holds it: nothing else can build a sealer (G1, review D2-7)


class KeyMissing(Exception):
    """No key for this epoch (above current, deleted, never held): the request is dropped (proto §8.6 step 2)."""


class SealRefused(Exception):  # noqa: N818 - the gate's verdict, not an I/O error
    """G1: data may not be sealed under this epoch now (old, burned or gated): the caller answers with a refusal."""


class EpochKeySource(Protocol):
    """R6's custody of ``WK_e``: the committed epoch, the key of a held epoch at or below it, the seal state and the
    key of a burned epoch above it (refusals only, proto §7.2 H4 / §8.5 G2b)."""

    def current_epoch(self) -> int: ...

    def wk(self, epoch: int) -> bytes | None: ...

    def seal_state(self) -> SealState: ...

    def refusal_key(self, epoch: int) -> bytes | None:
        """A BURNED epoch's key (above the committed one), usable only to open a request and seal its signed
        ``stale_epoch``; ``None`` for every other epoch."""
        ...

    def seal_snapshot(self) -> contextlib.AbstractContextManager[SealState]:
        """R6's seal lock, held for one seal, and ONE :class:`SealState` taken under it (11A.15.2 ``seal_snapshot``;
        R6a). The lock is R6's, not this module's: R6's gate (``R6Gate.raise_for``, called by the store inside its
        own lock) takes the same lock, so a snapshot is never older than the gate."""
        ...

    def serialised(self) -> contextlib.AbstractContextManager[None]:
        """R6's seal lock alone (R6 updates its in-memory seal state under it; it never calls a writer method inside
        it, R6a round 2)."""
        ...

    def is_burned(self, epoch: int) -> bool:
        """``epoch`` is burned (an abandoned exposure or an orphan key), below the committed epoch too: proto §7.2 H4,
        nothing but ``stale_epoch`` is ever sealed under it (R6a round 2)."""
        ...


@dataclass(frozen=True)
class Member:
    """One entry of the accepted member list (R6): ids are 32 lower-case hex (no ``d_``/``p_`` prefix)."""

    device_id: str
    person_id: str
    scopes: tuple[str, ...]
    cert: Mapping[str, object]
    pk_pub: bytes


@dataclass(frozen=True)
class MemberList:
    epoch: int
    list_hash: str  # "sha256:<64 hex>" of the accepted signed list
    members: Mapping[str, Member]


class R6View(Protocol):
    def member_list(self) -> MemberList | None: ...


class BridgeKeys:
    """Open requests and seal responses under ``K_bridge(e)`` (proto §8.2). ``workspace`` is the 16-byte id."""

    def __init__(self, source: EpochKeySource, workspace: bytes) -> None:
        if type(workspace) is not bytes or len(workspace) != 16:
            raise ValueError("workspace id")
        self._source, self._ws = source, workspace

    @property
    def current(self) -> int:
        return self._source.current_epoch()

    def serves(self, epoch: int) -> bool:
        """A committed or older epoch whose key is held: requests under it are opened and answered (§8.6 step 2)."""
        return type(epoch) is int and 1 <= epoch <= self.current and self._source.wk(epoch) is not None

    def burned(self, epoch: int) -> bool:
        """A burned epoch: above the committed one, opened only to answer its signed ``stale_epoch`` (C5, G2b); at
        or below it (R6a round 2), served as an old epoch but, as above, only ``stale_epoch`` is sealed under it
        (proto §7.2 H4)."""
        if type(epoch) is not int or epoch < 1:
            return False
        if epoch > self.current:
            return self._source.refusal_key(epoch) is not None
        return self._source.is_burned(epoch)

    def holds(self, epoch: int) -> bool:
        """For the signer (:class:`orch.wsproto.sign.EpochKeys`): served or burned (G2b: burned counts as held)."""
        return self.serves(epoch) or self.burned(epoch)

    def _wk(self, epoch: int) -> bytes:
        if self.serves(epoch):
            wk = self._source.wk(epoch)
        elif self.burned(epoch):
            wk = self._source.refusal_key(epoch)
        else:
            raise KeyMissing("epoch key not held")
        assert wk is not None
        return wire.k_bridge(wk, self._ws, epoch)

    def open_request(self, header: bytes, body: bytes) -> bytes:
        """The plaintext of a request body; :class:`KeyMissing` (drop) or :class:`wire.WireError` ``tag`` (drop)."""
        h = wire.parse_header(header)
        return wire.open_body(self._wk(h.epoch), header, body)

    def state(self) -> SealState:
        """A snapshot of the seal state for a decision that seals nothing itself (step 9); every seal takes its own
        snapshot inside :meth:`sealing`."""
        return self._source.seal_state()

    @contextlib.contextmanager
    def serialised(self) -> Iterator[None]:
        """The seal lock (the source's): no seal snapshot is taken while it is held. R6 updates its in-memory seal
        state (gate, committed epoch) under it, after its own durable write; it never calls a writer method inside
        it (R6a round 2: one lock order, store, R6 writer, seal, RidStore)."""
        with self._source.serialised():
            yield

    @contextlib.contextmanager
    def sealing(self, signer: RelaySigner) -> Iterator[_Sealer]:
        """G1: the seal lock and ONE snapshot of the seal state; seal AND commit the replayable bytes inside it. Both
        come from the source (R6a): the one seal lock is R6's, shared with its gate."""
        with self._source.seal_snapshot() as state:
            if type(state) is not SealState:
                raise TypeError("seal_snapshot must yield a SealState")
            yield _Sealer(_TOKEN, self, signer, state, self._wk)

    def _k_push(self, epoch: int) -> bytes:
        if not self.serves(epoch):
            raise KeyMissing("epoch key not held")
        wk = self._source.wk(epoch)
        assert wk is not None
        return cpush.k_push(wk, self._ws, epoch)

    @contextlib.contextmanager
    def push_sealing(self, signer: RelaySigner) -> Iterator[_PushSealer]:
        """H13-1 (11A.16.2): the seal lock and ONE snapshot of the seal state, like :meth:`sealing`, for push objects.
        The caller seals AND persists the outer inside the span. A sealer is bound to the snapshot's committed epoch
        and refuses while the gate is up."""
        with self._source.seal_snapshot() as state:
            if type(state) is not SealState:
                raise TypeError("seal_snapshot must yield a SealState")
            yield _PushSealer(_TOKEN, self, signer, state, self._ws, self._k_push)


class _PushSealer:
    """Seals push objects under one :class:`SealState` snapshot (11A.16.2 ``_PushSealer``). Built only by
    :meth:`BridgeKeys.push_sealing` (the token); the key never leaves ``BridgeKeys``: ``channel`` is its
    ``K_push(epoch)`` closure and is used only inside :meth:`seal_push`."""

    def __init__(
        self, token: object, keys: BridgeKeys, signer: RelaySigner, state: SealState, ws: bytes,
        channel: Callable[[int], bytes],
    ) -> None:  # fmt: skip
        if token is not _TOKEN:
            raise TypeError("a push sealer is built only by BridgeKeys.push_sealing")
        self._keys, self._signer, self.state, self._ws, self._channel = keys, signer, state, ws, channel

    @property
    def sealable(self) -> bool:
        """The snapshot's committed epoch is held and the rotation gate is down."""
        return not self.state.gated and self._keys.serves(self.state.current)

    @property
    def epoch(self) -> int:
        return self.state.current

    def seal_push(self, payload: Mapping[str, Any]) -> tuple[bytes, int]:
        """``(cj(outer), epoch)`` for ``payload`` = exactly ``{kind, id, label, ts_ms}`` (the label unpadded: it is
        a fixed point of ``clean_label``, padded here to 80 code points), signed inside by ``RelaySigner.push`` and
        sealed under ``K_push`` of the snapshot's epoch. :class:`SealRefused` when the snapshot is not sealable;
        :class:`orch.crypto.push.PushError` for a bad payload or an outer above 3,072 bytes."""
        if not self.sealable:
            raise SealRefused("no push under a gated or unheld epoch")
        if not isinstance(payload, Mapping) or set(payload) != {"kind", "id", "label", "ts_ms"}:
            raise cpush.PushError("input", "payload is exactly {kind, id, label, ts_ms}")
        epoch = self.state.current
        padded = cpush.push_payload(payload["kind"], payload["id"], payload["label"], payload["ts_ms"])
        sig = self._signer.push(epoch, padded)
        return cpush.seal_push(self._channel(epoch), self._ws, epoch, padded, sig), epoch


class _Sealer:
    """The only way to seal a response chunk, bound to one :class:`SealState` snapshot (G1). Private: built only by
    :meth:`BridgeKeys.sealing` (the token), never with a caller's own (forged) :class:`SealState`."""

    def __init__(
        self, token: object, keys: BridgeKeys, signer: RelaySigner, state: SealState, channel: Callable[[int], bytes]
    ) -> None:
        if token is not _TOKEN:
            raise TypeError("a sealer is built only by BridgeKeys.sealing")
        self._keys, self._signer, self.state, self._channel = keys, signer, state, channel

    def data_ok(self, epoch: int) -> bool:
        """Data may be sealed under ``epoch``: it is the snapshot's committed epoch, the gate is down, the key held."""
        return epoch == self.state.current and not self.state.gated and self._keys.serves(epoch)

    def _seal(self, request: wire.Header, idx: int, flags: int, ts_ms: int, plaintext: bytes) -> bytes:
        h = wire.Header(
            2, 2, flags, wire.SUITE, request.workspace, request.device, request.rid, bytes(16), idx, ts_ms,
            request.epoch, secrets.token_bytes(16),
        )  # fmt: skip
        hb = h.pack()
        body = wire.seal_body(self._channel(request.epoch), hb, plaintext)
        return wire.build_envelope(hb, body, self._signer.response_chunk(hb, body))

    def seal_response(self, *, request: wire.Header, idx: int, last: bool, ts_ms: int, plaintext: bytes) -> bytes:
        """A DATA chunk (never ``REFUSAL``): only under the snapshot's committed epoch with the gate down, else
        :class:`SealRefused`."""
        if not self.data_ok(request.epoch):
            raise SealRefused("no data under an old, burned or gated epoch")
        return self._seal(request, idx, wire.FLAG_LAST if last else 0, ts_ms, plaintext)

    def seal_refusal(self, *, request: wire.Header, idx: int, ts_ms: int, code: str, **fields: Any) -> bytes:
        """A ``LAST | REFUSAL`` chunk whose plaintext this method builds from the closed refusal fields
        (:func:`orch.relay.framing.refusal_plaintext`); any served epoch, old or gated included. Under a burned
        epoch only ``stale_epoch`` (proto §7.2 H4: nothing else is ever sealed under it)."""
        if self._keys.burned(request.epoch) and code != "stale_epoch":
            raise SealRefused("only stale_epoch under a burned epoch")
        pt = framing.refusal_plaintext(code, **fields)
        return self._seal(request, idx, wire.FLAG_LAST | wire.FLAG_REFUSAL, ts_ms, pt)
