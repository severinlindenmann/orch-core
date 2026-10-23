"""R6c: pairing, host side (contract 11A.15.6, 11A.15.9A; proto §8.3, §8.4, §8.6).

:class:`PairEngine` is the offer state machine, synchronous and testable without threads or network (as R2's
``Bridge``); :class:`PairThread` runs it on the workspace's **pair thread** ``orch-relay-r6p-<id8>`` (event-driven:
a job queue and a timer, no sleep; every relay call has a 5 s deadline in the transport). The engine is also the
**internal pairing API** of 11A.15.9A (:meth:`PairEngine.offer`, :meth:`answer`, :meth:`cancel`,
:meth:`list_offers`), which the CLI ops and the dashboard wrappers (orch-52) call as jobs on the pair thread, so the
link rules live in one place.

**The record's states** (11A.15.6): ``open → pending → confirmed → cert_pending → allocated → published → approved``
and the terminal ``rejected``, ``expired``, ``cancelled``, ``failed``; a presented certificate (7a) skips
``cert_pending``. Every transition is one atomic write of ``pairing/<offer>.json`` **before** the answer or network
write it enables (:mod:`orch.relay.r6.pairstore`). Every answer is a pure function of the record and its persisted
challenge (:func:`status_answer`, :func:`resend_answer`): a resend or a re-poll gets the same state.

**What the engine never does** (11A.15.6 end): it signs nothing with ``PK`` or a device key (only WSK objects through
``RelaySigner.pair_answer``/``cert_request``); it seals no ``WK`` (publication is the membership thread's, R6b, after
steps 8 and 9); it puts no number in an ``approved`` answer other than the acknowledged publication's; it never
re-signs a request or an answer: it resends stored bytes.

**Seams** (orch.relay never imports orch.host, 11A.2):

* :class:`PairHost`: what only the host can answer (the step-4 reader gate, F1 standing and ``HostPins`` evidence,
  the verified primary, the offer preconditions under the store lock, the accepted card and the held checkpoint).
  The engine never calls it while it holds its own lock (lock order: store lock, then the pairing lock).
* :class:`PairRelay`: the relay routes of pairing (the answer POST, the cert request, ``PUT /v2/devices``, the
  baseline). :class:`SessionPairRelay` is the production adapter over R1's ``Session``.
* :class:`MembershipQueue`: the R6b seam (orch-47). The pair thread **never waits on** the membership thread: it
  posts "publish the addition of D for offer O" and gets ``on_committed``/``on_refused`` back as events. The
  production default while R6b is absent is :class:`UnavailableMembership`: **fail closed**, it answers
  ``refused(publisher_unavailable)`` and reports not ready, so no pairing can complete without R6b.
"""

from __future__ import annotations

import collections
import logging
import queue
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from orch import canon, crypto
from orch.identity import answers, certreq, pairing
from orch.identity.certs import check_cert, verify_cert
from orch.identity.errors import Refused
from orch.identity.pairdevice import PHONE_SCOPES, PairDeviceFacts, PairDeviceInvalid, cert_digest
from orch.wsproto import wire
from orch.wsproto.sign import RelaySigner, SignerRefused
from orch.wsproto.state import StateError

from .pairstore import ANSWER_LIFE_MS, PairStore, PairStoreError

__all__ = [
    "LIVE",
    "TERMINAL",
    "EDGES",
    "GateRefused",
    "IllegalTransition",
    "MembershipEvents",
    "MembershipQueue",
    "OfferHandle",
    "OfferSummary",
    "PairContext",
    "PairEngine",
    "PairHost",
    "PairLinks",
    "PairRefused",
    "PairRelay",
    "PairRelayError",
    "PrimaryInfo",
    "StaleRecord",
    "UnavailableMembership",
    "resend_answer",
    "status_answer",
]

log = logging.getLogger("orch.relay.r6.pair")

TERMINAL = frozenset({"rejected", "expired", "cancelled", "failed"})
LIVE = frozenset({"open", "pending", "confirmed", "cert_pending", "allocated", "published"})
STATES = LIVE | TERMINAL | {"approved"}
OFFER_TTL_MS = 600_000  # 10 minutes, the most an offer lives (11A.15.6 step 1, --ttl)
PHONE_WINDOW_MS = 600_000  # the phone gives up 10 min after its first send (path 7a and the SAS phase, orch-f1 B2)
CHALLENGE_GRACE_MS = 60_000  # path 7b: the challenge's expires_ms + 60 s
STEP9_MARGIN_MS = 120_000  # R6 refuses to enter step 9 within 2 min of the phone's deadline (JUDGEMENT CALL)
TAIL_MS = 600_000  # S is kept until a terminal state plus 10 min (late requests get an answer, not silence)
TOMBSTONE_MS = 7 * 86_400_000  # the record shrinks to a tombstone after 7 days (the baseline life)
WINDOW_MS = 300_000  # ±300 s (proto §8.3 step 3.4)
REFUSALS_PER_MIN = 5  # each offer's own refusal budget (in memory; past it, silence)
SAS_TRIES = 3  # typed SAS attempts per offer (N3c)
REPOSTS_PER_TICK = 4
REPOST_EVERY_MS = 2_000  # a stored answer whose POST failed is posted again at most this often
POLL_EVERY_MS = 10_000  # GET cert-requests/{id} fallback, at most every 10 s
BASELINE_RETRY_MS = 30_000
#: any other failed baseline POST (the relay does not hold the host's held checkpoint yet because the publisher has not
#: uploaded it, or a transient answer such as rate_limited): ask again soon, the phone's deadline is running
BASELINE_SOON_MS = 2_000
OUTSTANDING_GRACE_MS = 60_000
PAIR_SCOPES = PHONE_SCOPES  # look, decide: operate and type are refused for a phone offer in v1.7 (N1)
NEVER_CONNECTED_MS = 86_400_000

# the phone's closed set of refusal codes (orch-f1 S5, proto §8.3, the corpus's pair_* cases): anything else is
# dropped by the phone and shown as "link used by someone else", so every internal condition maps to one of these or
# to the short ``rejected`` answer
REFUSAL_CODES = frozenset(
    {"malformed", "pairing_closed", "bad_signature", "stale_timestamp", "other_person", "cert_invalid", "cert_expired",
     "revoked"}
)  # fmt: skip


class PairRefused(Exception):
    """A refusal of the internal API or the step-9 source, with the host code the wrapper answers (11A.15.9:
    ``relay.pair_open``, ``relay.not_linked``, ``relay.no_primary``, ``relay.quota``, ``relay.pair_closed``,
    ``not_found``, ``invalid.input``, ``workspace.unavailable``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class GateRefused(Exception):
    """The host's step-4 reader gate refused the phone (``orch.host.convenience.pair_gate``'s ``HostError``, mapped
    by the host adapter). ``code`` and ``reason`` are for the host screen only; the phone gets the short
    ``rejected``."""

    def __init__(self, code: str, reason: str = "") -> None:
        super().__init__(code)
        self.code, self.reason = code, reason


class PairRelayError(Exception):
    """A relay call of pairing that did not succeed. ``kind``: ``retry`` (transient: same bytes later),
    ``request_open``, ``replayed``, ``expired``, ``not_found``, ``gone``, ``quota``, ``cert_conflict``, ``conflict``
    (another answer for the rid), ``dropped`` (a 403), ``refused`` (any other definite refusal)."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class PairContext:
    """The workspace's fixed facts: the link origin (byte-equal to the card's ``relay_url``, orch-f1 S7), the own
    WSK public key, the owner's ``PK`` from the genesis delegation (never from the relay) and its person id."""

    workspace_id: str
    origin: str
    wsk_pub: bytes
    owner_pk_pub: bytes

    @property
    def owner_person_id(self) -> str:
        return crypto.hex_id(crypto.person_id(self.owner_pk_pub))


@dataclass(frozen=True)
class PrimaryInfo:
    """The primary as R6 verified it from ``membership/primary.json`` (11A.15.1): its device id and ``dk_kx_pub``
    (taken **only** from its PK-signed certificate) and the human's cleaned display label."""

    device_id: str
    kx_pub: bytes
    label: str


class PairHost(Protocol):
    """The host's answers (``orch.host.relaypair`` implements it). None of these is called under the pairing
    lock."""

    def gate(self, meta: pairing.PairMeta, offer_id: str, who: dict[str, str]) -> tuple[Any, Any]:
        """Step 4: ``(decl, decl_who)`` (both ``None`` for a level-0 phone with no declaration) or
        :class:`GateRefused`."""
        ...

    def revoked(self, device_id: str) -> bool:
        """Whether a revocation or removal of ``device_id`` is known (F1, the relay feed, ``HostPins``)."""
        ...

    def primary(self) -> PrimaryInfo | None:
        """The primary, verified now (``check_cert`` under the genesis ``PK``, the owner's person, not ``drop:``,
        not revoked), or ``None``."""
        ...

    def offer_blocked(self) -> PairRefused | None:
        """Step 1's preconditions under the store lock: linked, a running worker holding a gen, no
        ``membership_quota`` alarm, the device count below the cap."""
        ...

    def card_seq(self) -> int | None:
        """The current accepted card's ``card_seq`` (``head.json``), or ``None``."""
        ...

    def checkpoint(self) -> dict[str, Any] | None:
        """R1's verified held checkpoint ``{n, hash}`` (step 11), or ``None``."""
        ...

    def settle(self, fn: Callable[[Callable[[Mapping[str, Any]], bool]], Any]) -> Any:
        """Run ``fn(landed)`` under the store lock, ``landed(intent)`` telling whether the intent's event is in the
        log (by its id and digest): no submit can be between its precommit and its append meanwhile."""
        ...


class PairRelay(Protocol):
    """The relay routes of pairing. Each raises :class:`PairRelayError` unless the relay answered 2xx."""

    def post_answer(self, offer_id: str, rid: str, envelope: bytes) -> None: ...

    def post_cert_request(self, person_id: str, signed: dict[str, Any]) -> None: ...

    def get_cert_request(self, request_id: str) -> dict[str, Any]: ...

    def put_device(self, device_id: str, cert: dict[str, Any]) -> None: ...

    def post_baseline(self, hash_: str, offer_id: str) -> None: ...


class MembershipEvents(Protocol):
    """What the membership thread calls back (from its own thread): the engine turns each into a pair-thread job."""

    def on_committed(self, offer_id: str, publication: Mapping[str, Any]) -> None:
        """The relay accepted (2xx) the body that adds the offer's device: ``{list_seq, epoch, adds[, list_hash]}``
        of exactly that locally persisted publication."""
        ...

    def on_refused(self, offer_id: str, reason: str) -> None:
        """The addition will not be published (a content refusal, the device excluded, or no publisher)."""
        ...


