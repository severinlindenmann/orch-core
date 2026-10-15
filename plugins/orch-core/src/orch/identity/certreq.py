"""Pure builders and checks for R6's ``cert_pending`` path (design 11A.15.10, H12c item 8 and the pure half of item 10):
the host's cert request, the primary's checks and opening of it, the primary's challenge and rejection, and the host's
checks of that challenge and rejection before it relays them.

Specs: orch-relay ``docs/protocol-v2.md`` §8.4 (request, checks, challenge, rejection, G3b binding) and §5.3
(purpose ``cert-request``). Vectors: the relay corpus sections ``cert_request`` (``signed``, ``inner``, ``cases``,
``open_cases``, ``challenge``, ``attack_challenge``, ``primary_nonce``, ``signed_bytes_challenge``),
``cert_binding`` (``relay``, ``primary``) and ``rejected_forms.objects`` (``tests/identity/test_certreq.py``).

No I/O, no clock, no key held. The primary's private keys stay in custody: :func:`open_cert_request` takes an
``unseal`` callable (the ``kx`` key's purpose-bound unseal, item 9), and the PK-signed builders take a ``sign``
callable over the exact signed bytes (a ``PersonKeySource``-bound signer, owner decision Q1 = B: the person key is
never stored). Which custody may stand behind those callables is the call site's rule, not this module's: relay
pairing refuses convenience custody (11A.15.6, owner rule 11A.11 Q4), and the CLI that drives the primary enforces
it. Every check returns a typed frozen object or raises :class:`Refused` with the protocol's code.

Labels: the request's inner ``label`` is :func:`orch.identity.pairing.clean_label`'s output **unchanged** (no NFC
anywhere, 11A.15.13); the builder refuses any other text. The primary returns the inner label as the host sealed it
and displays it only through ``clean_label``, as "named by the device".
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidTag

from orch import canon, crypto
from orch.crypto.labels import L

from .certs import SCOPE_ORDER, SKEW_MS, scopes_ok
from .errors import Refused
from .pairing import (
    _SIG_LABEL,
    CHALLENGE_FIELDS,
    MAX_LABEL,
    _b64_pub,
    _bad,
    _id,
    _pub,
    _raw,
    _verify_signed,
    cert_code,
    clean_label,
)
from .wscard import VerifiedCard

__all__ = [
    "CERT_PENDING_MS",
    "RequestRefused",
    "INNER_FIELDS",
    "REJECTION_FIELDS",
    "REQUEST_FIELDS",
    "CertChallenge",
    "CertInner",
    "CertJournal",
    "CertRejection",
    "CertRequest",
    "check_cert_challenge",
    "check_cert_rejection",
    "check_cert_request",
    "cert_request_o",
    "make_cert_challenge",
    "make_cert_rejection",
    "open_cert_request",
]

CERT_PENDING_MS = 600_000  # §8.4: expires_ms = created_ms + 10 min
MAX_SAFE_INT = 2**53 - 1
REQUEST_FIELDS = frozenset(
    {
        "v",
        "suite",
        "kind",
        "request_id",
        "workspace_id",
        "person_id",
        "primary_device_id",
        "offer_id",
        "attempt",
        "device_id",
        "dk_hash",
        "created_ms",
        "expires_ms",
        "sealed",
    }
)
INNER_FIELDS = frozenset({"device_id", "dk_sig_pub", "dk_kx_pub", "label", "scopes_max", "offer_id", "attempt"})
REJECTION_FIELDS = frozenset(
    {"v", "suite", "kind", "request_id", "workspace_id", "person_id", "offer_id", "attempt", "device_id", "dk_hash",
     "expires_ms"}
)  # fmt: skip
_REQUEST_IDS = ("request_id", "workspace_id", "person_id", "primary_device_id", "offer_id", "attempt", "device_id")

Signer = Callable[[bytes], bytes]
Unseal = Callable[[bytes, bytes], bytes]


def _sign(o: dict[str, Any], sign: Signer) -> dict[str, Any]:
    sig = sign(L[_SIG_LABEL[o["kind"]]] + canon.dumps(o))
    if type(sig) is not bytes or len(sig) != crypto.SIG_LEN:
        raise Refused("malformed", "signer returned a bad signature")
    return {"o": o, "sig": crypto.b64u(sig)}


def _phone_scopes(value: object) -> list[str]:
    """``scopes_max`` a host may request: a non-empty prefix of ``look, decide, operate, type`` (no ``drop:``)."""
    if type(value) not in (list, tuple) or not value or list(value) != SCOPE_ORDER[: len(value)]:
        raise _bad("scopes_max")
    return list(value)


def _dk_hash(sig_pub: bytes, kx_pub: bytes) -> str:
    return crypto.b64u(crypto.dk_hash(sig_pub, kx_pub))


# --- 8a. the host's request (§8.4 step 1) ---------------------------------------------------------------------------


def cert_request_o(
    *,
    request_id: str,
    workspace_id: str,
    person_id: str,
    primary_device_id: str,
    primary_kx_pub: bytes,
    offer_id: str,
    attempt: str,
    dk_sig_pub: bytes,
    dk_kx_pub: bytes,
    label: str,
    scopes_max: list[str],
    created_ms: int,
) -> dict[str, Any]:
    """The unsigned §8.4 request ``o`` (R6 signs it through ``RelaySigner.cert_request``): the clear G3b binding
    (``offer_id``, ``attempt``, ``device_id = device_id(dk_sig_pub)``, ``dk_hash``), ``expires_ms = created_ms + 10
    min``, and ``sealed`` = ``seal_to(primary dk_kx, "cert-request", object = request_id, epoch 0, cj(inner))`` with
    ``inner = {device_id, dk_sig_pub, dk_kx_pub, label, scopes_max, offer_id, attempt}`` and no host nonce.
    ``primary_kx_pub`` and ``primary_device_id`` come from the primary's **verified** certificate
    (``membership/primary.json``, 11A.15.1): whoever holds that ``kx`` key reads the inner (keys and label).

    ``label`` must be :func:`clean_label`'s output (refused otherwise) and is sealed unchanged; ``scopes_max`` is a
    non-empty prefix of ``look, decide, operate, type``. The seal draws a fresh ephemeral key."""
    rid = _id(request_id, "request_id")
    primary = _id(primary_device_id, "primary_device_id")
    ids = {
        "request_id": crypto.hex_id(rid),
        "workspace_id": crypto.hex_id(_id(workspace_id, "workspace_id")),
        "person_id": crypto.hex_id(_id(person_id, "person_id")),
        "primary_device_id": crypto.hex_id(primary),
        "offer_id": crypto.hex_id(_id(offer_id, "offer_id")),
        "attempt": crypto.hex_id(_id(attempt, "attempt")),
    }
    kx_primary = _pub(primary_kx_pub, "primary_kx_pub")
    sig_pub, kx_pub = _pub(dk_sig_pub, "dk_sig_pub"), _pub(dk_kx_pub, "dk_kx_pub")
    if type(label) is not str or clean_label(label) != label:
        raise _bad("label is not clean_label's output")
    if type(created_ms) is not int or not 0 <= created_ms <= MAX_SAFE_INT - CERT_PENDING_MS:
        raise _bad("created_ms")
    device = crypto.hex_id(crypto.device_id(sig_pub))
    inner = {
        "device_id": device,
        "dk_sig_pub": crypto.b64u(sig_pub),
        "dk_kx_pub": crypto.b64u(kx_pub),
        "label": label,
        "scopes_max": _phone_scopes(scopes_max),
        "offer_id": ids["offer_id"],
        "attempt": ids["attempt"],
    }
    try:
        sealed = crypto.seal(kx_primary, primary, "cert-request", rid, 0, canon.dumps(inner))
    except (crypto.CryptoError, canon.JcsError):
        raise _bad("seal") from None
    return {
        "v": 2,
        "suite": crypto.SUITE_ID,
        "kind": "cert_request",
        **ids,
        "device_id": device,
        "dk_hash": _dk_hash(sig_pub, kx_pub),
        "created_ms": created_ms,
        "expires_ms": created_ms + CERT_PENDING_MS,
        "sealed": crypto.b64u(sealed),
    }


# --- 8b. the primary (§8.4 step 2) ----------------------------------------------------------------------------------


class RequestRefused(Refused):
    """A refusal of a request whose ``request_id`` is known (from step 2.4 on: past the signature and the addressee).
    The primary records ``request_id`` as seen (:meth:`CertJournal.with_seen`), so the id can never come back with
    other content (it is then ``replayed``)."""

    def __init__(self, code: str, detail: str, request_id: str) -> None:
        super().__init__(code, detail)
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class CertJournal:
    """The primary's durable memory of requests, as the §8.4 step 2 checks read it (the caller persists it):
    ``seen``, every request id it ever opened or refused from step 2.4 on (``replayed``); ``open``,
    ``(workspace_id, request_id, expires_ms)`` per opened request; ``closed``, the request ids it signed or rejected.
    A request is open while it is unexpired and neither signed nor rejected (``request_open``).

    **One lock, record first (Opus L2).** "One open request per workspace" is the R1 defence, and these checks are
    pure. So the primary runs :func:`check_cert_request`, :func:`open_cert_request` and the journal write
    (:meth:`with_open`) under **one** lock or transaction, and persists the new journal **before** it draws
    ``primary_nonce`` or posts the challenge. Two requests checked concurrently would otherwise both pass
    ``request_open``. A request refused at step 2.4 or later (a :class:`RequestRefused`, or any refusal of
    :func:`open_cert_request`) is recorded with :meth:`with_seen` under the same lock."""

    seen: frozenset[str] = frozenset()
    open: tuple[tuple[str, str, int], ...] = ()
    closed: frozenset[str] = frozenset()

    def open_request(self, workspace_id: str, now_ms: int) -> str | None:
        for ws, rid, expires in self.open:
            if ws == workspace_id and now_ms < expires and rid not in self.closed:
                return rid
        return None

    def with_seen(self, request_id: str) -> CertJournal:
        """The journal after a refused request: its id is never judged fresh again."""
        return CertJournal(seen=self.seen | {crypto.hex_id(_id(request_id, "request_id"))}, open=self.open,
                           closed=self.closed)  # fmt: skip

    def with_open(self, req: CertRequest) -> CertJournal:
        """The journal after ``req`` passed both checks: seen, and the workspace's open request."""
        if type(req) is not CertRequest:
            raise _bad("req")
        rest = tuple(e for e in self.open if e[0] != req.workspace_id)
        return CertJournal(seen=self.seen | {req.request_id}, open=(*rest, (req.workspace_id, req.request_id,
                           req.expires_ms)), closed=self.closed)  # fmt: skip

    def with_closed(self, request_id: str) -> CertJournal:
        """The journal after the request was signed or rejected: it no longer blocks the workspace."""
        return CertJournal(seen=self.seen, open=self.open,
                           closed=self.closed | {crypto.hex_id(_id(request_id, "request_id"))})  # fmt: skip


