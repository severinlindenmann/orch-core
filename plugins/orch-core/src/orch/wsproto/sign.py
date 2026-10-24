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
``member_list``    (R6a, 11A.15.7) exact field set and shape, the link's workspace, the epoch    ``sig_member_list``
                   floor (H4: never a burned epoch, never below the highest signed epoch, one
                   ``wk_commit`` per epoch), never two lists at one ``list_seq`` nor one below
                   the highest signed
``wk_grant``       (R6a) exact field set, the link's workspace, an epoch with a recorded          ``sig_wk_grant``
                   ``wk_commit``, the sealed key's length, the device in the list signed at
                   ``list_seq``; never other bytes for ``(device, epoch)``
``pair_answer``    (R6c) ORB2 direction 2, **epoch 0**, exactly one chunk (``seq`` 0, flags      ``sig_bridge``
                   ``LAST`` or ``LAST|REFUSAL``, never ``STREAM``), the link's workspace,
                   ``stream`` an offer R6 holds live (non-terminal or within its 10-minute tail:
                   the ``pair_live`` callback, not an import of R6 state), the whole envelope
                   within 64 KiB
``cert_request``   (R6c) exact field set, the link's workspace, ``person_id`` the pinned owner,  ``sig_cert_request``
                   ``primary_device_id`` the verified ``primary.json``'s (``primary_device``
                   callback, re-verified at every call), ``expires_ms - created_ms <= 600 000``,
                   and the clear binding (``offer_id``, ``attempt``, ``device_id``,
                   ``dk_hash``) exactly the pairing R6 holds at that step (``pair_binding``
                   callback); never other bytes for one ``request_id`` (ledger)
=================  ==========================================================================  ==================

The key comes in as a :class:`WorkspaceKey`, a structural interface (``public_key``, ``sign(payload)``) that
``orch.store.store.BackendSigner`` satisfies; this module does not import it, ``FileBackend`` or ``orch.custody``
(``tests/relay/test_import_ban.py``). The custody label backstop (``HOST_LABELS``) stays with the backend. This is an
API rule, not key isolation (11A.2 "What this is not"): the key is captured in a closure and the signer exposes only
its purpose-specific methods, and ``tests/relay/test_import_ban.py`` keeps ``orch.relay`` from reaching private names,
dynamic attribute access or ``orch.crypto`` signing entry points; but same-process code can always traverse objects
(closures included), and the key file is readable by the host UID.

**Last-signed ledger** (review M2, 11A.2 "``card_seq`` > the last signed one"). ``card`` needs a durable
:class:`orch.wsproto.state.SignLedger`. Each signed card is recorded as ``("card", card_seq) -> (sha256(cj(o)), sig)``
**before** the signature is returned, so a crash cannot leave two different WSK-signed cards at one ``card_seq``. The
same card at a recorded ``card_seq`` gets the recorded signature back (no re-sign: the WSK is ECDSA P-256 with
randomised signatures, so a re-sign would give other bytes); a different card there, or any card below the highest
recorded ``card_seq``, is refused.