class MembershipQueue(Protocol):
    """**The R6b seam (orch-47).** The membership thread's queue as the pair thread sees it. It never blocks the
    caller: work is queued and answered later through :class:`MembershipEvents`.

    * :meth:`bind` once, before any post: the engine's events;
    * :meth:`ready`: whether a publisher runs that can add a device now (step 9 refuses otherwise);
    * :meth:`post_addition`: "publish the addition of ``device_id`` (certificate ``cert``) for ``offer_id``" (a
      rotation if the gate is up; R6b reads the scopes from ``devices/<id>.json``). **Idempotent per offer and
      device**: after a crash the pair thread posts it again (it posts, then marks);
    * :meth:`post_exclusion`: "``device_id`` was allocated for ``offer_id`` but the pairing ended" (abandon, the
      deadline, a refused add): R6b puts it in ``excluded`` and the holder invariant rotates it out."""

    def bind(self, events: MembershipEvents) -> None: ...

    def ready(self) -> bool: ...

    def post_addition(self, offer_id: str, device_id: str, cert: dict[str, Any]) -> None: ...

    def post_exclusion(self, offer_id: str, device_id: str, reason: str) -> None: ...


class UnavailableMembership:
    """The production default while R6b is absent: **fail closed**. Never ready; every addition is answered
    ``refused(publisher_unavailable)`` (posted back as an event, never inline: the pair thread never waits on the
    membership thread); exclusions are dropped (nothing was ever published, so nothing holds a key)."""

    REASON = "publisher_unavailable"

    def __init__(self) -> None:
        self._events: MembershipEvents | None = None

    def bind(self, events: MembershipEvents) -> None:
        self._events = events

    def ready(self) -> bool:
        return False

    def post_addition(self, offer_id: str, device_id: str, cert: dict[str, Any]) -> None:
        if self._events is not None:
            self._events.on_refused(offer_id, self.REASON)

    def post_exclusion(self, offer_id: str, device_id: str, reason: str) -> None:
        return None


@dataclass(frozen=True)
class PairLinks:
    """The two links of an offer (11A.15.9): ``native`` (``orch://``, in the QR) and ``https`` (labelled "fallback:
    your relay operator's page can read it"). Never in a ``repr``."""

    native: str = field(repr=False)
    https: str = field(repr=False)


class OfferHandle:
    """What :meth:`PairEngine.offer` returns (11A.15.9A). **The link exists only here**: ``repr``, ``str``, pickling
    and :meth:`public` leave it out; it is derived from ``S`` when the offer is made and never stored."""

    __slots__ = ("_link", "expires_ms", "offer_id", "scopes", "state")

    def __init__(self, offer_id: str, state: str, scopes: tuple[str, ...], expires_ms: int, link: PairLinks):
        self.offer_id, self.state, self.scopes, self.expires_ms = offer_id, state, scopes, expires_ms
        self._link = lambda: link  # a closure: pickling the handle fails (a lambda never pickles)

    @property
    def link(self) -> PairLinks:
        return self._link()

    def public(self) -> dict[str, Any]:
        """The handle without the link (logs, outcome records, ``sub`` items)."""
        return {"offer_id": self.offer_id, "state": self.state, "scopes": list(self.scopes),
                "expires_ms": self.expires_ms}  # fmt: skip

    def __repr__(self) -> str:
        return f"OfferHandle(offer_id={self.offer_id!r}, state={self.state!r}, expires_ms={self.expires_ms})"

    __str__ = __repr__

    def __copy__(self) -> OfferHandle:
        raise TypeError("an OfferHandle is never copied: the link exists once")

    def __deepcopy__(self, memo: Any) -> OfferHandle:
        raise TypeError("an OfferHandle is never copied: the link exists once")


@dataclass(frozen=True)
class OfferSummary:
    """:meth:`PairEngine.list_offers`: no secret (never ``S``, the link, the SAS, ``sas_nonce`` or a challenge)."""

    offer_id: str
    state: str
    scopes: tuple[str, ...]
    expires_ms: int
    label: str | None
    device_id: str | None
    path: str | None
    outstanding_request: str | None
    second_key: bool
    sas_tries_left: int | None
    has_cert: bool = False
    phone_deadline_ms: int | None = None


# -- answers: pure functions of the record (11A.15.6 "Answers per state") ------------------------------------------


def _pub(rec: Mapping[str, Any], name: str) -> bytes:
    return crypto.unb64u(rec["consumed"][name])


def _challenge(rec: Mapping[str, Any], ctx: PairContext) -> certreq.CertChallenge | None:
    signed = rec.get("challenge")
    if signed is None:
        return None
    return certreq.check_cert_challenge(signed, ctx.owner_pk_pub, rec["request"]["signed"]["o"])


def _rejected(rec: Mapping[str, Any], ctx: PairContext) -> dict[str, Any]:
    if rec.get("rejection") is not None:
        rej = certreq.check_cert_rejection(rec["rejection"], ctx.owner_pk_pub, rec["request"]["signed"]["o"])
        return answers.answer_rejected_long(ctx.wsk_pub, rejection=rej)
    return answers.answer_rejected_short(ctx.wsk_pub)


def _pending(rec: Mapping[str, Any], ctx: PairContext) -> dict[str, Any]:
    return answers.answer_pending(
        ctx.wsk_pub, crypto.unb64u(rec["sas_nonce"]), _pub(rec, "dk_sig_pub"), _pub(rec, "dk_kx_pub")
    )


def _cert_pending(rec: Mapping[str, Any], ctx: PairContext) -> dict[str, Any]:
    return answers.answer_cert_pending(
        ctx.wsk_pub, primary_label=rec["primary_label"], pk_pub=ctx.owner_pk_pub, challenge=_challenge(rec, ctx)
    )


def _approved(rec: Mapping[str, Any], ctx: PairContext) -> dict[str, Any]:
    pub = rec["publication"]
    return answers.answer_approved(
        ctx.wsk_pub, scopes=list(rec["scopes"]), cert=rec["cert"], pk_pub=ctx.owner_pk_pub, epoch=pub["epoch"],
        list_seq=pub["list_seq"], card_seq=rec["card_seq"], baseline=rec["baseline"],
    )  # fmt: skip


def resend_answer(rec: Mapping[str, Any], ctx: PairContext) -> dict[str, Any]:
    """An ``op:"pair"`` resend with the consumed keys and ``attempt`` (the table's first column)."""
    s = rec["state"]
    if s in ("pending", "confirmed", "cert_pending", "allocated", "published", "approved"):
        return _pending(rec, ctx)
    if s == "rejected":
        return _rejected(rec, ctx)
    if s == "expired":
        return answers.answer_expired(ctx.wsk_pub)
    return answers.answer_rejected_short(ctx.wsk_pub)  # failed, and cancelled after consumption


def status_answer(rec: Mapping[str, Any], ctx: PairContext, now_ms: int) -> dict[str, Any]:
    """A ``pair_status`` of the consumed device (the table's path 7b and 7a columns). A ``cert_pending`` offer whose
    request ran out with no certificate in hand answers ``expired`` (proto §8.3: "cert_pending ran 10 minutes without
    a certificate"; the corpus case ``pair_status_cert_pending_expired``) even before the tick writes it."""
    s, path = rec["state"], rec.get("path")
    if s in ("pending", "confirmed"):
        return _pending(rec, ctx)
    if s == "cert_pending":
        req = rec.get("request")
        if rec.get("cert") is None and req is not None and now_ms >= int(req["expires_ms"]):
            return answers.answer_expired(ctx.wsk_pub)
        return _cert_pending(rec, ctx)
    if s in ("allocated", "published"):
        return _cert_pending(rec, ctx) if path == "7b" else _pending(rec, ctx)
    if s == "approved":
        return _approved(rec, ctx)
    if s == "rejected":
        return _rejected(rec, ctx)
    if s == "expired":
        return answers.answer_expired(ctx.wsk_pub)
    return answers.answer_rejected_short(ctx.wsk_pub)  # failed, and cancelled after consumption


# -- the engine ----------------------------------------------------------------------------------------------------


def _digest(envelope: bytes) -> str:
    return "sha256:" + crypto.sha256(envelope).hex()


def _hex32(v: object) -> bool:
    return type(v) is str and len(v) == 32 and all(c in "0123456789abcdef" for c in v)


INTENT_FIELDS = frozenset(
    {"offer_id", "attempt", "device", "cert_digest", "scopes", "review_digest", "event_id", "event_digest",
     "decl_digest"}
)  # fmt: skip


class StaleRecord(Exception):  # noqa: N818 - a verdict: the record moved on since it was read
    """The compare-and-set of :meth:`PairEngine._commit` failed: another event wrote the offer first."""


class IllegalTransition(Exception):  # noqa: N818 - a verdict on a transition
    """An edge not in :data:`EDGES`, a guard that does not hold, or a field a state may not change."""


#: the host's settlement of step 9 (Opus review of #588, item 1): with an allocation intent recorded, only a write
#: carrying this token may move the offer (committed -> allocated, refused/not landed -> failed); ``allocated``
#: always needs it. Module-private: the settlement paths are :meth:`PairEngine.committed`, :meth:`refused`,
#: :meth:`recover` and the tick's settlement through the host's store lock.
_SETTLE = object()
#: the settlement edges (each needs ``_SETTLE``)
SETTLEMENT_EDGES = frozenset({("confirmed", "allocated"), ("cert_pending", "allocated"), ("confirmed", "failed"),
                              ("cert_pending", "failed")})  # fmt: skip
_HELD_FIELDS = frozenset({"intent", "intent_ms", "second_key"})

#: every allowed edge of the offer record (11A.15.6); anything else is refused
EDGES: dict[str, frozenset[str]] = {
    "open": frozenset({"pending", "rejected", "expired", "cancelled"}),
    "pending": frozenset({"confirmed", "rejected", "expired", "cancelled"}),
    "confirmed": frozenset({"cert_pending", "allocated", "failed", "expired", "cancelled"}),
    "cert_pending": frozenset({"allocated", "rejected", "failed", "expired", "cancelled"}),
    "allocated": frozenset({"published", "failed", "expired"}),
    "published": frozenset({"approved", "failed", "expired"}),
    "approved": frozenset(),
    **{t: frozenset() for t in ("rejected", "expired", "cancelled", "failed")},
}
#: the fields a same-state write may change, per state
FIELDS: dict[str, frozenset[str]] = {
    "open": frozenset(),
    "pending": frozenset({"sas_tries", "second_key"}),
    "confirmed": frozenset({"cert", "intent", "intent_ms", "second_key"}),
    "cert_pending": frozenset({"request", "challenge", "cert", "intent", "intent_ms", "second_key"}),
    "allocated": frozenset({"device_put", "addition_posted", "publication", "second_key"}),
    "published": frozenset({"second_key"}),
    "approved": frozenset({"second_key"}),
    **{t: frozenset({"secret", "sas_nonce", "second_key"}) for t in ("rejected", "expired", "cancelled", "failed")},
}
#: set once, never changed: what the phone was shown or what the human compared stays what it was
ONCE = ("consumed", "challenge", "cert", "path", "event_id", "publication", "rejection")
_FORWARD = frozenset({"pending", "confirmed", "cert_pending"})