@dataclass(frozen=True, slots=True)
class CertRequest:
    """A request that passed :func:`check_cert_request` (still sealed). ``o`` is the verified object, a copy."""

    request_id: str
    workspace_id: str
    person_id: str
    primary_device_id: str
    offer_id: str
    attempt: str
    device_id: str
    dk_hash: bytes
    created_ms: int
    expires_ms: int
    sealed: bytes = field(repr=False)
    client_hosted: bool
    o: dict[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CertInner:
    """The opened inner of a request. ``label`` is the raw sealed text (at most 80 code points, for the certificate's
    ``label_sealed``); a screen shows :attr:`display_label` (``clean_label`` applied), as named by the device, never
    ``label`` (Opus L5)."""

    device_id: str
    dk_sig_pub: bytes
    dk_kx_pub: bytes
    label: str
    scopes_max: tuple[str, ...]
    offer_id: str
    attempt: str

    @property
    def display_label(self) -> str:
        return clean_label(self.label)

    def as_dict(self) -> dict[str, Any]:
        """The inner object as it was sealed (the ``cj`` field names and b64u keys)."""
        return {
            "device_id": self.device_id,
            "dk_sig_pub": crypto.b64u(self.dk_sig_pub),
            "dk_kx_pub": crypto.b64u(self.dk_kx_pub),
            "label": self.label,
            "scopes_max": list(self.scopes_max),
            "offer_id": self.offer_id,
            "attempt": self.attempt,
        }


def _int(value: object, what: str) -> int:
    if type(value) is not int:
        raise Refused("malformed", what)
    return value


def _request_steps(o: dict[str, Any], ws: str, journal: CertJournal, now_ms: int) -> tuple[int, int, bytes, bytes]:
    """Steps 2.4 to 2.7 (clear part) of a request whose ``request_id`` is a valid id."""
    created = _int(o.get("created_ms"), "created_ms")
    expires = _int(o.get("expires_ms"), "expires_ms")
    if not created - SKEW_MS <= now_ms < expires or expires - created > CERT_PENDING_MS:
        raise Refused("expired", "cert_request")
    if o["request_id"] in journal.seen:
        raise Refused("replayed", "cert_request")
    if journal.open_request(ws, now_ms) is not None:
        raise Refused("request_open", "cert_request")
    if set(o) != REQUEST_FIELDS:
        raise Refused("malformed", "cert_request field set")
    try:
        for k in _REQUEST_IDS:
            _id(o[k], k)
        return created, expires, crypto.unb64u(o["dk_hash"], 32), crypto.unb64u(o["sealed"])
    except (Refused, crypto.EncodingError):
        raise Refused("malformed", "cert_request ids, dk_hash or sealed") from None


def check_cert_request(
    signed: object,
    card: VerifiedCard,
    *,
    me_person_id: str,
    me_device_id: str,
    journal: CertJournal,
    now_ms: int,
) -> CertRequest:
    """§8.4 step 2.1 to 2.7 (the clear part), in the protocol's order: the card is ``me``'s person's
    (``other_person``); ``sig`` under the card's ``WSK`` and the card's workspace (``bad_signature``); addressed to
    ``me`` for ``me``'s person (``not_for_this_device``); ``created - 300 s <= now < expires`` and ``expires - created
    <= 10 min`` (``expired``); a new ``request_id`` (``replayed``); no other open request of the workspace
    (``request_open``); the exact field set and the clear binding's shape (``malformed``). Then
    :func:`open_cert_request` finishes step 2.7 and runs 2.8.

    From step 2.4 on the refusal is a :class:`RequestRefused` naming the id. The caller runs this, the opening and
    the journal write under one lock and records before the challenge goes out (see :class:`CertJournal`)."""
    me_person = crypto.hex_id(_id(me_person_id, "me_person_id"))
    me = crypto.hex_id(_id(me_device_id, "me_device_id"))
    if type(journal) is not CertJournal:
        raise _bad("journal")
    if type(now_ms) is not int:
        raise _bad("now_ms")
    if type(card) is not VerifiedCard:
        raise _bad("card: a VerifiedCard from orch.identity.wscard.verify_card")
    ws, owner, wsk, hosted = card.workspace_id, card.owner_person_id, card.wsk_pub, card.client_hosted
    if owner != me_person:
        raise Refused("other_person", "the card is another person's")
    o = _verify_signed(wsk, signed, "cert_request")
    if o is None or o.get("workspace_id") != ws:
        raise Refused("bad_signature", "cert_request")
    if any(k in o and type(o[k]) is not str for k in (*_REQUEST_IDS, "dk_hash", "sealed")):
        raise Refused("malformed", "cert_request: an id is not a string")  # before any set or dict lookup (Opus L1)
    if o.get("primary_device_id") != me or o.get("person_id") != me_person:
        raise Refused("not_for_this_device", "cert_request")
    try:
        rid = crypto.hex_id(_id(o.get("request_id"), "request_id"))
    except Refused:
        raise Refused("malformed", "cert_request request_id") from None
    try:
        created, expires, dk_hash, sealed = _request_steps(o, ws, journal, now_ms)
    except Refused as e:
        raise RequestRefused(e.code, e.detail, rid) from None
    return CertRequest(
        request_id=o["request_id"],
        workspace_id=o["workspace_id"],
        person_id=o["person_id"],
        primary_device_id=o["primary_device_id"],
        offer_id=o["offer_id"],
        attempt=o["attempt"],
        device_id=o["device_id"],
        dk_hash=dk_hash,
        created_ms=created,
        expires_ms=expires,
        sealed=sealed,
        client_hosted=hosted,
        o=canon.loads_strict(canon.dumps(o)),
    )


def open_cert_request(req: CertRequest, unseal: Unseal) -> CertInner:
    """§8.4 steps 2.7 (the inner) and 2.8. ``unseal(object_id, sealed) -> plaintext`` is the primary's ``kx`` custody
    unseal for purpose ``cert-request`` at epoch 0, with ``object_id`` the 16-byte ``request_id`` passed here; it
    raises ``InvalidTag``, ``ValueError`` or ``CryptoError`` when the blob does not open. Refusals: ``malformed``
    (does not open, not strict JSON, not exactly the seven fields, a bad key, ``device_id`` not of ``dk_sig_pub``,
    invalid scopes, or a clear field of the request that differs from the inner: G3b), ``scope_exceeded`` (a
    ``drop:`` scope: scoped agents enrol only through §6.4)."""
    if type(req) is not CertRequest:
        raise _bad("req")
    try:
        pt = unseal(bytes.fromhex(req.request_id), req.sealed)
        inner = canon.loads_strict(pt) if type(pt) is bytes else None
    except (InvalidTag, ValueError, crypto.CryptoError, canon.JcsError):
        raise Refused("malformed", "cert_request does not open") from None
    if type(inner) is not dict or set(inner) != INNER_FIELDS:
        raise Refused("malformed", "inner field set")
    try:
        sig_pub = _b64_pub(inner["dk_sig_pub"], "dk_sig_pub")
        kx_pub = _b64_pub(inner["dk_kx_pub"], "dk_kx_pub")
        _id(inner["offer_id"], "offer_id")
        _id(inner["attempt"], "attempt")
    except Refused:
        raise Refused("malformed", "inner keys or ids") from None
    scopes = inner["scopes_max"]
    if inner["device_id"] != crypto.hex_id(crypto.device_id(sig_pub)) or not scopes_ok(scopes):
        raise Refused("malformed", "inner device_id or scopes")
    if type(inner["label"]) is not str or len(inner["label"]) > MAX_LABEL:
        raise Refused("malformed", "inner label: a string of at most 80 code points (§8.3)")
    clear = (req.offer_id, req.attempt, req.device_id, req.dk_hash)
    if clear != (inner["offer_id"], inner["attempt"], inner["device_id"], crypto.dk_hash(sig_pub, kx_pub)):
        raise Refused("malformed", "the clear binding differs from the sealed inner")
    if scopes[0].startswith("drop:"):
        raise Refused("scope_exceeded", "a drop: scope")
    return CertInner(
        device_id=inner["device_id"],
        dk_sig_pub=sig_pub,
        dk_kx_pub=kx_pub,
        label=inner["label"],
        scopes_max=tuple(scopes),
        offer_id=inner["offer_id"],
        attempt=inner["attempt"],
    )


# --- 10 (pure half). the primary's challenge and rejection (§8.4 step 3, P3-D11) ------------------------------------


def _request_clear(request_o: object) -> dict[str, Any]:
    """A cert request object with the exact field set and well-formed clear fields (the caller's own, verified)."""
    if type(request_o) is not dict or set(request_o) != REQUEST_FIELDS:
        raise _bad("request field set")
    if (request_o["v"], request_o["suite"], request_o["kind"]) != (2, crypto.SUITE_ID, "cert_request") or any(
        type(request_o[k]) is not int for k in ("v", "suite", "created_ms", "expires_ms")
    ):
        raise _bad("request v, suite, kind or times")
    if type(request_o["sealed"]) is not str:
        raise _bad("request sealed")
    for k in _REQUEST_IDS:
        _id(request_o[k], k)
    try:
        crypto.unb64u(request_o["dk_hash"], 32)
    except crypto.EncodingError:
        raise _bad("dk_hash") from None
    if type(request_o["expires_ms"]) is not int:
        raise _bad("expires_ms")
    return request_o


def make_cert_challenge(
    pk_pub: bytes, sign: Signer, request_o: Mapping[str, Any], inner: CertInner, primary_nonce: bytes
) -> dict[str, Any]:
    """The primary's PK-signed challenge (§8.4 step 3): the request's ids and expiry, the inner's attempt, device and
    both keys, and ``primary_nonce`` (32 CSPRNG bytes the caller drew **after** opening the request). ``sign`` signs
    the exact bytes ``"orch/v2/sig/cert-challenge|" || cj(o)`` with ``PK``. The result is re-checked with
    :func:`check_cert_challenge` under ``pk_pub`` against ``request_o``, so a wrong key or a mismatched inner is
    refused here."""
    req = _request_clear(dict(request_o) if isinstance(request_o, Mapping) else request_o)
    if type(inner) is not CertInner:
        raise _bad("inner")
    o = {
        "v": 2,
        "suite": crypto.SUITE_ID,
        "kind": "cert_challenge",
        "request_id": req["request_id"],
        "workspace_id": req["workspace_id"],
        "person_id": req["person_id"],
        "offer_id": inner.offer_id,
        "attempt": inner.attempt,
        "device_id": inner.device_id,
        "dk_sig_pub": crypto.b64u(inner.dk_sig_pub),
        "dk_kx_pub": crypto.b64u(inner.dk_kx_pub),
        "primary_nonce": crypto.b64u(_raw(primary_nonce, 32, "primary_nonce")),
        "expires_ms": req["expires_ms"],
    }
    pk = _pub(pk_pub, "pk_pub")
    _challenge_body(o, pk, req)  # the binding and PK's person, before any person-key operation (Opus L6)
    signed = _sign(o, sign)
    check_cert_challenge(signed, pk, req)
    return signed


def make_cert_rejection(pk_pub: bytes, sign: Signer, request_o: Mapping[str, Any]) -> dict[str, Any]:
    """The primary's PK-signed rejection (P3-D11, G3b): every field copied from the request it opened. Re-checked
    with :func:`check_cert_rejection`."""
    req = _request_clear(dict(request_o) if isinstance(request_o, Mapping) else request_o)
    o = {
        "v": 2,
        "suite": crypto.SUITE_ID,
        "kind": "cert_rejection",
        **{k: req[k] for k in ("request_id", "workspace_id", "person_id", "offer_id", "attempt", "device_id")},
        "dk_hash": req["dk_hash"],
        "expires_ms": req["expires_ms"],
    }
    pk = _pub(pk_pub, "pk_pub")
    _rejection_body(o, pk, req)  # before any person-key operation (Opus L6)
    signed = _sign(o, sign)
    check_cert_rejection(signed, pk, req)
    return signed


# --- 8c. the host's checks before relaying (11A.15.6 steps 7c and 7d) ------------------------------------------------


@dataclass(frozen=True, slots=True)
class CertChallenge:
    """A challenge verified under ``pk_pub`` and bound to the host's request. ``signed`` is relayed unchanged (only
    this type reaches ``answers.answer_cert_pending``); ``cert_code`` is what both screens show."""

    request_id: str
    device_id: str
    dk_sig_pub: bytes
    dk_kx_pub: bytes
    expires_ms: int
    cert_code: str
    pk_pub: bytes
    signed: dict[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CertRejection:
    """A rejection verified under ``pk_pub`` and bound to the host's request; ``signed`` is relayed unchanged in the
    long ``rejected`` answer (only this type reaches ``answers.answer_rejected_long``)."""

    request_id: str
    device_id: str
    dk_hash: bytes
    expires_ms: int
    pk_pub: bytes
    signed: dict[str, Any] = field(repr=False, compare=False)


def _binding(o: Mapping[str, Any], req: Mapping[str, Any], keys_hash: str) -> None:
    """G3b: the attempt binding (offer, attempt, device, both keys) equals the request's clear one."""
    mine = (o["offer_id"], o["attempt"], o["device_id"], keys_hash)
    if mine != (req["offer_id"], req["attempt"], req["device_id"], req["dk_hash"]):
        raise Refused("not_for_this_device", "not this request's attempt")


def _ids_of(o: Mapping[str, Any], req: Mapping[str, Any]) -> None:
    for key, code in (("person_id", "other_person"), ("workspace_id", "other_workspace")):
        if o[key] != req[key]:
            raise Refused(code, key)
    if o["request_id"] != req["request_id"]:
        raise Refused("not_for_this_device", "request_id")


def _copy(signed: object) -> dict[str, Any]:
    return canon.loads_strict(canon.dumps(signed))


def check_cert_challenge(signed: object, pk_pub: bytes, request_o: Mapping[str, Any]) -> CertChallenge:
    """The relay's G3b checks of a challenge, which the host repeats under the **pinned** ``PK`` against its **own**
    stored request before relaying it (11A.15.6 7c): ``PK``'s signature (``bad_signature``); the exact field set
    (``malformed``); the person, workspace and request (``other_person``, ``other_workspace``,
    ``not_for_this_device``); valid keys, a 32-byte nonce, hex ids, ``device_id`` of ``dk_sig_pub``, the request's
    expiry and ``person_id(PK)`` (``malformed``); then the attempt binding (``not_for_this_device``). The phone runs
    its own §8.4 step 5 checks again."""
    pk = _pub(pk_pub, "pk_pub")
    req = _request_clear(dict(request_o) if isinstance(request_o, Mapping) else request_o)
    o = _verify_signed(pk, signed, "cert_challenge")
    if o is None:
        raise Refused("bad_signature", "cert_challenge")
    sig_pub, kx_pub = _challenge_body(o, pk, req)
    return CertChallenge(
        request_id=o["request_id"],
        device_id=o["device_id"],
        dk_sig_pub=sig_pub,
        dk_kx_pub=kx_pub,
        expires_ms=o["expires_ms"],
        cert_code=cert_code(o),
        pk_pub=pk,
        signed=_copy(signed),
    )


def _challenge_body(o: dict[str, Any], pk: bytes, req: dict[str, Any]) -> tuple[bytes, bytes]:
    """Everything :func:`check_cert_challenge` checks after the signature; also run before signing."""
    if set(o) != CHALLENGE_FIELDS:
        raise Refused("malformed", "cert_challenge field set")
    _ids_of(o, req)
    try:
        sig_pub = _b64_pub(o["dk_sig_pub"], "dk_sig_pub")
        kx_pub = _b64_pub(o["dk_kx_pub"], "dk_kx_pub")
        crypto.unb64u(o["primary_nonce"], 32)
        _id(o["offer_id"], "offer_id")
        _id(o["attempt"], "attempt")
    except (Refused, crypto.EncodingError):
        raise Refused("malformed", "cert_challenge keys, nonce or ids") from None
    if (
        o["device_id"] != crypto.hex_id(crypto.device_id(sig_pub))
        or o["expires_ms"] != req["expires_ms"]
        or type(o["expires_ms"]) is not int
        or o["person_id"] != crypto.hex_id(crypto.person_id(pk))
    ):
        raise Refused("malformed", "cert_challenge device_id, expiry or person")
    _binding(o, req, _dk_hash(sig_pub, kx_pub))
    return sig_pub, kx_pub


def check_cert_rejection(signed: object, pk_pub: bytes, request_o: Mapping[str, Any]) -> CertRejection:
    """The relay's ``verify_cert_rejection`` order, repeated by the host under the pinned ``PK`` against its own
    request before relaying it (11A.15.6 7d): ``PK``'s signature (``bad_signature``); the exact field set and shapes
    (``malformed``); ``person_id(PK)`` and the request's person (``other_person``), workspace (``other_workspace``)
    and request id (``not_for_this_device``); the request's expiry (``malformed``); then the attempt binding
    (``not_for_this_device``)."""
    pk = _pub(pk_pub, "pk_pub")
    req = _request_clear(dict(request_o) if isinstance(request_o, Mapping) else request_o)
    o = _verify_signed(pk, signed, "cert_rejection")
    if o is None:
        raise Refused("bad_signature", "cert_rejection")
    dk_hash = _rejection_body(o, pk, req)
    return CertRejection(
        request_id=o["request_id"],
        device_id=o["device_id"],
        dk_hash=dk_hash,
        expires_ms=o["expires_ms"],
        pk_pub=pk,
        signed=_copy(signed),
    )


def _rejection_body(o: dict[str, Any], pk: bytes, req: dict[str, Any]) -> bytes:
    """Everything :func:`check_cert_rejection` checks after the signature; also run before signing."""
    if set(o) != REJECTION_FIELDS:
        raise Refused("malformed", "cert_rejection field set")
    try:
        for k in ("request_id", "workspace_id", "person_id", "offer_id", "attempt", "device_id"):
            _id(o[k], k)
        dk_hash = crypto.unb64u(o["dk_hash"], 32)
    except (Refused, crypto.EncodingError):
        raise Refused("malformed", "cert_rejection ids or dk_hash") from None
    if type(o["expires_ms"]) is not int:
        raise Refused("malformed", "cert_rejection expires_ms")
    if o["person_id"] != crypto.hex_id(crypto.person_id(pk)):
        raise Refused("other_person", "person_id is not of PK")
    _ids_of(o, req)
    if o["expires_ms"] != req["expires_ms"]:
        raise Refused("malformed", "cert_rejection expiry")
    _binding(o, req, o["dk_hash"])
    return dk_hash
