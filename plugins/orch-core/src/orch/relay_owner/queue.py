"""The primary's cert-request queue (orch-v2-host 11A.15.9 "Inline primary steps", protocol §8.4 steps 2 to 4): read
the open requests addressed to this primary, check and open each under the journal's lock, sign the challenge, the
certificate or the rejection with the person key, and upload them, every transition journaled before its effect.

Shared with the test owner client (N6): ``tests/relay/owner.py`` (orch-29) wraps this class with its sandbox person
key; only the person-key source and the packaging differ.

Rules (each enforced here, independently of what the relay asserts):

* **Check, open and record under one lock** (``CertJournal``, Opus L2): ``check_cert_request`` (with the caller's
  :class:`~orch.identity.wscard.VerifiedCard` for the request's workspace, never a relay-served card),
  ``open_cert_request`` (the primary's ``kx`` unseal, purpose ``cert-request``) and the ``verified`` write happen in
  one journal transaction, so two requests (or two owner clients) can never both pass ``request_open``. A refusal
  from step 2.4 on burns the id (``refused``); one before it (``other_person``, ``bad_signature``,
  ``not_for_this_device``) is reported and burns nothing (its id is not authenticated).
* **Journal first.** Live requests the journal holds are resumed from the stored bytes (the relay lists open requests
  only, so a challenged request is recoverable only here); a listed id the journal holds is compared byte for byte
  (``primary_request_conflict``); a listed id the journal closed is never acted on again.
* **Never re-sign.** A challenge, certificate or rejection is signed at most once per request; a resume resends the
  stored bytes. Nothing is signed after ``expires_ms`` (local clock), after a rejection, or for a request this primary
  closed. The certificate needs a stored challenge (the human compared ``cert_code`` first).
* **Least privilege.** The person key signs only ``sig_cert_challenge``, ``sig_cert_rejection`` and
  ``sig_device_cert`` (:data:`ANSWER_LABELS`; build the ``RecoveryCodeSource`` with exactly these), and only after
  its person is checked against the pinned owner key. **JUDGEMENT CALL (N1):** a request whose ``scopes_max``
  exceeds ``max_scopes`` (default ``look, decide``: no ``operate``, never ``type`` for a phone in v1.7) can be
  rejected but never challenged or signed.
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from orch import canon, crypto
from orch.identity import Refused, certreq, certs, pairing
from orch.identity.certreq import CertInner, CertRequest, RequestRefused
from orch.identity.wscard import VerifiedCard

from .client import OwnerClient
from .journal import Entry, JournalError, JournalView, PrimaryJournal, RequestConflict

__all__ = ["ANSWER_LABELS", "PHONE_SCOPES", "Pending", "Poll", "PrimaryQueue", "QueueRefused", "Skipped"]

ANSWER_LABELS = frozenset({"sig_cert_challenge", "sig_cert_rejection", "sig_device_cert"})
PHONE_SCOPES = ("look", "decide")  # 11A.15.6 N1: the v1.7 phone ceiling
MAX_REQUEST_BYTES = 8 * 1024  # a real request is about 1.5 KiB; a bigger one is never checked or journaled


class QueueRefused(Exception):
    """The queue refused a step locally, before any signature: ``code`` is ``expired``, ``scope_exceeded``,
    ``not_live``, ``not_challenged``, ``already_decided``, ``person_mismatch`` or ``not_this_request``."""

    def __init__(self, code: str, request_id: str) -> None:
        super().__init__(f"{code}: request {request_id}")
        self.code, self.request_id = code, request_id


@dataclass(frozen=True)
class Pending:
    """A request this primary checked and opened (minted ``request``/``inner``), live in the journal. Show
    :attr:`label` (``clean_label`` applied, "named by the device"), :attr:`scopes_max`, :attr:`cert_code` once
    challenged, and warn on ``client_hosted``."""

    request: CertRequest = field(repr=False)
    inner: CertInner = field(repr=False)
    state: str
    challenge: dict[str, Any] | None = field(default=None, repr=False)
    within_policy: bool = True

    @property
    def request_id(self) -> str:
        return self.request.request_id

    @property
    def workspace_id(self) -> str:
        return self.request.workspace_id

    @property
    def expires_ms(self) -> int:
        return self.request.expires_ms

    @property
    def device_id(self) -> str:
        return self.inner.device_id

    @property
    def label(self) -> str:
        return self.inner.display_label

    @property
    def scopes_max(self) -> tuple[str, ...]:
        return self.inner.scopes_max

    @property
    def client_hosted(self) -> bool:
        return self.request.client_hosted

    @property
    def cert_code(self) -> str | None:
        return None if self.challenge is None else pairing.cert_code(self.challenge["o"])


@dataclass(frozen=True)
class Skipped:
    """A listed request this primary did not take: ``code`` is the §8.4 refusal (``bad_signature``,
    ``not_for_this_device``, ``other_person``, ``expired``, ``replayed``, ``request_open``, ``malformed``,
    ``scope_exceeded``), ``unknown_workspace`` (no card for it), ``closed`` (the journal closed it),
    ``primary_request_conflict``, ``too_large`` (over :data:`MAX_REQUEST_BYTES`: never checked or journaled) or
    ``journal.full`` (it waits until expired records are pruned).
    ``request_id`` is ``None`` when the relay's text was not a valid id."""

    request_id: str | None
    code: str