Epoch keys come in as an :class:`EpochKeys` view (R2's ``BridgeKeys``); without one, ``response_chunk`` refuses.

**R6a ledger additions (11A.15.7).** ``("member_list", list_seq)`` (H6), ``("wk_commit", epoch)`` (digest = the
``wk_commit`` itself, no signature: the structural guard of "one WK per epoch") and
``("wk_grant", "<device>:<epoch>")``, each recorded before the signature leaves. ``burned`` is R6's narrow callback
(``MembershipKeys.is_burned``), not an import of R6 state; without it (and without a ledger) ``member_list`` refuses.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from orch import canon, crypto
from orch.crypto.labels import L
from orch.wsproto import wire
from orch.wsproto.state import SignLedger

__all__ = [
    "CARD_FIELDS", "CERT_REQUEST_FIELDS", "GRANT_FIELDS", "MEMBER_LIST_FIELDS", "PAIR_ANSWER_MAX", "EpochKeys",
    "LinkContext", "RelaySigner", "SignerRefused", "WorkspaceKey",
]  # fmt: skip

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


# proto §7.2 / §7.3 (the same sets as ``orch.identity.memberlist``, pinned equal by a test; not imported from there:
# ``orch.identity.wscard`` imports ``orch.wsproto.card``, which imports this module)
MEMBER_LIST_FIELDS = frozenset(
    {"v", "suite", "kind", "workspace_id", "epoch", "list_seq", "issued_ms", "wk_commit", "persons", "members"}
)
_MEMBER_FIELDS = frozenset({"device_id", "person_id", "scopes"})
GRANT_FIELDS = frozenset({"v", "suite", "kind", "workspace_id", "device_id", "epoch", "sealed"})
_SCOPES = ("look", "decide", "operate", "type")
_SEALED_KEY_LEN = crypto.PUB_LEN + 32 + 16
_MAX_EPOCH = 2**32 - 1
_MAX_SAFE = 2**53 - 1
_MAX_MEMBERS = 1024
# R6c (11A.15.7): a pair answer's whole envelope is at most 64 KiB; a cert request's fields (§8.4, the same set as
# ``orch.identity.certreq.REQUEST_FIELDS``, pinned equal by a test) and its 10-minute bound
PAIR_ANSWER_MAX = 65_536
CERT_REQUEST_FIELDS = frozenset(
    {"v", "suite", "kind", "request_id", "workspace_id", "person_id", "primary_device_id", "offer_id", "attempt",
     "device_id", "dk_hash", "created_ms", "expires_ms", "sealed"}
)  # fmt: skip
_CERT_PENDING_MS = 600_000


def _hex32(v: object) -> bool:
    return type(v) is str and len(v) == 32 and all(c in "0123456789abcdef" for c in v)


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

    __slots__ = (
        "_burned", "_epochs", "_last_card_seq", "_ledger", "_lock", "_owner", "_pair_binding", "_pair_live",
        "_primary", "_pub", "_sign_bytes", "link",
    )  # fmt: skip

    def __init__(
        self,
        key: WorkspaceKey,
        link: LinkContext,
        *,
        epochs: EpochKeys | None = None,
        ledger: SignLedger | None = None,
        last_card_seq: int = 0,
        burned: Callable[[int], bool] | None = None,
        owner_person_id: str | None = None,
        primary_device: Callable[[], str | None] | None = None,
        pair_live: Callable[[str], bool] | None = None,
        pair_binding: Callable[[str, str, str, str], bool] | None = None,
    ) -> None:
        if type(last_card_seq) is not int or last_card_seq < 0:
            raise SignerRefused("last card_seq")
        self.link, self._epochs = link, epochs
        self._pub = bytes(key.public_key)
        self._sign_bytes = _bind(key)
        self._last_card_seq = last_card_seq
        self._ledger = ledger
        self._burned = burned
        self._owner = owner_person_id
        self._primary = primary_device
        self._pair_live = pair_live
        self._pair_binding = pair_binding
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

    def push(self, epoch: int, payload: Mapping[str, Any]) -> bytes:
        """The raw ``WSK`` signature of a push payload (H13-1, 11A.16.2) over ``sig_push || suite || ws || u32(epoch) ||
        cj(payload)`` (protocol-v2 §9). Called by ``_PushSealer.seal_push`` only. Checks: the payload is exactly
        ``{kind, id, label, ts_ms}`` with a host kind and a padded label, and ``epoch`` is one ``BridgeKeys`` holds.
        The workspace is the link's."""
        from orch.crypto import push as cpush

        if self._epochs is None or type(epoch) is not int or epoch < 1 or not self._epochs.holds(epoch):
            raise SignerRefused("push epoch not held")
        try:
            p = dict(payload)
            if p.get("kind") not in cpush.HOST_PUSH_KINDS or type(p.get("label")) is not str:
                raise cpush.PushError("input", "kind")
            if cpush.pad_label(p["label"].rstrip(" ")) != p["label"]:
                raise cpush.PushError("input", "label is not padded")
            signed = cpush.push_signed_bytes(bytes.fromhex(self.link.workspace_id), epoch, p)
        except (cpush.PushError, TypeError, ValueError):
            raise SignerRefused("push payload") from None
        return self._sign(signed)

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
            # the highest-seq rule first (round 6, orch-52 #5): a cached older signature is never handed out again
            if any(int(k) > seq for k in self._ledger.keys("card")):
                raise SignerRefused("a card was signed at a later card_seq")
            held = self._ledger.get("card", str(seq))
            if held is not None:
                if not wire.ct_equal(held[0].encode(), digest.encode()):
                    raise SignerRefused("another card was signed at this card_seq")
                return held[1]
            sig = crypto.b64u(self._sign(L["sig_card_wsk"] + body))
            self._ledger.record("card", str(seq), digest, sig)  # durable before the signature leaves
        return sig

    # -- R6a: member lists and WK grants (11A.15.7) ------------------------------------------------------------------

    def _member_list_shape(self, o: Mapping[str, Any]) -> tuple[int, int, str, frozenset[str]]:
        """``(epoch, list_seq, wk_commit, device ids)`` of a well-formed list ``o`` of this workspace, else refused."""
        if not isinstance(o, Mapping) or set(o) != MEMBER_LIST_FIELDS:
            raise SignerRefused("member list field set")
        if o["v"] != 2 or o["suite"] != crypto.SUITE_ID or o["kind"] != "member_list":
            raise SignerRefused("member list v, suite or kind")
        if o["workspace_id"] != self.link.workspace_id:
            raise SignerRefused("member list workspace")
        epoch, seq, issued = o["epoch"], o["list_seq"], o["issued_ms"]
        if type(epoch) is not int or not 1 <= epoch <= _MAX_EPOCH:
            raise SignerRefused("member list epoch")
        if type(seq) is not int or not 1 <= seq <= _MAX_SAFE or type(issued) is not int or not 0 <= issued <= _MAX_SAFE:
            raise SignerRefused("member list list_seq or issued_ms")
        commit = o["wk_commit"]
        try:
            if type(commit) is not str or crypto.b64u(crypto.unb64u(commit, 32)) != commit:
                raise SignerRefused("member list wk_commit")
        except crypto.EncodingError:
            raise SignerRefused("member list wk_commit") from None
        members, persons = o["members"], o["persons"]
        if type(members) is not list or type(persons) is not list or len(members) > _MAX_MEMBERS:
            raise SignerRefused("member list members or persons")
        ids, member_persons = [], set()
        for m in members:
            if not isinstance(m, Mapping) or set(m) != _MEMBER_FIELDS:
                raise SignerRefused("member entry field set")
            sc = m["scopes"]
            if not _hex32(m["device_id"]) or not _hex32(m["person_id"]):
                raise SignerRefused("member entry ids")
            if type(sc) is not list or not sc or tuple(sc) != _SCOPES[: len(sc)]:
                raise SignerRefused("member entry scopes")
            ids.append(m["device_id"])
            member_persons.add(m["person_id"])
        if ids != sorted(set(ids)):
            raise SignerRefused("member ids not sorted and unique")
        if any(not _hex32(p) for p in persons) or persons != sorted(set(persons)):
            raise SignerRefused("persons not sorted and unique")
        if not set(persons) <= member_persons:
            raise SignerRefused("a listed person without a member")
        return epoch, seq, commit, frozenset(ids)

    def member_list(self, o: Mapping[str, Any]) -> str:
        """The b64u ``sig`` of a member list over ``"orch/v2/sig/member-list|" || cj(o)`` (proto §7.2). The H4 epoch
        floor first: a burned epoch, or one below the highest epoch with a recorded ``wk_commit``, is refused; an
        epoch already recorded only with the same ``wk_commit``. Then ``("member_list", list_seq)``: the same list
        gets the recorded signature back, another list there or any list below the highest signed is refused."""
        epoch, seq, commit, _ = self._member_list_shape(o)
        try:
            body = canon.dumps(dict(o))
        except canon.JcsError:
            raise SignerRefused("member list is not canonical JSON") from None
        if self._ledger is None or self._burned is None:
            raise SignerRefused("member list signing needs a durable sign ledger and R6's burned-epoch view")
        digest = crypto.sha256(body).hex()
        with self._lock:
            if self._burned(epoch):
                raise SignerRefused("a burned epoch is never signed")
            if any(int(k) > epoch for k in self._ledger.keys("wk_commit")):
                raise SignerRefused("epoch below the highest signed epoch (H4 floor)")
            held = self._ledger.get("wk_commit", str(epoch))
            if held is not None and not wire.ct_equal(held[0].encode(), commit.encode()):
                raise SignerRefused("another wk_commit was signed at this epoch")
            if any(int(k) > seq for k in self._ledger.keys("member_list")):
                raise SignerRefused("a member list was signed at a later list_seq")
            prior = self._ledger.get("member_list", str(seq))
            if prior is not None:
                if not wire.ct_equal(prior[0].encode(), digest.encode()):
                    raise SignerRefused("another member list was signed at this list_seq")
                return prior[1]
            if held is None:
                self._ledger.record("wk_commit", str(epoch), commit, "")  # durable: one WK per epoch, ever
            sig = crypto.b64u(self._sign(L["sig_member_list"] + body))
            self._ledger.record("member_list", str(seq), digest, sig)  # durable before the signature leaves
        return sig

    def wk_grant(self, o: Mapping[str, Any], *, signed_list: Mapping[str, Any]) -> str:
        """The b64u ``sig`` of a WK grant over ``"orch/v2/sig/wk-grant|" || cj(o)`` (proto §7.3). ``signed_list`` is
        the list ``o`` this grant travels with: it must be the one recorded at its ``list_seq`` (same digest), at
        the grant's epoch, and name the device. ``("wk_grant", device, epoch)``: other bytes are refused, the same
        bytes get the recorded signature back."""
        if not isinstance(o, Mapping) or set(o) != GRANT_FIELDS:
            raise SignerRefused("grant field set")
        if o["v"] != 2 or o["suite"] != crypto.SUITE_ID or o["kind"] != "wk_grant":
            raise SignerRefused("grant v, suite or kind")
        if o["workspace_id"] != self.link.workspace_id or not _hex32(o["device_id"]):
            raise SignerRefused("grant workspace or device")
        epoch = o["epoch"]
        if type(epoch) is not int or not 1 <= epoch <= _MAX_EPOCH:
            raise SignerRefused("grant epoch")
        try:
            sealed = crypto.unb64u(o["sealed"])
        except (crypto.EncodingError, TypeError):
            raise SignerRefused("grant sealed") from None
        if len(sealed) != _SEALED_KEY_LEN:
            raise SignerRefused("grant sealed length")
        l_epoch, seq, _, ids = self._member_list_shape(signed_list)
        if l_epoch != epoch or o["device_id"] not in ids:
            raise SignerRefused("the device is not in the list signed for this epoch")
        try:
            body = canon.dumps(dict(o))
            list_digest = crypto.sha256(canon.dumps(dict(signed_list))).hex()
        except canon.JcsError:
            raise SignerRefused("grant is not canonical JSON") from None
        if self._ledger is None or self._burned is None:
            raise SignerRefused("grant signing needs a durable sign ledger and R6's burned-epoch view")
        digest = crypto.sha256(body).hex()
        key = f"{o['device_id']}:{epoch}"
        with self._lock:
            if self._burned(epoch):
                raise SignerRefused("nothing is granted under a burned epoch (H4)")
            if self._ledger.get("wk_commit", str(epoch)) is None:
                raise SignerRefused("no wk_commit recorded for this epoch")
            signed = self._ledger.get("member_list", str(seq))
            if signed is None or not wire.ct_equal(signed[0].encode(), list_digest.encode()):
                raise SignerRefused("the list was not signed at its list_seq")
            prior = self._ledger.get("wk_grant", key)
            if prior is not None:
                if not wire.ct_equal(prior[0].encode(), digest.encode()):
                    raise SignerRefused("another grant was signed for this device and epoch")
                return prior[1]
            sig = crypto.b64u(self._sign(L["sig_wk_grant"] + body))
            self._ledger.record("wk_grant", key, digest, sig)
        return sig

    # -- R6c: pair answers and cert requests (11A.15.7) --------------------------------------------------------------

    def pair_answer(self, header: bytes, body: bytes) -> bytes:
        """The raw signature of a pair answer (ORB2 direction 2, **epoch 0**) over ``header || body``. ``stream`` must
        name an offer R6 holds live (``pair_live``: a non-terminal offer or one within its 10-minute tail); the whole
        envelope is at most 64 KiB. No ledger: answers are stored per ``rid`` instead (``pairing/<offer>.answers``).
        ``response_chunk`` keeps refusing epoch 0, so neither can pass as the other."""
        try:
            h = wire.parse_header(header)
        except wire.WireError:
            raise SignerRefused("ORB2 header") from None
        if h.direction != 2 or h.epoch != 0:
            raise SignerRefused("not an epoch-0 answer header")
        # an epoch-0 answer is exactly one chunk (the phone accepts only these flags, proto §8.3 step 4): never a
        # stream frame, never a later index, so a signed answer cannot be replayed as part of another exchange
        if h.seq != 0 or h.flags not in (wire.FLAG_LAST, wire.FLAG_LAST | wire.FLAG_REFUSAL):
            raise SignerRefused("a pair answer is one chunk: seq 0, LAST or LAST|REFUSAL")
        if not wire.ct_equal(h.workspace, bytes.fromhex(self.link.workspace_id)):
            raise SignerRefused("header workspace")
        if type(body) is not bytes or len(body) < wire.TAG_LEN:
            raise SignerRefused("body")
        if wire.HEADER_LEN + len(body) + wire.SIG_LEN > PAIR_ANSWER_MAX:
            raise SignerRefused("pair answer over 64 KiB")
        offer = h.stream.hex()
        if self._pair_live is None or offer == "0" * 32 or self._pair_live(offer) is not True:
            raise SignerRefused("the stream is not an offer R6 holds live")
        return self._sign(wire.bridge_signed_bytes(header, body))

    def cert_request(self, o: Mapping[str, Any]) -> str:
        """The b64u ``sig`` of a cert request over ``"orch/v2/sig/cert-request|" || cj(o)`` (proto §8.4 step 1).
        ``person_id`` must be the pinned owner and ``primary_device_id`` the device of the verified ``primary.json``
        (``primary_device()``, which R6 re-verifies under the genesis ``PK`` at every call; ``None``: no valid
        primary, refused). ``("cert_request", request_id)`` is recorded before the signature leaves: the same bytes
        get the recorded signature back, other bytes are refused (R6 never re-signs a request: it resends)."""
        if not isinstance(o, Mapping) or set(o) != CERT_REQUEST_FIELDS:
            raise SignerRefused("cert request field set")
        if o["v"] != 2 or o["suite"] != crypto.SUITE_ID or o["kind"] != "cert_request":
            raise SignerRefused("cert request v, suite or kind")
        if o["workspace_id"] != self.link.workspace_id:
            raise SignerRefused("cert request workspace")
        if not _hex32(o["request_id"]) or not _hex32(o["person_id"]) or not _hex32(o["primary_device_id"]):
            raise SignerRefused("cert request ids")
        if self._owner is None or not wire.ct_equal(o["person_id"].encode(), self._owner.encode()):
            raise SignerRefused("cert request person is not the pinned owner")
        primary = self._primary() if self._primary is not None else None
        if type(primary) is not str or not wire.ct_equal(o["primary_device_id"].encode(), primary.encode()):
            raise SignerRefused("cert request primary is not the verified primary")
        created, expires = o["created_ms"], o["expires_ms"]
        if type(created) is not int or type(expires) is not int or not 0 <= created < expires <= _MAX_SAFE:
            raise SignerRefused("cert request times")
        if expires - created > _CERT_PENDING_MS:
            raise SignerRefused("cert request longer than 10 minutes")
        # the clear binding (G3b) names exactly the pairing R6 holds at step 7b: this offer, consumed by this attempt
        # of this device with these keys (the wk_grant sibling binds its device to the signed list the same way)
        bound = (o["offer_id"], o["attempt"], o["device_id"], o["dk_hash"])
        if not all(type(v) is str for v in bound):
            raise SignerRefused("cert request binding")
        if self._pair_binding is None or self._pair_binding(*bound) is not True:
            raise SignerRefused("the binding is not a pairing R6 holds at the cert request")
        try:
            body = canon.dumps(dict(o))
        except canon.JcsError:
            raise SignerRefused("cert request is not canonical JSON") from None
        if self._ledger is None:
            raise SignerRefused("cert request signing needs a durable sign ledger")
        digest = crypto.sha256(body).hex()
        with self._lock:
            held = self._ledger.get("cert_request", o["request_id"])
            if held is not None:
                if not wire.ct_equal(held[0].encode(), digest.encode()):
                    raise SignerRefused("another cert request was signed with this request_id")
                return held[1]
            sig = crypto.b64u(self._sign(L["sig_cert_request"] + body))
            self._ledger.record("cert_request", o["request_id"], digest, sig)  # durable before the signature leaves
        return sig
