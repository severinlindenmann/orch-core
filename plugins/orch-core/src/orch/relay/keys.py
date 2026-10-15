"""R2's view of R6 (contract 11A.1, 11A.2): ``BridgeKeys`` (the only user of the epoch keys ``WK_e`` in the bridge) and
the read-only ``R6View`` of the accepted member list.

R6 is a security dependency of R2, not a fixture (11A.1): R2 is tested against a fixture ``R6View``, and ``RelayWorker``
(R5) does not start the poll thread until R6 is merged. Nothing here writes R6 state.

* :class:`BridgeKeys` opens a request body and seals a response chunk; it hands out plaintext or envelopes, never a key
  (11A.2 "Epoch keys are used only through BridgeKeys"). Burned epochs count as held (proto §8.5 G2b). It is also the
  :class:`orch.relay.sign.EpochKeys` the signer asks before it signs a chunk.
* :class:`R6View` returns the current accepted member list: per device its person, its scopes and its certificate, and
  the owner-verified person key (``pk_pub``) the certificate is checked under.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from . import wire
from .sign import RelaySigner

__all__ = ["BridgeKeys", "EpochKeySource", "KeyMissing", "Member", "MemberList", "R6View"]


class KeyMissing(Exception):
    """No key for this epoch (above current, deleted, never held): the request is dropped (proto §8.6 step 2)."""


class EpochKeySource(Protocol):
    """R6's custody of ``WK_e``: the current epoch, and the key of an epoch still held (burned keys included)."""

    def current_epoch(self) -> int: ...

    def wk(self, epoch: int) -> bytes | None: ...


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

    def holds(self, epoch: int) -> bool:
        return type(epoch) is int and 1 <= epoch <= self.current and self._source.wk(epoch) is not None

    def _channel(self, epoch: int) -> bytes:
        if not self.holds(epoch):
            raise KeyMissing("epoch key not held")
        wk = self._source.wk(epoch)
        assert wk is not None
        return wire.k_bridge(wk, self._ws, epoch)

    def open_request(self, header: bytes, body: bytes) -> bytes:
        """The plaintext of a request body; :class:`KeyMissing` (drop) or :class:`wire.WireError` ``tag`` (drop)."""
        h = wire.parse_header(header)
        if h.epoch > self.current:
            raise KeyMissing("future epoch")
        return wire.open_body(self._channel(h.epoch), header, body)

    def seal_response(
        self, signer: RelaySigner, *, request: wire.Header, idx: int, flags: int, ts_ms: int, plaintext: bytes
    ) -> bytes:
        """One response chunk for ``request`` at index ``idx``: sealed under the request's epoch (an old epoch only
        for refusals: the caller's rule, 11A.5), signed by the WSK through :meth:`RelaySigner.response_chunk`."""
        h = wire.Header(
            2, 2, flags, wire.SUITE, request.workspace, request.device, request.rid, bytes(16), idx, ts_ms,
            request.epoch, secrets.token_bytes(16),
        )  # fmt: skip
        hb = h.pack()
        body = wire.seal_body(self._channel(request.epoch), hb, plaintext)
        return wire.build_envelope(hb, body, signer.response_chunk(hb, body))