@dataclass(frozen=True)
class Poll:
    pending: tuple[Pending, ...]
    skipped: tuple[Skipped, ...]

    def get(self, request_id: str) -> Pending | None:
        return next((p for p in self.pending if p.request_id == request_id), None)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _rid(o: object) -> str | None:
    rid = o.get("request_id") if type(o) is dict else None
    try:
        return crypto.hex_id(crypto.unhex(rid, 16))
    except crypto.EncodingError:
        return None


class PrimaryQueue:
    """The primary's side of §8.4 for one command.

    ``client`` is logged in (or logs in) as the primary; ``journal`` the primary's journal; ``pk_pub`` the pinned
    owner key (the genesis delegation's, never the relay's); ``cards(workspace_id)`` the verified card the caller holds
    for a workspace (``None``: not this primary's business); ``unseal(object_id, sealed)`` the ``kx`` custody unseal
    (``orch.custody.primary.cert_request_unseal(session)``). ``now_ms`` and ``nonce`` are seams for tests."""

    def __init__(
        self,
        client: OwnerClient,
        journal: PrimaryJournal,
        *,
        pk_pub: bytes,
        cards: Callable[[str], VerifiedCard | None],
        unseal: Callable[[bytes, bytes], bytes],
        max_scopes: tuple[str, ...] = PHONE_SCOPES,
        now_ms: Callable[[], int] = _now_ms,
        nonce: Callable[[], bytes] = lambda: secrets.token_bytes(32),
    ) -> None:
        self._client, self._journal = client, journal
        self._pk = crypto.validate_public_key(pk_pub)
        self._person = crypto.person_id(self._pk).hex()
        self._cards, self._unseal = cards, unseal
        self._max = tuple(max_scopes)
        self._now, self._nonce = now_ms, nonce

    @property
    def person_id(self) -> str:
        return self._person

    # -- reading the queue --------------------------------------------------------------------------------------------

    def poll(self, *, wait: int | None = None) -> Poll:
        """Read the relay's queue, then, under one journal transaction: resume every live journaled request, and check,
        open and record each new one. Returns what the human may act on and what was skipped."""
        raws = self._client.cert_requests(self._person, wait=wait)
        pending: dict[str, Pending] = {}
        skipped: list[Skipped] = []
        with self._journal.transaction() as v:
            now = self._now()
            v.prune(now)
            listed = {}
            for raw in raws:
                rid = _rid(raw.get("o"))
                if rid is None:
                    skipped.append(Skipped(None, "malformed"))
                    continue
                listed[rid] = raw
            for e in v.entries():  # journal first
                if e.live:
                    p = self._resume(v, e, now, skipped)
                    if p is not None:
                        pending[p.request_id] = p
            for rid, raw in listed.items():
                cur = v.get(rid)
                if cur is not None:
                    if cur.request_cj is not None and canon.dumps(raw) != cur.request_cj:
                        skipped.append(Skipped(rid, "primary_request_conflict"))
                    elif rid not in pending:
                        skipped.append(Skipped(rid, "closed"))
                    continue
                p = self._take(v, rid, raw, now, skipped)
                if p is not None:
                    pending[rid] = p
        return Poll(tuple(pending.values()), tuple(skipped))

    def _policy(self, inner: CertInner) -> bool:
        return all(s in self._max for s in inner.scopes_max)

    def _take(self, v: JournalView, rid: str, raw: dict, now: int, skipped: list[Skipped]) -> Pending | None:
        if len(canon.dumps(raw)) > MAX_REQUEST_BYTES:
            skipped.append(Skipped(rid, "too_large"))
            return None
        o = raw["o"]
        ws = o.get("workspace_id")
        card = self._cards(ws) if type(ws) is str else None
        if card is None:
            skipped.append(Skipped(rid, "unknown_workspace"))
            return None
        try:
            req = certreq.check_cert_request(
                raw, card, me_person_id=self._person, me_device_id=self._client.device_id,
                journal=v.cert_journal(), now_ms=now,
            )  # fmt: skip
        except RequestRefused as e:  # step 2.4 on: the id is authenticated, burn it
            exp = o.get("expires_ms")
            ok = type(exp) is int and 0 <= exp <= certreq.MAX_SAFE_INT
            self._burn(v, Entry(e.request_id, card.workspace_id, exp if ok else 0, "refused"))
            skipped.append(Skipped(e.request_id, e.code))
            return None
        except Refused as e:  # before 2.4: nothing authenticated, nothing burned
            skipped.append(Skipped(rid, e.code))
            return None
        try:
            inner = certreq.open_cert_request(req, self._unseal)
        except Refused as e:
            self._burn(v, Entry(req.request_id, req.workspace_id, req.expires_ms, "refused"))
            skipped.append(Skipped(req.request_id, e.code))
            return None
        try:
            v.put(Entry(req.request_id, req.workspace_id, req.expires_ms, "verified", request_cj=canon.dumps(raw)))
        except RequestConflict:  # pragma: no cover - the id was not in the journal a moment ago, under the lock
            skipped.append(Skipped(rid, "primary_request_conflict"))
            return None
        except JournalError as e:  # journal.full: this request waits; nothing else breaks
            skipped.append(Skipped(rid, e.code))
            return None
        return Pending(req, inner, "verified", None, self._policy(inner))

    @staticmethod
    def _burn(v: JournalView, e: Entry) -> None:
        """Record a refused id; a full journal only means it is not remembered (it is refused again next time)."""
        try:
            v.put(e)
        except JournalError:
            pass

    def _resume(self, v: JournalView, e: Entry, now: int, skipped: list[Skipped]) -> Pending | None:
        """Re-check and re-open a live journaled request from its stored bytes (against everything else in the
        journal); an expired one is closed ``expired``."""
        raw = e.request
        card = self._cards(e.workspace_id)
        if card is None or raw is None:
            skipped.append(Skipped(e.request_id, "unknown_workspace"))
            return None
        try:
            req = certreq.check_cert_request(
                raw, card, me_person_id=self._person, me_device_id=self._client.device_id,
                journal=v.cert_journal(excluding=e.request_id), now_ms=now,
            )  # fmt: skip
            inner = certreq.open_cert_request(req, self._unseal)
        except Refused as r:
            if r.code == "expired":
                v.put(Entry(e.request_id, e.workspace_id, e.expires_ms, "expired", request_cj=e.request_cj,
                            challenge_cj=e.challenge_cj))  # fmt: skip
            skipped.append(Skipped(e.request_id, r.code))
            return None
        return Pending(req, inner, e.state, e.challenge, self._policy(inner))

    # -- acting on one request ----------------------------------------------------------------------------------------

    def _live(self, v: JournalView, p: Pending) -> Entry:
        if type(p) is not Pending:
            raise TypeError("a Pending from poll()")
        certreq.require_minted(p.request, CertRequest)
        e = v.get(p.request_id)
        if e is None or e.request is None or canon.dumps(e.request["o"]) != p.request.o_cj:
            raise QueueRefused("not_this_request", p.request_id)
        return e

    def _usable(self, v: JournalView, e: Entry, p: Pending, pk: Any) -> None:
        """Before any person-key operation: live, unexpired by the local clock, within policy, and ``pk`` is the
        pinned owner's key."""
        if not e.live:
            raise QueueRefused("already_decided" if e.state in ("decided", "uploaded") else "not_live", e.request_id)
        if self._now() >= e.expires_ms:
            v.put(Entry(e.request_id, e.workspace_id, e.expires_ms, "expired", request_cj=e.request_cj,
                        challenge_cj=e.challenge_cj))  # fmt: skip
            raise QueueRefused("expired", e.request_id)
        pub = pk.pk_pub
        if type(pub) is not bytes or not hmac.compare_digest(pub, self._pk):
            raise QueueRefused("person_mismatch", e.request_id)

    def challenge(self, p: Pending, pk: Any) -> Pending:
        """Sign the challenge (§8.4 step 3) with ``primary_nonce`` drawn now, journal it, then post it. A request
        already challenged is never signed again: the stored bytes are posted again. ``pk`` is the entered person-key
        source. Returns the challenged :class:`Pending` (show :attr:`Pending.cert_code`)."""
        with self._journal.transaction() as v:
            e = self._live(v, p)
            if e.state != "challenged":
                self._usable(v, e, p, pk)
                if not p.within_policy:
                    raise QueueRefused("scope_exceeded", p.request_id)
                signed = certreq.make_cert_challenge(
                    self._pk, pk.signer("sig_cert_challenge"), p.request, p.inner, self._nonce()
                )
                e = v.put(Entry(e.request_id, e.workspace_id, e.expires_ms, "challenged", request_cj=e.request_cj,
                                challenge_cj=canon.dumps(signed)))  # fmt: skip
        challenge = e.challenge
        assert challenge is not None
        self._client.post_challenge(p.request_id, challenge)
        return Pending(p.request, p.inner, "challenged", challenge, p.within_policy)

    def sign(self, p: Pending, pk: Any) -> dict[str, Any]:
        """Sign the device certificate (§8.4 step 4) after the human confirmed ``cert_code``: ``scopes_max`` as
        requested, ``expires_ms: null`` (v1.7), the label sealed under the label key derived from ``PK``. Journaled
        ``decided`` before ``PUT /v2/devices/{d}``; returns the certificate (hand it to R6 with ``relay.pair_cert``).
        A request already signed returns (and re-uploads) the stored certificate."""
        with self._journal.transaction() as v:
            e = self._live(v, p)
            if e.state == "challenged":
                self._usable(v, e, p, pk)
                if not p.within_policy:
                    raise QueueRefused("scope_exceeded", p.request_id)
                person = crypto.person_id(self._pk)
                device = bytes.fromhex(p.inner.device_id)
                label_sealed = certs.seal_device_label(pk.derive_label_key(), person, device, p.inner.label)
                cert = certs.make_device_cert(
                    self._pk, pk.signer("sig_device_cert"), dk_sig_pub=p.inner.dk_sig_pub,
                    dk_kx_pub=p.inner.dk_kx_pub, label_sealed=label_sealed, created_ms=self._now(), expires_ms=None,
                    scopes_max=list(p.inner.scopes_max),
                )  # fmt: skip
                e = v.put(Entry(e.request_id, e.workspace_id, e.expires_ms, "decided", request_cj=e.request_cj,
                                challenge_cj=e.challenge_cj, decision="sign",
                                decided_cj=canon.dumps(cert)))  # fmt: skip
            elif e.state == "verified":
                raise QueueRefused("not_challenged", p.request_id)
            elif e.decision != "sign":
                raise QueueRefused("already_decided", p.request_id)
        return self._upload(e)

    def reject(self, p: Pending, pk: Any) -> dict[str, Any]:
        """Sign the rejection (P3-D11) and post it, journaled ``decided`` first. A request already rejected re-posts
        the stored rejection; one already signed is refused."""
        with self._journal.transaction() as v:
            e = self._live(v, p)
            if e.live:
                self._usable(v, e, p, pk)
                signed = certreq.make_cert_rejection(self._pk, pk.signer("sig_cert_rejection"), p.request)
                e = v.put(Entry(e.request_id, e.workspace_id, e.expires_ms, "decided", request_cj=e.request_cj,
                                challenge_cj=e.challenge_cj, decision="reject",
                                decided_cj=canon.dumps(signed)))  # fmt: skip
            elif e.decision != "reject":
                raise QueueRefused("already_decided" if e.decision else "not_live", p.request_id)
        return self._upload(e)

    def _upload(self, e: Entry) -> dict[str, Any]:
        decided = e.decided
        assert decided is not None and e.decision is not None
        if e.state == "decided":
            if e.decision == "sign":
                self._client.register_device(decided, self._pk)
            else:
                self._client.post_rejection(e.request_id, decided)
            with self._journal.transaction() as v:
                cur = v.get(e.request_id)
                if cur is not None and cur.state == "decided":
                    v.put(Entry(e.request_id, e.workspace_id, e.expires_ms, "uploaded", request_cj=e.request_cj,
                                challenge_cj=e.challenge_cj, decision=e.decision, decided_cj=e.decided_cj))  # fmt: skip
        return decided

    def finish_uploads(self) -> list[str]:
        """Resend every ``decided`` record's stored bytes (a crash between the decision and the relay's 2xx). Returns
        the request ids now uploaded."""
        with self._journal.transaction() as v:
            todo = [e for e in v.entries() if e.state == "decided" and not e.tombstone]
        for e in todo:
            self._upload(e)
        return [e.request_id for e in todo]

    def close(self, request_id: str, state: str) -> None:
        """End a live request locally: ``cancelled`` (its offer was cancelled), ``expired`` or ``superseded``."""
        if state not in ("cancelled", "expired", "superseded"):
            raise ValueError("state")
        with self._journal.transaction() as v:
            e = v.get(request_id)
            if e is None or not e.live:
                raise QueueRefused("not_live", request_id)
            v.put(Entry(e.request_id, e.workspace_id, e.expires_ms, state, request_cj=e.request_cj,
                        challenge_cj=e.challenge_cj))  # fmt: skip