def _guard(frm: str, to: str, new: Mapping[str, Any], now: int) -> None:
    """What each forward edge needs in the record it writes (11A.15.6): the step a state stands for really ran."""
    c = new.get("consumed")
    if to == "pending" and (c is None or c.get("gated") is not True or new.get("phone_deadline_ms") is None):
        raise IllegalTransition("pending needs a consumed request that passed the step-4 gate")
    if to in ("confirmed", "cert_pending", "allocated") and new.get("sas_ok") is not True:
        raise IllegalTransition(f"{to} needs the typed SAS (step 6)")
    if to in _FORWARD and new.get("phone_deadline_ms") is not None and now > int(new["phone_deadline_ms"]):
        raise IllegalTransition(f"{to} after the phone's deadline")
    if to == "cert_pending" and (new.get("path") != "7b" or new.get("request") is None):
        raise IllegalTransition("cert_pending needs a 7b cert request")
    if to == "allocated":
        intent = new.get("intent")
        if new.get("cert") is None or intent is None or new.get("event_id") != intent.get("event_id"):
            raise IllegalTransition("allocated needs the certificate and the intent's event")
    if to == "published" and (new.get("publication") is None or new.get("baseline") is None or
                              new.get("card_seq") is None):  # fmt: skip
        raise IllegalTransition("published needs the acknowledged publication, the card and the baseline")
    if to == "approved" and (new.get("publication") is None or new.get("baseline") is None):
        raise IllegalTransition("approved needs the publication and the baseline")
    if to == "rejected" and frm == "open" and (c is None or c.get("gated") is not False):
        raise IllegalTransition("open -> rejected only for a request the step-4 gate refused")


def _intent(alloc: Mapping[str, Any]) -> dict[str, Any]:
    """The allocation intent as stored (11A.15.6 step 9 (ii)): exactly the ``PairAllocation`` fields, JSON-shaped."""
    if not isinstance(alloc, Mapping) or set(alloc) != INTENT_FIELDS:
        raise PairRefused("relay.pair_closed", "an allocation of another shape")
    out = dict(alloc)
    out["scopes"] = list(out["scopes"])
    return canon.loads_strict(canon.dumps(out))


