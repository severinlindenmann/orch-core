"""``RelaySigner``: the only holder of the workspace key (WSK) in ``orch.relay`` (contract 11A.2).

It has no ``sign(payload)``. Each method validates its context, then builds its label and bytes itself:

=================  ==========================================================================  ==================
Method             Checks before signing                                                       Label
=================  ==========================================================================  ==================
``login``          origin and workspace id from the link, kind ``workspace``, challenge 32 B   ``sig_relay_auth``
``response_chunk`` ORB2 direction 2, the link's workspace, an epoch ``BridgeKeys`` holds,      ``sig_bridge``
                   the whole envelope within the relay's 256 KiB chunk cap
``card``           exact field set, the link's workspace, WSK public key = own, ``relay_url`` =  ``sig_card_wsk``
                   the stored origin, ``card_seq`` above the last accepted one and not below
                   the last signed one, never two different cards at one ``card_seq``
=================  ==========================================================================  ==================

The key comes in as a :class:`WorkspaceKey`, a structural interface (``public_key``, ``sign(payload)``) that
``orch.store.store.BackendSigner`` satisfies; this module does not import it, ``FileBackend`` or ``orch.custody``
(``tests/relay/test_import_ban.py``). The custody label backstop (``HOST_LABELS``) stays with the backend. This is an
API rule, not key isolation (11A.2 "What this is not"): the key is captured in a closure and the signer exposes only
its purpose-specific methods, and ``tests/relay/test_import_ban.py`` keeps ``orch.relay`` from reaching private names,
dynamic attribute access or ``orch.crypto`` signing entry points; but same-process code can always traverse objects
(closures included), and the key file is readable by the host UID.

**Last-signed ledger** (review M2, 11A.2 "``card_seq`` > the last signed one"). ``card`` needs a durable
:class:`orch.relay.state.SignLedger`. Each signed card is recorded as ``("card", card_seq) -> (sha256(cj(o)), sig)``
**before** the signature is returned, so a crash cannot leave two different WSK-signed cards at one ``card_seq``. The
same card at a recorded ``card_seq`` gets the recorded signature back (no re-sign: the WSK is ECDSA P-256 with
randomised signatures, so a re-sign would give other bytes); a different card there, or any card below the highest
recorded ``card_seq``, is refused.

Epoch keys come in as an :class:`EpochKeys` view (R2's ``BridgeKeys``); without one, ``response_chunk`` refuses.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from orch import canon, crypto
from orch.crypto.labels import L

from . import wire
from .state import SignLedger

__all__ = ["CARD_FIELDS", "EpochKeys", "LinkContext", "RelaySigner", "SignerRefused", "WorkspaceKey"]

CARD_FIELDS = frozenset(
    {
        "v",
        "suite",
        "kind",
        "workspace_id",
        "wsk_pub",
        "wxk_pub",
        "wxk_version",
        "owner_person_id",
        "relay_url",
        "card_seq",
        "issued_ms",
        "sealed_hash",
        "ck_commit",
    }
)


class SignerRefused(ValueError):
    """A signing request outside its method's context. ``what`` names the rule; never any input bytes."""

    def __init__(self, what: str) -> None:
        super().__init__(f"relay signer refused: {what}")
        self.what = what


class WorkspaceKey(Protocol):
    """The WSK as a signer: what ``orch.store.store.BackendSigner`` already provides."""

    @property
    def public_key(self) -> bytes: ...

    def sign(self, payload: bytes) -> bytes: ...


class EpochKeys(Protocol):
    """What R2's ``BridgeKeys`` tells the signer: whether it holds the key of an epoch (burned keys included)."""

    def holds(self, epoch: int) -> bool: ...


@dataclass(frozen=True)
class LinkContext:
    """The context every relay signature is bound to, from ``link.json``: the workspace id (32 lower-case hex) and the
    relay origin, stored verbatim (11A.2)."""

    workspace_id: str
    origin: str

    def __post_init__(self) -> None:
        try:
            crypto.unhex(self.workspace_id, 16)
            wire.check_origin(self.origin)
        except (crypto.EncodingError, wire.WireError):
            raise SignerRefused("link context") from None


def _bind(key: WorkspaceKey) -> Callable[[bytes], bytes]:
    """The key's ``sign`` captured in a closure: the signer keeps no attribute that names the key object."""

    def sign_bytes(payload: bytes) -> bytes:
        return key.sign(payload)

    return sign_bytes