class PairEngine:
    """The offer state machine of one workspace (see the module docstring). Thread rules: everything but the step-9
    hooks (:meth:`precommit`, :meth:`committed`, :meth:`refused`, run by the host under the store lock) and
    :meth:`facts` runs on the pair thread. ``post(fn)`` queues ``fn`` as a later pair-thread job (membership events,
    step 10 after an allocation)."""

    def __init__(
        self,
        ctx: PairContext,
        store: PairStore,
        signer: RelaySigner,
        host: PairHost,
        relay: PairRelay,
        membership: MembershipQueue,
        *,
        now_ms: Callable[[], int],
        post: Callable[[Callable[[], None]], None],
        draw: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self.ctx, self.store, self.signer, self.host, self.routes = ctx, store, signer, host, relay
        self.membership = membership
        self.now_ms, self.post, self._draw = now_ms, post, draw
        self._budget: dict[str, collections.deque[int]] = {}
        self._last_poll: dict[str, int] = {}
        self._reposted: dict[tuple[str, str, str], int] = {}
        self._retry_at: dict[str, int] = {}
        self.dropped = 0  # silent drops (unknown offer, bad tag, budget, overflow), counted (alarm pairing_dropped)
        self._dropped_flushed = 0
        self._dropped_at = -(10**15)
        self._req_digest = ""  # the request being answered (receive() runs one at a time, on the pair thread)
        membership.bind(_Events(self))

    # -- small helpers ---------------------------------------------------------------------------------------------

    def _ws(self) -> bytes:
        return bytes.fromhex(self.ctx.workspace_id)

    def _alarm(self, code: str, detail: str = "") -> None:
        with self.store.lock:
            obj = self.store.alarms()
            a = obj["alarms"].setdefault(code, {"count": 0, "at_ms": 0, "detail": ""})
            a["count"] = int(a.get("count", 0)) + 1
            a["at_ms"] = self.now_ms()
            a["detail"] = detail[:200]
            self.store.write_alarms(obj)
        log.warning("pairing alarm %s", code)

    def _need(self, offer_id: str, text: str) -> None:
        with self.store.lock:
            obj = self.store.alarms()
            if not any(n.get("offer_id") == offer_id and n.get("text") == text for n in obj["needs"]):
                obj["needs"].append({"offer_id": offer_id, "text": text[:300], "at_ms": self.now_ms()})
                self.store.write_alarms(obj)

    def is_live(self, offer_id: str) -> bool:
        """The signer's ``pair_live`` callback: a non-terminal offer, or a terminal one within its 10-minute tail
        that still holds ``S``."""
        try:
            rec = self._get(offer_id)
        except PairStoreError:
            return False
        if rec is None or rec.get("tombstone") or rec.get("secret") is None:
            return False
        if rec["state"] in TERMINAL:
            return self.now_ms() <= int(rec.get("terminal_ms") or 0) + TAIL_MS
        return True

    def binding_held(self, offer_id: str, attempt: str, device_id: str, dk_hash: str) -> bool:
        """The signer's ``pair_binding`` callback (11A.15.7 ``cert_request``): the offer is confirmed on path 7b,
        consumed by exactly this attempt, device and key pair."""
        try:
            rec = self._get(offer_id)
        except PairStoreError:
            return False
        c = (rec or {}).get("consumed")
        if rec is None or c is None or rec["state"] not in ("confirmed", "cert_pending") or rec.get("path") != "7b":
            return False
        mine = crypto.b64u(crypto.dk_hash(crypto.unb64u(c["dk_sig_pub"]), crypto.unb64u(c["dk_kx_pub"])))
        return (c["attempt"], c["device_id"], mine) == (attempt, device_id, dk_hash)

    def _commit(
        self, rec: Mapping[str, Any], to: str | None = None, *, settle: object = None, **changes: Any
    ) -> dict[str, Any]:
        """**The one durable write of an offer record** (every transition and every field update goes through it).

        Under the pairing lock: the record is re-read and must still carry the version ``rec`` was read at
        (compare-and-set: two events on one offer, a relay answer and a deadline, a pair-thread job and the host's
        step-9 hook, can never overwrite each other; the loser gets :class:`StaleRecord` and re-reads). ``to``, if
        given and not the current state, must be an edge of :data:`EDGES` and pass :func:`_guard` on the resulting
        record (no state is skipped: no ``pending`` without the step-4 gate, no ``confirmed`` without the typed
        SAS, no ``allocated`` without a certificate and an intent, nothing forward past the phone's deadline). A
        same-state write may change only the fields :data:`FIELDS` lists for that state, and never a field
        :data:`ONCE` holds once set. The new version is the old plus one.

        **Durable first** (no state drift): the engine holds no offer state in memory; the record returned is the
        one on disk. A write that fails raises out of here before anything acts on the new state (no answer, no
        POST, no hook), and the offer stays in its last durable state at its old version."""
        with self.store.lock:
            cur = self._get(rec["offer_id"])
            if cur is None or cur.get("tombstone") or cur.get("ver", 0) != rec.get("ver", 0):
                raise StaleRecord(rec["offer_id"])
            frm = cur["state"]
            new = {**cur, **changes}
            moving = to is not None and to != frm
            # step 9 is exclusive (Opus review of #588, item 1): once the allocation intent is recorded, no tick,
            # cancel, relay answer or deadline may end or change the offer; only the host's settlement can
            held = cur.get("intent") is not None and frm in ("confirmed", "cert_pending")
            if moving and to == "allocated" and settle is not _SETTLE:
                raise IllegalTransition("allocated only by the host's settlement of step 9")
            if held and moving and (settle is not _SETTLE or (frm, to) not in SETTLEMENT_EDGES):
                raise IllegalTransition(f"{frm} -> {to}: the allocation intent is recorded; only the host settles it")
            if held and not moving and set(changes) - _HELD_FIELDS:
                raise IllegalTransition("the allocation intent is recorded: the offer's fields stay as reviewed")
            if moving:
                if to not in EDGES.get(frm, ()):
                    raise IllegalTransition(f"{frm} -> {to}")
                new["state"] = to
                if to in TERMINAL:
                    new["terminal_ms"] = self.now_ms()
                _guard(frm, to, new, self.now_ms())
            else:
                extra = set(changes) - FIELDS.get(frm, frozenset())
                if extra:
                    raise IllegalTransition(f"{frm}: {sorted(extra)} may not change")
                if frm in TERMINAL and any(changes.get(k) is not None for k in ("secret", "sas_nonce")):
                    raise IllegalTransition("a terminal record only drops its secrets")
            for k in ONCE:
                if cur.get(k) is not None and new.get(k) != cur.get(k):
                    raise IllegalTransition(f"{k} is set once")
            new["ver"] = int(cur.get("ver", 0)) + 1
            self.store.put(new)
            return new

    def _get(self, offer_id: str) -> dict[str, Any] | None:
        """The durable record, the only source of an offer's state (the engine keeps no offer state in memory: every
        decision re-reads it, and :meth:`_commit` writes before it returns). A record whose state is not one of
        :data:`STATES`, or whose version is not an integer, is refused like an unreadable one (PairStoreError): fail
        closed, never a permissive default."""
        rec = self.store.get(offer_id)
        if rec is None or rec.get("tombstone"):
            return rec
        if rec.get("state") not in STATES or type(rec.get("ver", 0)) is not int:
            raise PairStoreError("offer record of an unknown state or version")
        return rec

    def quarantine(self) -> list[str]:
        """At open, before either R6 thread starts: every unreadable, unknown or corrupt offer record becomes a
        terminal ``failed`` record with no secret (fail closed: never ``open``, never a live state), alarmed
        ``pairing_state_corrupt`` with a ``needs`` item. An outstanding request that is unreadable is cleared (the
        relay's own one-request rule and its expiry still hold). Returns the offers quarantined."""
        out = []
        for oid in self.store.offer_ids():
            try:
                self._get(oid)
                continue
            except PairStoreError:
                pass
            with self.store.lock:
                self.store.put({
                    "v": 1, "offer_id": oid, "state": "failed", "reason": "corrupt", "secret": None,
                    "sas_nonce": None, "consumed": None, "path": None, "terminal_ms": self.now_ms(), "corrupt": True,
                    "scopes": [], "expires_ms": 0, "sas_ok": False, "ver": 1 << 20,
                })  # fmt: skip
            self._alarm("pairing_state_corrupt", oid)
            self._need(oid, "a pairing record was unreadable and was ended: if a device was being added, remove it")
            out.append(oid)
        try:
            self.store.outstanding()
        except PairStoreError:
            with self.store.lock:
                self.store.clear_outstanding_unchecked()
            self._alarm("pairing_state_corrupt", "outstanding")
        return out

    def _move(self, rec: Mapping[str, Any], to: str | None, **changes: Any) -> dict[str, Any]:
        return self._commit(rec, to, **changes)

    def _settle_move(self, rec: Mapping[str, Any], to: str, **changes: Any) -> dict[str, Any]:
        return self._commit(rec, to, settle=_SETTLE, **changes)

    def _live_offer(self) -> str | None:
        """The live offer or the outstanding request that blocks a new offer (Q6), or ``None``."""
        for oid in self.store.offer_ids():
            try:
                rec = self._get(oid)
            except PairStoreError:
                return oid  # an unreadable record blocks (fail closed)
            if rec is not None and rec["state"] in LIVE:
                return oid
        out = self.store.outstanding()
        if out is not None and self.now_ms() <= int(out["expires_ms"]) + OUTSTANDING_GRACE_MS:
            return out["offer_id"]
        return None

    # -- 11A.15.9A: the internal API -------------------------------------------------------------------------------

    def offer(
        self, scopes: tuple[str, ...] | list[str] = ("look", "decide"), ttl_ms: int = OFFER_TTL_MS
    ) -> OfferHandle:
        """Steps 1 and 2: the preconditions, then the ``open`` record (the pairing reservation of 11A.6) and the
        links, derived here from ``S`` and returned only in the handle."""
        scopes = tuple(scopes) if type(scopes) in (list, tuple) else ()
        if (
            not scopes
            or not all(type(s) is str for s in scopes)
            or list(scopes) != [s for s in PAIR_SCOPES if s in scopes]
            or "decide" not in scopes  # the phone ceiling is look+decide; answers need decide (Opus #588 item 5)
        ):
            raise PairRefused("invalid.input", "scopes: a non-empty subset of look, decide (operate and type are "
                                               "refused for a phone in v1.7)")  # fmt: skip
        if type(ttl_ms) is not int or not 60_000 <= ttl_ms <= OFFER_TTL_MS:
            raise PairRefused("invalid.input", "ttl: between 1 and 10 minutes")
        blocked = self.host.offer_blocked()  # under the store lock, never under ours
        if blocked is not None:
            raise blocked
        if self.host.primary() is None:
            raise PairRefused("relay.no_primary", "no verified primary: run orch host relay primary set")
        with self.store.lock:
            if self._live_offer() is not None:
                raise PairRefused("relay.pair_open", "a live offer or an outstanding cert request exists: cancel it "
                                                     "or wait for it to end")  # fmt: skip
            offer_id = self._draw(16).hex()
            s, nonce = self._draw(32), self._draw(32)
            now = self.now_ms()
            rec = {
                "v": 1, "offer_id": offer_id, "state": "open", "secret": crypto.b64u(s),
                "sas_nonce": crypto.b64u(nonce), "scopes": list(scopes), "created_ms": now, "expires_ms": now + ttl_ms,
                "phone_deadline_ms": None, "consumed": None, "path": None, "sas_tries": 0, "request": None,
                "challenge": None, "rejection": None, "cert": None, "intent": None, "event_id": None,
                "publication": None, "card_seq": None, "baseline": None, "terminal_ms": None, "second_key": False,
                "primary_label": None, "reason": None, "device_put": False, "addition_posted": False,
                "sas_ok": False, "ver": 0,
            }  # fmt: skip
            self.store.put(rec)  # the link is shown only after this write returns
        https, native = pairing.pair_link(self.ctx.origin, self.ctx.workspace_id, offer_id, s, self.ctx.wsk_pub,
                                          self.ctx.owner_pk_pub)  # fmt: skip
        return OfferHandle(offer_id, "open", scopes, rec["expires_ms"], PairLinks(native=native, https=https))

    def answer(self, offer_id: str, typed_code: str, approve: bool) -> tuple[str, int]:
        return self._api(self._answer_sas, offer_id, typed_code, approve)

    def _api(self, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        """The internal API's calls: a compare-and-set loss or a refused transition is a refusal, never a crash."""
        try:
            return fn(*args, **kw)
        except StaleRecord:
            raise PairRefused("relay.pair_closed", "the offer changed meanwhile: look at it again") from None
        except IllegalTransition as e:
            raise PairRefused("relay.pair_closed", f"not allowed now: {e}") from None

    def _answer_sas(self, offer_id: str, typed_code: str, approve: bool) -> tuple[str, int]:
        """Step 6: approve only when ``typed_code`` (case-folded) equals the SAS; a wrong code counts one of the 3
        attempts; empty or ``approve=False`` rejects at once. Returns ``(state, attempts left)``. The typed code is
        never stored or logged."""
        rec = self._get_or_refuse(offer_id)
        if rec["state"] != "pending":
            raise PairRefused("relay.pair_closed", f"the offer is {rec['state']}, not waiting for the code")
        if self.now_ms() > int(rec["phone_deadline_ms"]):
            rec = self._move(rec, "expired", reason="phone_deadline")
            return rec["state"], 0
        code = typed_code.strip().upper() if type(typed_code) is str else ""
        if approve is not True or not code:
            rec = self._move(rec, "rejected", reason="human")
            return rec["state"], 0
        c = rec["consumed"]
        want = pairing.sas(self.ctx.workspace_id, offer_id, _pub(rec, "dk_sig_pub"), _pub(rec, "dk_kx_pub"),
                           self.ctx.wsk_pub, crypto.unb64u(rec["sas_nonce"]))  # fmt: skip
        if not wire.ct_equal(code.encode(), want.encode()):
            tries = int(rec["sas_tries"]) + 1
            if tries >= SAS_TRIES:
                rec = self._move(rec, "rejected", sas_tries=tries, reason="sas_mismatch")
                return rec["state"], 0
            self._move(rec, None, sas_tries=tries)
            return "pending", SAS_TRIES - tries
        rec = self._move(rec, "confirmed", sas_ok=True)
        if c.get("cert") is not None:  # 7a: the presented certificate, step 8
            rec = self._cert_in_hand(rec, c["cert"])
        else:  # 7b: the cert request
            rec = self._request_cert(rec)
        return rec["state"], SAS_TRIES - int(rec["sas_tries"])

    def cancel(self, offer_id: str) -> str:
        """11A.15.6 "Cancellation": any state before ``allocated``; later ``relay.pair_closed`` (use abandon, or
        remove the device). An outstanding cert request stays tracked."""
        return self._api(self._cancel, offer_id)

    def _cancel(self, offer_id: str) -> str:
        rec = self._get_or_refuse(offer_id)
        if rec["state"] in ("allocated", "published", "approved"):
            raise PairRefused("relay.pair_closed", "the device is allocated: use abandon, or remove the device")
        if rec.get("intent") is not None and rec["state"] not in TERMINAL:
            raise PairRefused("relay.pair_closed", "the allocation is being recorded: it settles on its own")
        if rec["state"] in TERMINAL:
            return rec["state"]
        return self._move(rec, "cancelled", reason="cancelled", cancelled_from=rec["state"])["state"]

    def abandon(self, offer_id: str) -> str:
        """11A.15.6 "Ending a stuck offer": an ``allocated`` or ``published`` offer ends ``failed``; the device
        enters ``excluded`` (R6b) and the ``needs`` item asks the human to remove it."""
        return self._api(self._abandon, offer_id)

    def _abandon(self, offer_id: str) -> str:
        rec = self._get_or_refuse(offer_id)
        if rec["state"] not in ("allocated", "published"):
            raise PairRefused("relay.pair_closed", f"only an allocated or published offer is abandoned, not "
                                                   f"{rec['state']}")  # fmt: skip
        rec = self._move(rec, "failed", reason="abandoned")
        self._exclude(rec, "abandoned")
        return rec["state"]

    def list_offers(self) -> list[OfferSummary]:
        out = self.store.outstanding()
        rows = []
        for oid in self.store.offer_ids():
            try:
                rec = self._get(oid)
            except PairStoreError:
                continue
            if rec is None:
                continue
            c = rec.get("consumed") or {}
            rows.append(
                OfferSummary(
                    offer_id=oid,
                    state=rec["state"],
                    scopes=tuple(rec.get("scopes") or ()),
                    expires_ms=int(rec.get("expires_ms") or 0),
                    label=c.get("label"),
                    device_id=c.get("device_id"),
                    path=rec.get("path"),
                    outstanding_request=out["request_id"] if out is not None and out["offer_id"] == oid else None,
                    second_key=bool(rec.get("second_key")),
                    sas_tries_left=SAS_TRIES - int(rec.get("sas_tries") or 0) if rec["state"] == "pending" else None,
                    has_cert=rec.get("cert") is not None,
                    phone_deadline_ms=rec.get("phone_deadline_ms"),
                )  # fmt: skip
            )
        return rows

    def show(self, offer_id: str) -> OfferSummary:
        """One offer's summary (the attached CLI's view): no secret, as :meth:`list_offers`."""
        self._get_or_refuse(offer_id)
        row = next((r for r in self.list_offers() if r.offer_id == offer_id), None)
        if row is None:
            raise PairRefused("not_found", "no such offer")
        return row

    def api_table(self) -> dict[str, Callable[..., Any]]:
        """**The internal API (11A.15.9A), and nothing else.** The one table every outside caller goes through
        (:meth:`PairThread.api`, :func:`run_api`): the host's settlement paths (``settle``, ``committed``,
        ``refused``, ``recover``, ``adopt_existing``, ``precommit``) are never in it (Opus review of #588)."""
        table = {
            "offer": self.offer, "answer": self.answer, "cancel": self.cancel, "abandon": self.abandon,
            "list_offers": self.list_offers, "pair_cert": self.pair_cert, "show": self.show,
        }  # fmt: skip
        assert frozenset(table) == API_METHODS
        return table

    def pair_cert(self, offer_id: str, *, cert: Any = None, rejection: Any = None, challenge: Any = None) -> str:
        """``relay.pair_cert`` (Opus L4): the inline primary hands the certificate it signed, or its rejection
        (and the challenge it posted, so ``cert_code`` is known even if the relay never served it). R6 verifies
        each itself (step 8, 7d)."""
        return self._api(self._pair_cert, offer_id, cert=cert, rejection=rejection, challenge=challenge)

    def _pair_cert(self, offer_id: str, *, cert: Any, rejection: Any, challenge: Any) -> str:
        rec = self._get_or_refuse(offer_id)
        if rec["state"] != "cert_pending" or rec.get("request") is None:
            raise PairRefused("relay.pair_closed", f"the offer is {rec['state']}, not waiting for a certificate")
        if challenge is not None:
            rec = self._store_challenge(rec, challenge)
        if rejection is not None:
            return self._rejection_in_hand(rec, rejection)["state"]
        if cert is not None:
            return self._cert_in_hand(rec, cert)["state"]
        return rec["state"]

    def _get_or_refuse(self, offer_id: str) -> dict[str, Any]:
        if not _hex32(offer_id):
            raise PairRefused("invalid.input", "offer: 32 lower-case hex")
        try:
            rec = self._get(offer_id)
        except PairStoreError as e:
            raise PairRefused("workspace.unavailable", str(e)) from None
        if rec is None or rec.get("tombstone"):
            raise PairRefused("not_found", "no such offer")
        return rec

    # -- the pair request (steps 3 to 5; §8.6 steps 1 to 3) --------------------------------------------------------

    def receive(self, envelope: bytes, rid: str, device: str | None = None) -> str:
        """One ``route:"pair"`` poll item. Returns what happened (``drop:<why>``, ``replay``, ``refuse:<code>``,
        ``answer:<state>``); every outcome is durable before this returns, so the item may be acknowledged. A record
        another event wrote meanwhile (compare-and-set) is a silent drop: the device resends."""
        try:
            return self._receive(envelope, rid, device)
        except StaleRecord:
            return self._drop("stale")
        except IllegalTransition as e:
            log.error("pairing: refused transition on a pair request: %s", e)
            return self._drop("transition")

    def _receive(self, envelope: bytes, rid: str, device: str | None) -> str:
        try:
            h, body, sig = wire.parse_envelope(envelope, direction=1)
        except wire.WireError:
            return self._drop("shape")
        hb = envelope[: wire.HEADER_LEN]
        if h.workspace != self._ws() or h.rid.hex() != rid or (device and h.device.hex() != device):
            return self._drop("shape")
        if h.epoch != 0:
            return self._drop("not_pairing")
        offer_id = h.stream.hex()
        try:
            rec = self._get(offer_id)
        except (PairStoreError, ValueError):
            return self._drop("no_key")
        if rec is None or rec.get("tombstone") or rec.get("secret") is None:
            return self._drop("no_key")
        now = self.now_ms()
        if rec["state"] in TERMINAL and now > int(rec.get("terminal_ms") or 0) + TAIL_MS:
            return self._drop("no_key")  # past its tail: silence
        try:
            k = wire.k_pair(crypto.unb64u(rec["secret"]), h.workspace, h.stream)
            pt = wire.open_body(k, hb, body)
        except (wire.WireError, crypto.EncodingError):
            return self._drop("tag")
        stored = self.store.answer_for(offer_id, h.device.hex(), rid)
        if stored is not None:
            if stored.get("digest") != _digest(envelope):
                return self._drop("rid_conflict")  # other bytes under an answered rid: the relay keeps one answer
            # an identical resend: byte for byte (Opus L1)
            self._post(offer_id, rid, crypto.unb64u(stored["env"]), h.device.hex())
            return "replay"
        self._req_digest = _digest(envelope)
        try:
            meta, _ = wire.parse_plaintext(pt)
            op = meta.get("op")
        except wire.WireError:
            return self._refuse(rec, h, "malformed")
        if op == "pair_status":
            return self._pair_status(rec, h, hb, body, sig, now)
        if op != "pair":
            return self._refuse(rec, h, "malformed")
        return self._pair(rec, h, hb, body, sig, meta, now)

    def _drop(self, why: str) -> str:
        self.dropped += 1
        return f"drop:{why}"

    def _pair_status(self, rec: dict[str, Any], h: wire.Header, hb: bytes, body: bytes, sig: bytes, now: int) -> str:
        c = rec.get("consumed")
        if c is None or c["device_id"] != h.device.hex():
            return self._refuse(rec, h, "pairing_closed")
        if not crypto.verify(crypto.unb64u(c["dk_sig_pub"]), sig, wire.bridge_signed_bytes(hb, body)):
            return self._refuse(rec, h, "bad_signature")
        if abs(now - h.ts_ms) > WINDOW_MS:
            return self._refuse(rec, h, "stale_timestamp", host_ms=now)
        return self._answer(rec, h, status_answer(rec, self.ctx, now))

    def _pair(self, rec, h, hb, body, sig, meta, now) -> str:  # noqa: ANN001 - the receive() locals
        try:
            pm = pairing.check_pair_meta(meta)
        except Refused:
            return self._refuse(rec, h, "malformed")
        c = rec.get("consumed")
        resend = (
            c is not None
            and c["dk_sig_pub"] == crypto.b64u(pm.dk_sig_pub)
            and c["dk_kx_pub"] == crypto.b64u(pm.dk_kx_pub)
            and c["attempt"] == pm.attempt
        )
        if not resend:
            if rec["state"] == "cancelled" and c is None:
                return self._answer(rec, h, answers.answer_expired(self.ctx.wsk_pub))  # orch-f1 S6
            if rec["state"] != "open" or now >= int(rec["expires_ms"]):
                if c is not None and not rec.get("second_key"):  # a second tag-valid key used the offer (Opus L10)
                    rec = self._move(rec, None, second_key=True)
                    self._alarm("pairing_second_key", rec["offer_id"])
                return self._refuse(rec, h, "pairing_closed")
        if crypto.device_id(pm.dk_sig_pub) != h.device or not crypto.verify(
            pm.dk_sig_pub, sig, wire.bridge_signed_bytes(hb, body)
        ):
            return self._refuse(rec, h, "bad_signature")
        if abs(now - h.ts_ms) > WINDOW_MS:
            return self._refuse(rec, h, "stale_timestamp", host_ms=now)
        if resend:
            return self._answer(rec, h, resend_answer(rec, self.ctx))
        path = "7b"
        who = {"pk_pin": crypto.b64u(crypto.pk_pin(self.ctx.owner_pk_pub))}
        if pm.cert is not None:  # step 3.5: a presented certificate
            # the protocol's order (proto §8.3 step 3.5, corpus pair_with_another_persons_cert): a certificate naming
            # another person is refused other_person before its signature is checked. That unverified field only
            # selects the refusal code; nothing is accepted before verify_cert under the pinned owner PK below.
            o = pm.cert.get("o") if type(pm.cert) is dict else None
            if type(o) is dict and o.get("person_id") != self.ctx.owner_person_id:
                return self._refuse(rec, h, "other_person")
            try:
                o = verify_cert(pm.cert, self.ctx.owner_pk_pub, now)
            except Refused as e:
                return self._refuse(rec, h, e.code if e.code in REFUSAL_CODES else "cert_invalid")
            if self.host.revoked(o["device_id"]):
                return self._refuse(rec, h, "revoked")
            if o["dk_sig_pub"] != crypto.b64u(pm.dk_sig_pub) or o["dk_kx_pub"] != crypto.b64u(pm.dk_kx_pub):
                return self._refuse(rec, h, "cert_invalid")
            path, who = "7a", {"person_id": o["person_id"]}
        consumed = {
            "device_id": pm.device_id, "dk_sig_pub": crypto.b64u(pm.dk_sig_pub),
            "dk_kx_pub": crypto.b64u(pm.dk_kx_pub), "attempt": pm.attempt, "label": pm.label, "cert": pm.cert,
            "decl": None, "decl_who": None, "gated": False,
        }  # fmt: skip
        # step 4: the reader gate (H12d-3), before the offer is consumed, the SAS shown or the primary asked; never
        # under the pairing lock (it takes the store lock)
        try:
            decl, decl_who = self.host.gate(pm, rec["offer_id"], who)
        except GateRefused as e:
            try:  # compare-and-set: the offer must still be the open one this request found
                rec = self._move(rec, "rejected", consumed=consumed, path=path, reason=f"reader_gate:{e.code}",
                                 phone_deadline_ms=h.ts_ms + PHONE_WINDOW_MS)  # fmt: skip
            except (StaleRecord, IllegalTransition):
                return self._refuse(rec, h, "pairing_closed")
            return self._answer(rec, h, answers.answer_rejected_short(self.ctx.wsk_pub))
        consumed["decl"], consumed["decl_who"], consumed["gated"] = decl, decl_who, True
        # step 5: consume, durably, before the pending answer (compare-and-set on the open record)
        try:
            rec = self._move(rec, "pending", consumed=consumed, path=path,
                             phone_deadline_ms=h.ts_ms + PHONE_WINDOW_MS)  # fmt: skip
        except (StaleRecord, IllegalTransition):
            return self._refuse(rec, h, "pairing_closed")
        return self._answer(rec, h, resend_answer(rec, self.ctx))

    # -- answers on the wire ---------------------------------------------------------------------------------------

    def _refuse(self, rec: dict[str, Any], h: wire.Header, code: str, **extra: Any) -> str:
        """A refusal, sealed and signed, within the offer's budget of 5 per minute (past it: silence)."""
        assert code in REFUSAL_CODES, code
        now = self.now_ms()
        q = self._budget.setdefault(rec["offer_id"], collections.deque())
        while q and now - q[0] >= 60_000:
            q.popleft()
        if len(q) >= REFUSALS_PER_MIN:
            return self._drop("budget")
        q.append(now)
        meta = {"refusal": code, "wsk_pub": crypto.b64u(self.ctx.wsk_pub), **extra}
        self._send(rec, h, meta, refusal=True)
        return f"refuse:{code}"

    def _answer(self, rec: dict[str, Any], h: wire.Header, meta: dict[str, Any]) -> str:
        self._send(rec, h, meta, refusal=False)
        return f"answer:{meta['state']}"

    def _send(self, rec: dict[str, Any], h: wire.Header, meta: dict[str, Any], *, refusal: bool) -> None:
        flags = wire.FLAG_LAST | (wire.FLAG_REFUSAL if refusal else 0)
        hdr = wire.Header(2, 2, flags, crypto.SUITE_ID, h.workspace, h.device, h.rid, h.stream, 0, self.now_ms(), 0,
                          self._draw(16)).pack()  # fmt: skip
        k = wire.k_pair(crypto.unb64u(rec["secret"]), h.workspace, h.stream)
        body = wire.seal_body(k, hdr, wire.build_plaintext(meta, b""))
        try:
            sig = self.signer.pair_answer(hdr, body)
        except SignerRefused:
            log.warning("pairing: the signer refused an answer (offer not live)")
            return
        env = wire.build_envelope(hdr, body, sig)
        rid, dev = h.rid.hex(), h.device.hex()
        try:  # durable before the POST
            self.store.add_answer(rec["offer_id"], dev, rid, self._req_digest, self.now_ms(), crypto.b64u(env))
        except PairStoreError:
            stored = self.store.answer_for(rec["offer_id"], dev, rid)
            if stored is None:
                raise
            env = crypto.unb64u(stored["env"])
        self._post(rec["offer_id"], rid, env, dev)

    def _post(self, offer_id: str, rid: str, env: bytes, device: str | None = None) -> bool:
        """POST an answer; False only for a transient failure (it stays unposted, and the tick posts the stored bytes
        again, :meth:`_repost`; a phone resend replays the same bytes too)."""
        key = (offer_id, device or "", rid)
        try:
            self.routes.post_answer(offer_id, rid, env)
        except PairRelayError as e:
            log.info("pairing: answer POST %s", e.kind)
            if e.kind == "retry":
                self._reposted[key] = self.now_ms()
                return False
            # refused for good: never retried
        if device is not None:
            self.store.mark_answer_posted(offer_id, device, rid)  # may raise: the throttle key stays then
        self._reposted.pop(key, None)
        return True

    def _repost(self, now: int) -> None:
        """Re-POST stored answers whose POST failed transiently, until the relay takes them or their rid lifetime
        (:data:`ANSWER_LIFE_MS`) ends: the phone's resend is not what an answer depends on. At most
        :data:`REPOSTS_PER_TICK` per tick, and none after the first transient failure of a tick (the relay is
        failing: the pair thread's other work, SAS, cancel and approve, is not made to wait behind more POSTs)."""
        done = 0
        for oid in self.store.offer_ids():
            try:
                pending = [a for a in self.store.answers(oid) if a.get("posted") is False]
            except PairStoreError:
                continue
            for a in pending:
                key = (oid, a["device"], a["rid"])
                if now - int(a.get("at_ms", 0)) >= ANSWER_LIFE_MS:
                    self._reposted.pop(key, None)
                    continue
                if now - self._reposted.get(key, 0) < REPOST_EVERY_MS:
                    continue
                if done >= REPOSTS_PER_TICK:
                    return
                self._reposted[key] = now  # before the POST: whatever happens, the next try is REPOST_EVERY_MS away
                done += 1
                if not self._post(oid, a["rid"], crypto.unb64u(a["env"]), a["device"]):
                    return

    # -- 7b: the cert request; 7c: the challenge; 7d: the cert or the rejection ------------------------------------

    def _request_cert(self, rec: dict[str, Any]) -> dict[str, Any]:
        primary = self.host.primary()
        if primary is None:
            self._alarm("pairing_no_primary", rec["offer_id"])
            return self._move(rec, "failed", reason="no_primary")
        c = rec["consumed"]
        now = self.now_ms()
        o = certreq.cert_request_o(
            request_id=self._draw(16).hex(), workspace_id=self.ctx.workspace_id,
            person_id=self.ctx.owner_person_id, primary_device_id=primary.device_id, primary_kx_pub=primary.kx_pub,
            offer_id=rec["offer_id"], attempt=c["attempt"], dk_sig_pub=crypto.unb64u(c["dk_sig_pub"]),
            dk_kx_pub=crypto.unb64u(c["dk_kx_pub"]), label=c["label"], scopes_max=list(rec["scopes"]),
            created_ms=now,
        )  # fmt: skip
        try:
            sig = self.signer.cert_request(o)
        except SignerRefused as e:
            log.warning("pairing: the signer refused the cert request: %s", e)
            return self._move(rec, "failed", reason="signer")
        signed = {"o": o, "sig": sig}
        deadline = min(int(rec["phone_deadline_ms"]), o["expires_ms"] + CHALLENGE_GRACE_MS)
        with self.store.lock:  # the record and outstanding.json, both before the POST
            rec = self._move(rec, "cert_pending", primary_label=primary.label, phone_deadline_ms=deadline,
                             request={"request_id": o["request_id"], "signed": signed, "expires_ms": o["expires_ms"],
                                      "posted": False})  # fmt: skip
            self.store.set_outstanding(o["request_id"], rec["offer_id"], o["expires_ms"])
        return self._post_request(rec)

    def _post_request(self, rec: dict[str, Any]) -> dict[str, Any]:
        req = rec["request"]
        try:
            self.routes.post_cert_request(self.ctx.owner_person_id, req["signed"])
        except PairRelayError as e:
            if e.kind == "request_open":  # a foreign or stale request: R6 never makes two (alarm, retry the POST)
                self._alarm("pairing_request_open", rec["offer_id"])
                return rec
            if e.kind == "replayed":
                self._alarm("pairing_request_replayed", rec["offer_id"])
                self.store.clear_outstanding(req["request_id"])
                return self._move(rec, "failed", reason="replayed")
            if e.kind == "expired":
                self.store.clear_outstanding(req["request_id"])
                return self._move(rec, "expired", reason="request_expired")
            if e.kind == "refused":  # a definite refusal (not_eligible, other_person, ...): this request is dead
                self._alarm("pairing_request_refused", rec["offer_id"])
                self.store.clear_outstanding(req["request_id"])
                return self._move(rec, "failed", reason="request_refused")
            return rec  # transient: the same bytes on the next tick
        return self._move(rec, None, request={**req, "posted": True})

    def notice(self, request_ids: list[str]) -> None:
        """A ``notices.cert_requests`` wake from R2's poll: read each request this workspace holds now."""
        for oid in self.store.offer_ids():
            rec = self._get(oid)
            if rec and rec["state"] == "cert_pending" and (rec.get("request") or {}).get("request_id") in request_ids:
                self._poll_request(rec)

    def _poll_request(self, rec: dict[str, Any]) -> dict[str, Any]:
        if rec.get("cert") is not None:  # a verified cert in hand: the relay's unauthenticated state is moot (#588 2)
            return rec
        req = rec["request"]
        self._last_poll[rec["offer_id"]] = self.now_ms()
        try:
            got = self.routes.get_cert_request(req["request_id"])
        except PairRelayError as e:
            if e.kind in ("not_found", "gone", "expired"):
                return rec  # the deadline decides
            return rec
        if type(got) is not dict or got.get("state") not in ("open", "challenged", "signed", "rejected", "expired"):
            self._alarm("relay_misbehaving", "cert request answer")
            return rec
        if got.get("challenge") is not None and rec.get("challenge") is None:
            rec = self._store_challenge(rec, got["challenge"])
        if got["state"] == "signed" and got.get("cert") is not None:
            return self._cert_in_hand(rec, got["cert"])
        if got["state"] == "rejected" and got.get("rejection") is not None:
            return self._rejection_in_hand(rec, got["rejection"])
        if got["state"] == "expired":
            self.store.clear_outstanding(req["request_id"])
            return self._move(rec, "expired", reason="request_expired")
        return rec

    def _store_challenge(self, rec: dict[str, Any], signed: Any) -> dict[str, Any]:
        """7c: verified under the pinned ``PK`` against the stored request, then stored once and never replaced."""
        if rec.get("challenge") is not None:
            return rec
        try:
            certreq.check_cert_challenge(signed, self.ctx.owner_pk_pub, rec["request"]["signed"]["o"])
        except (Refused, ValueError, TypeError, KeyError):
            self._alarm("relay_misbehaving", "unverifiable challenge")
            return rec
        return self._move(rec, None, challenge=canon.loads_strict(canon.dumps(signed)))

    def _rejection_in_hand(self, rec: dict[str, Any], signed: Any) -> dict[str, Any]:
        if rec.get("cert") is not None:
            return rec
        try:
            certreq.check_cert_rejection(signed, self.ctx.owner_pk_pub, rec["request"]["signed"]["o"])
        except (Refused, ValueError, TypeError, KeyError):
            self._alarm("relay_misbehaving", "unverifiable rejection")
            return rec
        rec = self._move(rec, "rejected", rejection=canon.loads_strict(canon.dumps(signed)), reason="primary")
        self.store.clear_outstanding(rec["request"]["request_id"])
        return rec

    def _cert_in_hand(self, rec: dict[str, Any], cert: Any) -> dict[str, Any]:
        """Step 8: R6 verifies the certificate itself, never trusting the relay's state."""
        if rec["state"] not in ("confirmed", "cert_pending"):
            return rec  # a cancelled offer's cert is refused here
        c = rec["consumed"]
        try:
            o = check_cert(cert, self.ctx.owner_pk_pub)
            if o["dk_sig_pub"] != c["dk_sig_pub"] or o["dk_kx_pub"] != c["dk_kx_pub"]:
                raise Refused("cert_invalid", "other keys")
            if o["person_id"] != self.ctx.owner_person_id:
                raise Refused("cert_invalid", "other person")
            verify_cert(cert, self.ctx.owner_pk_pub, self.now_ms())
            if self.host.revoked(o["device_id"]):
                raise Refused("revoked")
            ceiling = list(o["scopes_max"])
            if rec["path"] == "7b" and ceiling != list(rec["scopes"]):
                raise Refused("cert_invalid", "scopes_max is not what was requested")
            if not set(rec["scopes"]) <= set(ceiling):
                raise Refused("cert_invalid", "the offer's scopes exceed the certificate's ceiling")
            if "decide" not in ceiling or not set(ceiling) <= set(PAIR_SCOPES):
                raise Refused("cert_invalid", "a phone certificate holds decide and at most look (N1)")
        except Refused as e:
            self._alarm("pairing_cert_invalid", e.code)
            rec = self._move(rec, "failed", reason=f"cert:{e.code}")
            if rec.get("request") is not None:
                self.store.clear_outstanding(rec["request"]["request_id"])
            return rec
        rec = self._move(rec, None, cert=canon.loads_strict(canon.dumps(cert)))
        if rec.get("request") is not None:
            self.store.clear_outstanding(rec["request"]["request_id"])
        return rec

    # -- step 9: the R6 allocation (the host's PairDeviceSource calls these) ---------------------------------------

    def _step9(self, rec: dict[str, Any] | None) -> dict[str, Any]:
        if rec is None or rec.get("tombstone"):
            raise PairRefused("relay.pair_closed", "no such offer")
        if rec["state"] not in ("confirmed", "cert_pending") or rec.get("cert") is None:
            raise PairRefused("relay.pair_closed", f"the offer is {rec['state']}, not at the allocation")
        if self.now_ms() > int(rec["phone_deadline_ms"]) - STEP9_MARGIN_MS:
            raise PairRefused("relay.pair_closed", "the phone's deadline is too near: start a new pairing")
        return rec

    def facts(self, offer_id: str) -> PairDeviceFacts:
        """Step 9's facts for ``relay.pair_device`` (``human.prepare`` and the re-fetch before ``precommit``)."""
        if not _hex32(offer_id):
            raise PairRefused("relay.pair_closed", "no such offer")
        if not self.membership.ready():
            raise PairRefused("relay.pair_closed", "the membership publisher is not running; nothing is allocated")
        rec = self._step9(self._get(offer_id))
        c = rec["consumed"]
        if rec["path"] == "7a":
            code = pairing.sas(self.ctx.workspace_id, offer_id, _pub(rec, "dk_sig_pub"), _pub(rec, "dk_kx_pub"),
                               self.ctx.wsk_pub, crypto.unb64u(rec["sas_nonce"]))  # fmt: skip
        else:
            ch = _challenge(rec, self.ctx)
            if ch is None:
                raise PairRefused("relay.pair_closed", "no verified challenge is recorded for this certificate")
            code = ch.cert_code
        try:
            return PairDeviceFacts(
                workspace_id=self.ctx.workspace_id, offer_id=offer_id, attempt=c["attempt"], cert=rec["cert"],
                label=c["label"], scopes=tuple(rec["scopes"]), path=rec["path"], code=code, decl=c["decl"],
                decl_who=c["decl_who"],
            )  # fmt: skip
        except PairDeviceInvalid as e:
            raise PairRefused("relay.pair_closed", f"the pairing facts do not hold: {e}") from None

    def precommit(self, alloc: Mapping[str, Any], *, landed: Callable[[Mapping[str, Any]], bool]) -> None:
        """(i) and (ii) under the store lock: still at step 9 with this certificate, attempt, device and scopes,
        within the deadline; the allocation intent written. An equal repeat is accepted (roll-forward). An earlier
        intent of another event is replaced only if that event never landed (``landed(intent)``): the model refuses a
        second ``device.added`` of one device anyway."""
        alloc = _intent(alloc)
        with self.store.lock:
            rec = self._step9(self._get(alloc["offer_id"]))  # state and deadline first, an equal repeat too (#588 3)
            c = rec["consumed"]
            if (
                alloc["attempt"] != c["attempt"]
                or alloc["device"] != "d_" + c["device_id"]
                or alloc["cert_digest"] != cert_digest(rec["cert"])
                or list(alloc["scopes"]) != list(rec["scopes"])
            ):
                raise PairRefused("relay.pair_closed", "the allocation is not this offer's attempt, device, "
                                                       "certificate or scopes")  # fmt: skip
            if rec.get("intent") == alloc:
                return  # the equal repeat of a roll-forward, within a valid step-9 state
            if rec.get("intent") is not None and landed(rec["intent"]):
                raise PairRefused("relay.pair_closed", "another allocation of this offer is in the log")
            self._move(rec, None, intent=alloc, intent_ms=self.now_ms())

    def committed(self, alloc: Mapping[str, Any]) -> None:
        """(vi) and (vii): ``devices/<id>.json`` and the offer ``allocated``. Idempotent. The host checked the log
        line's ``event_digest`` before calling."""
        alloc = _intent(alloc)
        with self.store.lock:
            rec = self._get(alloc["offer_id"])
            if rec is None or rec.get("intent") != alloc:
                raise PairRefused("relay.pair_closed", "no allocation intent for this event")
            if rec["state"] in ("allocated", "published", "approved") and rec.get("event_id") == alloc["event_id"]:
                self._device_record(rec, alloc)
                return
            if rec["state"] not in ("confirmed", "cert_pending"):
                raise PairRefused("relay.pair_closed", f"the offer is {rec['state']}")
            rec = self._allocate(rec, alloc)
        if rec["state"] == "allocated":
            self.post(lambda: self.publish(alloc["offer_id"]))

    def refused(self, alloc: Mapping[str, Any], reason: str) -> None:
        """Nothing was appended after an accepted ``precommit``: the offer ends ``failed`` (short ``rejected``)."""
        alloc = _intent(alloc)
        with self.store.lock:
            rec = self._get(alloc["offer_id"])
            if rec is None or rec.get("intent") != alloc or rec["state"] not in ("confirmed", "cert_pending"):
                return
            self._settle_move(rec, "failed", reason=f"allocation:{reason}"[:120])

    def adopt_existing(self, offer_id: str, alloc: Mapping[str, Any]) -> None:
        """ "Already in F1" (orch-f1 S3): the host found this exact device, keys and certificate in F1, valid; the
        intent names the existing event and the allocation completes without a second append."""
        alloc = _intent(alloc)
        with self.store.lock:
            rec = self._step9(self._get(offer_id))
            if (
                alloc.get("cert_digest") != cert_digest(rec["cert"])
                or alloc.get("device") != "d_" + rec["consumed"]["device_id"]
            ):
                raise PairRefused("relay.pair_closed", "the existing device is not this offer's")
            self._move(rec, None, intent=alloc, intent_ms=self.now_ms())
        self.committed(alloc)

    def recover(self, landed: Callable[[Mapping[str, Any]], bool]) -> list[str]:
        """At open, before either R6 thread starts: an offer whose intent's event is in the log (``landed`` matches
        by the intent's event id and digest, never by "same cert, same device") is rolled forward (``devices/`` and
        ``allocated``), through the transition table like every other write. Corrupt records are quarantined first
        (:meth:`quarantine`). Returns the offers rolled forward."""
        self.quarantine()
        out = []
        for oid in self.store.offer_ids():
            try:  # one failing offer never stops the others (#588 item 4)
                rec = self._get(oid)
                if rec is None or rec.get("intent") is None or rec["state"] not in ("confirmed", "cert_pending"):
                    continue
                if landed(rec["intent"]):
                    with self.store.lock:
                        if self._allocate(rec, rec["intent"])["state"] == "allocated":
                            out.append(oid)
            except Exception as e:  # noqa: BLE001 - alarmed with a needs item; the next open retries
                log.error("pairing: recovery of an offer failed (%s)", type(e).__name__)
                self._alarm("pairing_recovery_failed", oid)
                self._need(oid, "a pairing could not be recovered at start: look at orch host status")
        return out

    def settle(self, offer_id: str, landed: Callable[[Mapping[str, Any]], bool]) -> str | None:
        """The settlement of a recorded intent past the phone's deadline, run by the tick **through the host's store
        lock** (``PairHost.settle``), so no submit is between its precommit and its append: the intent's event in the
        log → ``allocated``; not in the log → ``failed`` (a later resubmit then fails precommit and appends
        nothing)."""
        with self.store.lock:
            rec = self._get(offer_id)
            if rec is None or rec.get("intent") is None or rec["state"] not in ("confirmed", "cert_pending"):
                return None
            if landed(rec["intent"]):
                return self._allocate(rec, rec["intent"])["state"]
            return self._settle_move(rec, "failed", reason="allocation:not_landed")["state"]

    _IDENTITY = ("device_id", "person_id", "cert", "scopes")

    def _allocate(self, rec: dict[str, Any], alloc: Mapping[str, Any]) -> dict[str, Any]:
        """(vi) then (vii) with the settlement token. ``devices/<id>.json`` is write-once: a re-admitted device whose
        stored record names the same identity (device, person, certificate, scopes) keeps it; any other content is
        alarmed, never overwritten, and the offer fails (the F1 device stays: a needs item asks to remove it)."""
        try:
            self._device_record(rec, alloc)
        except PairStoreError:
            held = self.store.device(rec["consumed"]["device_id"])
            if held is None:
                raise
            mine = self._device_doc(rec, alloc)
            if any(held.get(k) != mine[k] for k in self._IDENTITY):
                self._alarm("pairing_device_conflict", rec["offer_id"])
                self._need(rec["offer_id"], f"pairing of {rec['consumed']['label']} conflicts with an earlier "
                                            "record of the same device: remove the device")  # fmt: skip
                return self._settle_move(rec, "failed", reason="device_conflict")
        rec = self._settle_move(rec, "allocated", event_id=alloc["event_id"], allocated_ms=self.now_ms())
        if rec.get("request") is not None:
            self.store.clear_outstanding(rec["request"]["request_id"])
        return rec

    def _device_doc(self, rec: Mapping[str, Any], alloc: Mapping[str, Any]) -> dict[str, Any]:
        c = rec["consumed"]
        return {"v": 1, "device_id": c["device_id"], "person_id": self.ctx.owner_person_id, "cert": rec["cert"],
                "label": c["label"], "offer_id": rec["offer_id"], "paired_ms": int(rec.get("intent_ms") or 0),
                "decl_digest": alloc.get("decl_digest"), "scopes": list(alloc["scopes"]),
                "event_id": alloc["event_id"]}  # fmt: skip

    def _device_record(self, rec: Mapping[str, Any], alloc: Mapping[str, Any]) -> None:
        self.store.write_device(self._device_doc(rec, alloc))

    # -- step 10: publication; step 11: the baseline; step 12: approved --------------------------------------------

    def publish(self, offer_id: str) -> None:
        rec = self._get(offer_id)
        if rec is None or rec["state"] != "allocated":
            return
        device = rec["consumed"]["device_id"]
        if not rec.get("device_put"):
            try:
                self.routes.put_device(device, rec["cert"])
            except PairRelayError as e:
                if e.kind in ("cert_conflict", "refused"):  # definite: the device never becomes relay-reachable
                    self._alarm("pairing_cert_conflict" if e.kind == "cert_conflict" else "pairing_device_refused",
                                offer_id)  # fmt: skip
                    rec = self._move(rec, "failed", reason=e.kind)
                    self._exclude(rec, e.kind)
                return  # transient: the next tick
            rec = self._move(rec, None, device_put=True)
        if not rec.get("addition_posted"):
            # post, then mark: a crash in between posts again on the next tick (R6b's post_addition is idempotent
            # per offer and device); marking first would lose the addition and leave the offer to its deadline
            self.membership.post_addition(offer_id, device, rec["cert"])
            self._move(rec, None, addition_posted=True)

    def membership_committed(self, offer_id: str, publication: Mapping[str, Any]) -> None:
        rec = self._get(offer_id)
        if rec is None or rec["state"] != "allocated":
            return
        adds = publication.get("adds")
        lseq, epoch = publication.get("list_seq"), publication.get("epoch")
        if type(adds) not in (list, tuple) or rec["consumed"]["device_id"] not in adds or type(lseq) is not int or \
                type(epoch) is not int or not 1 <= epoch < 2**32 or not 1 <= lseq < 2**53:  # fmt: skip
            self._alarm("pairing_publication_mismatch", offer_id)
            return
        pub = {"list_seq": lseq, "epoch": epoch, "list_hash": publication.get("list_hash")}
        self._move(rec, None, publication=pub)
        self.baseline(offer_id)

    def membership_refused(self, offer_id: str, reason: str) -> None:
        rec = self._get(offer_id)
        if rec is None or rec["state"] != "allocated":
            return
        label = rec["consumed"]["label"]
        self._need(offer_id, f"pairing of {label} failed after it was added: remove the device")
        self._alarm("pairing_publication_refused", str(reason)[:60])
        self._move(rec, "failed", reason=f"publication:{reason}"[:120])

    def baseline(self, offer_id: str) -> None:
        rec = self._get(offer_id)
        if rec is None or rec["state"] != "allocated" or rec.get("publication") is None:
            return
        now = self.now_ms()
        if now < self._retry_at.get(offer_id, 0):
            return
        cp = self.host.checkpoint()
        card_seq = self.host.card_seq()
        if cp is None or card_seq is None:
            self._retry_at[offer_id] = now + BASELINE_RETRY_MS
            self._alarm("pairing_baseline_missing", offer_id)  # a linked workspace always holds a checkpoint
            return
        try:
            self.routes.post_baseline(cp["hash"], offer_id)
        except PairRelayError as e:
            self._retry_at[offer_id] = now + (BASELINE_RETRY_MS if e.kind == "quota" else BASELINE_SOON_MS)
            if e.kind == "quota":
                self._alarm("pairing_baseline_quota", offer_id)
            return
        rec = self._move(rec, "published", card_seq=card_seq, baseline={"n": cp["n"], "hash": cp["hash"]})
        self._move(rec, "approved", approved_ms=self.now_ms())

    def _exclude(self, rec: Mapping[str, Any], reason: str) -> None:
        c = rec.get("consumed") or {}
        if c.get("device_id"):
            self.membership.post_exclusion(rec["offer_id"], c["device_id"], reason)
            self._need(rec["offer_id"], f"pairing of {c.get('label', '')} did not finish: remove the device")

    # -- the timer (deadlines, polls, retries, tails) --------------------------------------------------------------

    def tick(self) -> None:
        now = self.now_ms()
        for oid in self.store.offer_ids():
            try:
                rec = self._get(oid)
            except PairStoreError:
                continue
            if rec is None or rec.get("tombstone"):
                continue
            s = rec["state"]
            dl = rec.get("phone_deadline_ms")
            try:
                if s == "open" and now >= int(rec["expires_ms"]):
                    self._move(rec, "expired", reason="offer_expired")
                elif s in ("confirmed", "cert_pending") and rec.get("intent") is not None:
                    # step 9 is the host's: past the deadline the tick settles it under the store lock, by the log
                    if dl is not None and now > int(dl):
                        self.host.settle(lambda landed, o=oid: self.settle(o, landed))
                elif (
                    s in ("pending", "confirmed", "cert_pending")
                    and dl is not None
                    and (now > int(dl) or (rec.get("cert") is not None and now > int(dl) - STEP9_MARGIN_MS))
                ):
                    self._move(rec, "expired", reason="phone_deadline")
                elif s in ("allocated", "published") and dl is not None and now > int(dl):
                    rec = self._move(rec, "expired", reason="phone_deadline")
                    self._exclude(rec, "deadline")
                elif (
                    s == "cert_pending"
                    and rec.get("request") is not None
                    and rec.get("cert") is None
                    and now >= int(rec["request"]["expires_ms"])
                ):
                    self._move(rec, "expired", reason="request_expired")
                elif s == "cert_pending" and rec.get("request") is not None and rec.get("cert") is None:
                    if not rec["request"].get("posted"):
                        self._post_request(rec)
                    elif now - self._last_poll.get(oid, 0) >= POLL_EVERY_MS:
                        self._poll_request(rec)
                elif s == "allocated":
                    if rec.get("publication") is None:
                        self.publish(oid)
                    else:
                        self.baseline(oid)
                elif s in TERMINAL or s == "approved":
                    self._tail(rec, now)
            except PairStoreError:
                log.warning("pairing: offer record unreadable during the tick")
            except StaleRecord:
                continue  # another event wrote it first: the next tick re-reads
            except IllegalTransition as e:
                log.error("pairing: refused transition in the tick: %s", e)
            except (OSError, StateError) as e:  # a failed write: the offer stays in its last durable state
                log.error(
                    "pairing: a durable write failed in the tick (%s); retried at the next tick", type(e).__name__
                )
        out = self.store.outstanding()
        if out is not None and now > int(out["expires_ms"]) + OUTSTANDING_GRACE_MS:
            self.store.clear_outstanding(out["request_id"])
        try:
            self._repost(now)
        except (OSError, StateError, PairStoreError, ValueError, KeyError) as e:  # never ends the tick
            log.error("pairing: re-posting a stored answer failed (%s); retried later", type(e).__name__)
        self._flush_dropped(now)

    def _flush_dropped(self, now: int) -> None:
        """The ``pairing_dropped`` counter (11A.15.2), durably at most once a minute: the alarm's ``count`` is the
        total of silent drops (unknown offer, bad tag, budget, queue overflow) since the alarms were reset."""
        delta = self.dropped - self._dropped_flushed
        if delta <= 0 or now - self._dropped_at < 60_000:
            return
        try:
            with self.store.lock:
                obj = self.store.alarms()
                a = obj["alarms"].setdefault("pairing_dropped", {"count": 0, "at_ms": 0, "detail": ""})
                a["count"] = int(a.get("count", 0)) + delta
                a["at_ms"], a["detail"] = now, "silent drops"
                self.store.write_alarms(obj)
        except (OSError, StateError):
            return  # retried at the next tick
        self._dropped_flushed += delta
        self._dropped_at = now

    def _tail(self, rec: dict[str, Any], now: int) -> None:
        end = int(rec.get("terminal_ms") or rec.get("approved_ms") or 0)
        if rec["state"] != "approved" and rec.get("secret") is not None and now > end + TAIL_MS:
            self._move(rec, None, secret=None, sas_nonce=None)
            self.store.remove_answers(rec["offer_id"])
        elif now > end + TOMBSTONE_MS:
            c = rec.get("consumed") or {}
            with self.store.lock:  # the tombstone replaces the record: compare-and-set too
                cur = self._get(rec["offer_id"])
                if cur is None or cur.get("ver", 0) != rec.get("ver", 0) or cur["state"] not in TERMINAL | {"approved"}:
                    raise StaleRecord(rec["offer_id"])
                tomb = {"v": 1, "offer_id": rec["offer_id"], "state": rec["state"], "device_id": c.get("device_id"),
                        "tombstone": True, "ver": int(cur.get("ver", 0)) + 1}  # fmt: skip
                self.store.put(tomb)


class _Events:
    """The engine's :class:`MembershipEvents`: each event becomes a pair-thread job (never inline on the membership
    thread, never re-entrant)."""

    def __init__(self, engine: PairEngine) -> None:
        self._engine = engine

    def on_committed(self, offer_id: str, publication: Mapping[str, Any]) -> None:
        pub = dict(publication)
        self._engine.post(lambda: self._engine.membership_committed(offer_id, pub))

    def on_refused(self, offer_id: str, reason: str) -> None:
        self._engine.post(lambda: self._engine.membership_refused(offer_id, reason))


#: the internal API's names (11A.15.9A); pinned by a test, the settlement paths are never among them
API_METHODS = frozenset({"offer", "answer", "cancel", "abandon", "list_offers", "pair_cert", "show"})


def run_api(engine: PairEngine, name: str, *args: Any, **kw: Any) -> Any:
    """Run one internal-API call on ``engine`` (the caller is on the pair thread, or the engine is dormant)."""
    if type(name) is not str or name not in API_METHODS:
        raise PairRefused("invalid.input", "not a pairing API call")
    return engine.api_table()[name](*args, **kw)


# -- the thread and R2's seam --------------------------------------------------------------------------------------

QUEUE_ITEMS = 64  # the pair thread's bounded item queue; overflow is a silent, counted drop (Codex 2)
TICK_S = 1.0


class PairThread:
    """``orch-relay-r6p-<id8>``: runs every engine job and the timer. Event-driven: it waits on its queue with a
    timeout (the next tick), never sleeps. :meth:`call` runs a job and returns its result (the internal API);
    :meth:`post` queues one. :meth:`handle` and :meth:`completed` are R2's ``R6Pairing`` (11A.15.2)."""

    def __init__(self, engine: PairEngine, *, name: str, on_stop: Callable[[], None] | None = None) -> None:
        self.engine = engine
        self._on_stop = on_stop
        self._q: queue.Queue[Any] = queue.Queue()
        self._items = 0  # pair items queued and not yet handled
        self._lock = threading.Lock()
        self._done: set[int] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._main, name=name, daemon=True)
        engine.post = self.post

    @property
    def thread(self) -> threading.Thread:
        return self._thread

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Stop the thread; ``on_stop`` returns the engine to dormant (no session, no publisher: step 9 refuses)."""
        self._stop.set()
        self._q.put(None)
        if self._on_stop is not None:
            self._on_stop()

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def post(self, fn: Callable[[], None]) -> None:
        self._q.put(("job", fn, None))

    def api(self, name: str, *args: Any, timeout: float = 30.0, **kw: Any) -> Any:
        """Run the internal-API call ``name`` (one of :data:`API_METHODS`, nothing else) as a pair-thread job and
        return (or raise) its result. There is no generic call: the settlement paths stay the host's."""
        if type(name) is not str or name not in API_METHODS:
            raise PairRefused("invalid.input", "not a pairing API call")
        return self._call(lambda: run_api(self.engine, name, *args, **kw), timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    def _call(self, fn: Callable[[], Any], timeout: float = 30.0) -> Any:
        """Run ``fn`` on the pair thread and return (or raise) its result. A call that times out **before it runs**
        is dropped (it never runs later: the caller was told ``workspace.unavailable``, so nothing was applied); one
        that times out **while it runs** is ``outcome.unknown`` (look at the offer before acting again). Opus review
        of #589, L2."""
        if threading.current_thread() is self._thread:
            return fn()
        done = threading.Event()
        box: dict[str, Any] = {}
        self._q.put(("call", fn, (done, box)))
        if not done.wait(timeout):
            with self._lock:
                started = box.get("started", False)
                if not started:
                    box["dropped"] = True
            if not started:
                raise PairRefused("workspace.unavailable", "the pair thread did not answer in time; nothing was done")
            if not done.wait(0):
                raise PairRefused("outcome.unknown", "the call is still running: look at the offer before retrying")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # R2's R6Pairing ---------------------------------------------------------------------------------------------------

    def handle(self, n: int, envelope: bytes, rid: str, device: str | None) -> None:
        """Queue a ``route:"pair"`` item and return at once. Overflow (64 items) is a silent, counted drop, and the
        item is completed at once (no write)."""
        with self._lock:
            if self._items >= QUEUE_ITEMS:
                self._done.add(n)
                self.engine.dropped += 1
                return
            self._items += 1
        self._q.put(("item", (n, envelope, rid, device), None))

    def notice(self, request_ids: list[str]) -> None:
        """A ``notices.cert_requests`` wake from R2's poll."""
        ids = list(request_ids)
        self.post(lambda: self.engine.notice(ids))

    def completed(self, n: int) -> bool:
        with self._lock:
            return n in self._done

    def forget_below(self, n: int) -> None:
        """R2's cursor passed ``n``: completed numbers at or below it are no longer asked about."""
        with self._lock:
            self._done = {m for m in self._done if m > n}

    # the loop ---------------------------------------------------------------------------------------------------------

    def _main(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            wait = max(0.0, next_tick - time.monotonic())
            try:
                job = self._q.get(timeout=wait)
            except queue.Empty:
                job = ("tick", None, None)
            if job is None:
                return
            kind, fn, extra = job
            try:
                if kind == "tick":
                    try:
                        self.engine.tick()
                    finally:  # a tick that raised is not run again at once: no unthrottled loop
                        next_tick = time.monotonic() + TICK_S
                elif kind == "item":
                    n, env, rid, device = fn
                    try:
                        self.engine.receive(env, rid, device)
                    finally:
                        with self._lock:
                            self._items -= 1
                            self._done.add(n)
                elif kind == "call":
                    done, box = extra
                    with self._lock:
                        dropped = box.get("dropped", False)
                        box["started"] = not dropped
                    if dropped:  # the caller gave up before it started: never run later
                        continue
                    try:
                        box["result"] = fn()
                    except BaseException as e:  # noqa: BLE001 - handed back to the caller
                        box["error"] = e
                    finally:
                        done.set()
                else:
                    fn()
            except Exception as e:  # noqa: BLE001 - one job never stops the pair thread
                log.error("pair thread job failed: %s", type(e).__name__)