class RelaySigner:
    """Purpose-specific WSK signatures for the relay client (11A.2)."""

    __slots__ = ("_epochs", "_last_card_seq", "_ledger", "_lock", "_pub", "_sign_bytes", "link")

    def __init__(
        self,
        key: WorkspaceKey,
        link: LinkContext,
        *,
        epochs: EpochKeys | None = None,
        ledger: SignLedger | None = None,
        last_card_seq: int = 0,
    ) -> None:
        if type(last_card_seq) is not int or last_card_seq < 0:
            raise SignerRefused("last card_seq")
        self.link, self._epochs = link, epochs
        self._pub = bytes(key.public_key)
        self._sign_bytes = _bind(key)
        self._last_card_seq = last_card_seq
        self._ledger = ledger
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"RelaySigner(workspace={self.link.workspace_id}, origin={self.link.origin})"

    @property
    def public_key(self) -> bytes:
        return self._pub

    def _sign(self, payload: bytes) -> bytes:
        sig = self._sign_bytes(payload)
        if type(sig) is not bytes or len(sig) != crypto.SIG_LEN:
            raise SignerRefused("the key returned a malformed signature")
        return sig

    def login(self, challenge: bytes) -> bytes:
        """The raw signature of a workspace login over ``auth_bytes(origin, challenge, "workspace", ws)``."""
        if type(challenge) is not bytes or len(challenge) != wire.CHALLENGE_LEN:
            raise SignerRefused("challenge must be 32 bytes")
        msg = wire.auth_bytes(self.link.origin, challenge, "workspace", self.link.workspace_id)
        return self._sign(msg)

    def response_chunk(self, header: bytes, body: bytes) -> bytes:
        """The raw signature of a response chunk (ORB2 direction 2) over ``header || body``."""
        try:
            h = wire.parse_header(header)
        except wire.WireError:
            raise SignerRefused("ORB2 header") from None
        if h.direction != 2:
            raise SignerRefused("not a response header")
        if not wire.ct_equal(h.workspace, bytes.fromhex(self.link.workspace_id)):
            raise SignerRefused("header workspace")
        if self._epochs is None or h.epoch < 1 or not self._epochs.holds(h.epoch):
            raise SignerRefused("epoch not held")
        if type(body) is not bytes or len(body) < wire.TAG_LEN:
            raise SignerRefused("body")
        if wire.HEADER_LEN + len(body) + wire.SIG_LEN > wire.MAX_CHUNK:
            raise SignerRefused("chunk over 256 KiB")
        return self._sign(wire.bridge_signed_bytes(header, body))

    def card(self, o: Mapping[str, Any]) -> str:
        """The b64u ``sig`` of a card object over ``"orch/v2/sig/card-wsk|" || cj(o)`` (proto §7.1)."""
        if not isinstance(o, Mapping) or set(o) != CARD_FIELDS:
            raise SignerRefused("card field set")
        if o["v"] != 2 or o["suite"] != crypto.SUITE_ID or o["kind"] != "card":
            raise SignerRefused("card v, suite or kind")
        if o["workspace_id"] != self.link.workspace_id:
            raise SignerRefused("card workspace")
        if o["wsk_pub"] != crypto.b64u(self._pub):
            raise SignerRefused("card wsk_pub is not this workspace's key")
        if o["relay_url"] != self.link.origin:
            raise SignerRefused("card relay_url is not the stored origin")
        seq = o["card_seq"]
        if type(seq) is not int or seq < 1:
            raise SignerRefused("card_seq")
        try:
            body = canon.dumps(dict(o))
        except canon.JcsError:
            raise SignerRefused("card is not canonical JSON") from None
        if self._ledger is None:
            raise SignerRefused("card signing needs a durable sign ledger")
        digest = crypto.sha256(body).hex()
        with self._lock:
            if seq <= self._last_card_seq:
                raise SignerRefused("card_seq not above the last accepted card")
            held = self._ledger.get("card", str(seq))
            if held is not None:
                if not wire.ct_equal(held[0].encode(), digest.encode()):
                    raise SignerRefused("another card was signed at this card_seq")
                return held[1]
            if any(int(k) > seq for k in self._ledger.keys("card")):
                raise SignerRefused("a card was signed at a later card_seq")
            sig = crypto.b64u(self._sign(L["sig_card_wsk"] + body))
            self._ledger.record("card", str(seq), digest, sig)  # durable before the signature leaves
        return sig
