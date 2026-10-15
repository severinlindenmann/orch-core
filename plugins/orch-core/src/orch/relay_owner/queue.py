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
* **One request per command** (review of #557, DECIDED): :meth:`PrimaryQueue.poll` works only the ``request_id`` R6
  reported for this command's offer; every other listed request is skipped ``other_open`` (shown as a warning),
  neither opened nor journaled. A listed entry that is not protocol JSON (nesting deeper than 16) is ``malformed``.
* **Journal first.** A live request the journal holds is resumed from the stored bytes (the relay lists open requests
  only, so a challenged request is recoverable only here); a listed copy is compared byte for byte
  (``primary_request_conflict``); an id the journal closed is never acted on again.
* **Actions take the id only** (review of #557, Opus Medium). :class:`Pending` is a display snapshot; ``challenge``,
  ``sign`` and ``reject`` take a ``request_id`` and, under the lock, re-check and re-open the request from the
  **stored** bytes, recompute the policy, and (for the certificate) require the inner's device and both keys to be
  the stored challenge's.
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
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from orch import canon, crypto
from orch.identity import Refused, certreq, certs, pairing
from orch.identity.certreq import CertInner, CertRequest, RequestRefused
from orch.identity.wscard import VerifiedCard

from .client import OwnerClient, OwnerError, OwnerRefused
from .journal import DECIDED, Entry, JournalError, JournalView, PrimaryJournal, RequestConflict

__all__ = [
    "ANSWER_LABELS",
    "PERMANENT_UPLOAD",
    "PHONE_SCOPES",
    "Pending",
    "Poll",
    "PrimaryQueue",
    "QueueRefused",
    "Skipped",
]

ANSWER_LABELS = frozenset({"sig_cert_challenge", "sig_cert_rejection", "sig_device_cert"})
PHONE_SCOPES = ("look", "decide")  # 11A.15.6 N1: the v1.7 phone ceiling
PERMANENT_UPLOAD = frozenset({"expired", "not_found", "cert_conflict"})  # review of #557 (Codex P2)
log = logging.getLogger("orch.relay_owner.queue")
MAX_REQUEST_BYTES = 8 * 1024  # a real request is about 1.5 KiB; a bigger one is never checked or journaled


class QueueRefused(Exception):
    """The queue refused a step locally, before any signature: ``code`` is ``expired``, ``scope_exceeded``,
    ``not_live``, ``not_challenged``, ``already_decided``, ``person_mismatch``, ``not_this_request`` (an id the journal
    does not hold, or a stored challenge that does not match the stored request), or a re-check's §8.4 code
    (``unknown_workspace`` when the card is gone)."""

    def __init__(self, code: str, request_id: str) -> None:
        super().__init__(f"{code}: request {request_id}")
        self.code, self.request_id = code, request_id


@dataclass(frozen=True)
class Pending:
    """What the human sees of one live request: a **display snapshot**, never an input. Every action takes the
    ``request_id`` only and re-derives everything from the journal's stored bytes under the lock (review of #557,
    Opus Medium: a caller-built or ``replace``-d snapshot changes nothing). Show :attr:`label` (``clean_label``
    applied, "named by the device"), :attr:`scopes_max`, :attr:`cert_code` once challenged, and warn on
    ``client_hosted``."""

    request_id: str
    workspace_id: str
    expires_ms: int
    device_id: str
    label: str
    scopes_max: tuple[str, ...]
    client_hosted: bool
    state: str
    within_policy: bool
    cert_code: str | None = None


@dataclass(frozen=True)
class Skipped:
    """A listed request this primary did not take: ``code`` is the §8.4 refusal (``bad_signature``,
    ``not_for_this_device``, ``other_person``, ``expired``, ``replayed``, ``request_open``, ``malformed``,
    ``scope_exceeded``), ``other_open`` (not the request this command works), ``unknown_workspace`` (no card for it),
    ``closed`` (the journal closed it),
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

    def poll(self, request_id: str, *, wait: int | None = None) -> Poll:
        """Work the one request R6 reported for this command's offer (11A.15.9: "The challenge, cert and rejection are
        made only for the request R6 created for **this** CLI's offer"). Reads the relay's queue, then, under one
        journal transaction: resumes the request if the journal holds it live, else checks, opens and records it.
        Every other listed request is skipped ``other_open`` (a warning to show), neither opened nor journaled."""
        target = _rid({"request_id": request_id})
        if target is None:
            raise ValueError("request_id")
        raws = self._client.cert_requests(self._person, wait=wait)
        skipped: list[Skipped] = []
        listed: dict[str, tuple[dict, bytes]] = {}
        for raw in raws:
            rid = _rid(raw.get("o"))
            try:
                raw_cj = canon.dumps(raw)  # protocol subset, depth <= 16 (review of #557: a deeper one is malformed)
            except (canon.JcsError, ValueError, RecursionError):
                log.warning("cert queue: a listed request is not canonical JSON; skipped")
                skipped.append(Skipped(rid, "malformed"))
                continue
            if rid is None:
                skipped.append(Skipped(None, "malformed"))
            elif rid != target:
                skipped.append(Skipped(rid, "other_open"))
            else:
                listed[rid] = (raw, raw_cj)
        with self._journal.transaction() as v:
            now = self._now()
            v.prune(now)
            cur = v.get(target)
            p = None
            if cur is not None:
                if target in listed and (cur.request_cj is None or listed[target][1] != cur.request_cj):
                    skipped.append(Skipped(target, "primary_request_conflict"))
                if cur.live:
                    p = self._resume(v, cur, now, skipped)
                elif target in listed and cur.request_cj == listed[target][1]:
                    skipped.append(Skipped(target, "closed"))
            elif target in listed:
                p = self._take(v, target, *listed[target], now, skipped)
        return Poll(() if p is None else (p,), tuple(skipped))

    def _policy(self, inner: CertInner) -> bool:
        return all(s in self._max for s in inner.scopes_max)

    def _view(self, req: CertRequest, inner: CertInner, e_state: str, challenge: dict | None) -> Pending:
        return Pending(
            request_id=req.request_id, workspace_id=req.workspace_id, expires_ms=req.expires_ms,
            device_id=inner.device_id, label=inner.display_label, scopes_max=inner.scopes_max,
            client_hosted=req.client_hosted, state=e_state, within_policy=self._policy(inner),
            cert_code=None if challenge is None else pairing.cert_code(challenge["o"]),
        )  # fmt: skip

    def _take(
        self, v: JournalView, rid: str, raw: dict, raw_cj: bytes, now: int, skipped: list[Skipped]
    ) -> Pending | None:
        if len(raw_cj) > MAX_REQUEST_BYTES:
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
            v.put(Entry(req.request_id, req.workspace_id, req.expires_ms, "verified", request_cj=raw_cj))
        except RequestConflict:  # pragma: no cover - the id was not in the journal a moment ago, under the lock
            skipped.append(Skipped(rid, "primary_request_conflict"))
            return None
        except JournalError as e:  # journal.full: this request waits; nothing else breaks
            skipped.append(Skipped(rid, e.code))
            return None
        return self._view(req, inner, "verified", None)

    @staticmethod
    def _burn(v: JournalView, e: Entry) -> None:
        """Record a refused id; a full journal only means it is not remembered (it is refused again next time)."""
        try:
            v.put(e)
        except JournalError:
            pass

    def _reload(self, v: JournalView, e: Entry, now: int) -> tuple[CertRequest, CertInner]:
        """The request and its inner, re-checked and re-opened from the journal's **stored** bytes (against everything
        else in the journal), at ``now``. An expired one is written ``expired``; a refusal is :class:`Refused`; no
        card is ``unknown_workspace`` (the record stays live until it expires: prune then ends it)."""
        raw = e.request
        card = self._cards(e.workspace_id)
        if card is None or raw is None:
            raise Refused("unknown_workspace", "no card for this request's workspace")
        try:
            req = certreq.check_cert_request(
                raw, card, me_person_id=self._person, me_device_id=self._client.device_id,
                journal=v.cert_journal(excluding=e.request_id), now_ms=now,
            )  # fmt: skip
            return req, certreq.open_cert_request(req, self._unseal)
        except Refused as r:
            if r.code == "expired":
                v.put(replace(e, state="expired"))
            raise

    def _resume(self, v: JournalView, e: Entry, now: int, skipped: list[Skipped]) -> Pending | None:
        try:
            req, inner = self._reload(v, e, now)
        except Refused as r:
            skipped.append(Skipped(e.request_id, r.code))
            return None
        return self._view(req, inner, e.state, e.challenge)

    # -- acting on one request (by id only) ---------------------------------------------------------------------------

    def _entry(self, v: JournalView, request_id: object) -> Entry:
        rid = _rid({"request_id": request_id})
        e = None if rid is None else v.get(rid)
        if e is None or e.tombstone:
            raise QueueRefused("not_this_request", str(rid))
        return e

    def _ready(self, v: JournalView, e: Entry, pk: Any) -> tuple[CertRequest, CertInner]:
        """Before any person-key operation, under the lock: live, unexpired by the local clock, ``pk`` is the pinned
        owner's key, and the request re-checked and re-opened from the stored bytes."""
        if not e.live:
            raise QueueRefused("already_decided" if e.state in DECIDED else "not_live", e.request_id)
        now = self._now()
        if now >= e.expires_ms:
            v.put(replace(e, state="expired"))
            raise QueueRefused("expired", e.request_id)
        pub = pk.pk_pub
        if type(pub) is not bytes or not hmac.compare_digest(pub, self._pk):
            raise QueueRefused("person_mismatch", e.request_id)
        try:
            return self._reload(v, e, now)
        except Refused as r:
            raise QueueRefused(r.code, e.request_id) from None

    def challenge(self, request_id: str, pk: Any) -> Pending:
        """Sign the challenge (§8.4 step 3) with ``primary_nonce`` drawn now, journal it, then post it. A request
        already challenged is never signed again: the stored bytes are posted again. ``pk`` is the entered person-key
        source. Returns the challenged snapshot (show :attr:`Pending.cert_code`)."""
        with self._journal.transaction() as v:
            e = self._entry(v, request_id)
            if e.state != "challenged":
                req, inner = self._ready(v, e, pk)
                if not self._policy(inner):
                    raise QueueRefused("scope_exceeded", e.request_id)
                signed = certreq.make_cert_challenge(self._pk, pk.signer("sig_cert_challenge"), req, inner,
                                                     self._nonce())  # fmt: skip
                e = v.put(replace(e, state="challenged", challenge_cj=canon.dumps(signed)))
                view = self._view(req, inner, "challenged", signed)
            else:
                try:
                    view = self._view(*self._reload(v, e, self._now()), "challenged", e.challenge)
                except Refused as r:
                    raise QueueRefused(r.code, e.request_id) from None
        challenge = e.challenge
        assert challenge is not None
        self._client.post_challenge(e.request_id, challenge)
        return view

    def sign(self, request_id: str, pk: Any) -> dict[str, Any]:
        """Sign the device certificate (§8.4 step 4) after the human confirmed ``cert_code``: the inner re-opened from
        the stored request, which must name the device and both keys of the **stored** challenge; ``scopes_max`` as
        requested and within policy; ``expires_ms: null`` (v1.7); the label sealed under the label key derived from
        ``PK``. Journaled ``decided`` before ``PUT /v2/devices/{d}``; returns the certificate (hand it to R6 with
        ``relay.pair_cert``). A request already signed returns (and re-uploads) the stored certificate."""
        with self._journal.transaction() as v:
            e = self._entry(v, request_id)
            if e.state == "challenged":
                req, inner = self._ready(v, e, pk)
                if not self._policy(inner):
                    raise QueueRefused("scope_exceeded", e.request_id)
                ch = e.challenge
                assert ch is not None
                co = ch["o"]
                if (co.get("request_id"), co.get("device_id"), co.get("dk_sig_pub"), co.get("dk_kx_pub")) != (
                    req.request_id, inner.device_id, crypto.b64u(inner.dk_sig_pub), crypto.b64u(inner.dk_kx_pub)
                ):  # fmt: skip
                    raise QueueRefused("not_this_request", e.request_id)
                person = crypto.person_id(self._pk)
                device = bytes.fromhex(inner.device_id)
                label_sealed = certs.seal_device_label(pk.derive_label_key(), person, device, inner.label)
                cert = certs.make_device_cert(
                    self._pk, pk.signer("sig_device_cert"), dk_sig_pub=inner.dk_sig_pub, dk_kx_pub=inner.dk_kx_pub,
                    label_sealed=label_sealed, created_ms=self._now(), expires_ms=None,
                    scopes_max=list(inner.scopes_max),
                )  # fmt: skip
                e = v.put(replace(e, state="decided", decision="sign", decided_cj=canon.dumps(cert)))
            elif e.state == "verified":
                raise QueueRefused("not_challenged", e.request_id)
            elif e.decision != "sign":
                raise QueueRefused("already_decided" if e.decision else "not_live", e.request_id)
        return self._upload(e)

    def reject(self, request_id: str, pk: Any) -> dict[str, Any]:
        """Sign the rejection (P3-D11) and post it, journaled ``decided`` first. A request already rejected re-posts
        the stored rejection; one already signed is refused."""
        with self._journal.transaction() as v:
            e = self._entry(v, request_id)
            if e.live:
                req, _inner = self._ready(v, e, pk)
                signed = certreq.make_cert_rejection(self._pk, pk.signer("sig_cert_rejection"), req)
                e = v.put(replace(e, state="decided", decision="reject", decided_cj=canon.dumps(signed)))
            elif e.decision != "reject":
                raise QueueRefused("already_decided" if e.decision else "not_live", e.request_id)
        return self._upload(e)

    def _upload(self, e: Entry) -> dict[str, Any]:
        """Send the stored decision; a 2xx makes it ``uploaded``, a permanent refusal (:data:`PERMANENT_UPLOAD`)
        ``upload_failed`` (then re-raised); anything else leaves it ``decided`` for :meth:`finish_uploads`."""
        decided = e.decided
        assert decided is not None and e.decision is not None
        if e.state != "decided":
            return decided
        try:
            if e.decision == "sign":
                self._client.register_device(decided, self._pk)
            else:
                self._client.post_rejection(e.request_id, decided)
        except OwnerRefused as r:
            if r.code in PERMANENT_UPLOAD:
                self._settle(e.request_id, "upload_failed")
            raise
        self._settle(e.request_id, "uploaded")
        return decided

    def _settle(self, request_id: str, state: str) -> None:
        with self._journal.transaction() as v:
            cur = v.get(request_id)
            if cur is not None and cur.state == "decided":
                v.put(replace(cur, state=state))

    def finish_uploads(self) -> dict[str, str]:
        """Resend every ``decided`` record's stored bytes (a crash between the decision and the relay's 2xx), each on
        its own: a refusal of one never stops the others. Returns ``{request_id: outcome}`` with outcome
        ``uploaded``, ``upload_failed`` (permanent) or ``retry`` (transient: still ``decided``)."""
        with self._journal.transaction() as v:
            todo = [e for e in v.entries() if e.state == "decided" and not e.tombstone]
        out: dict[str, str] = {}
        for e in todo:
            try:
                self._upload(e)
                out[e.request_id] = "uploaded"
            except OwnerRefused as r:
                out[e.request_id] = "upload_failed" if r.code in PERMANENT_UPLOAD else "retry"
            except OwnerError:
                out[e.request_id] = "retry"
        return out

    def close(self, request_id: str, state: str) -> None:
        """End a request locally: a live one ``cancelled`` (its offer was cancelled), ``expired`` or ``superseded``;
        a ``decided`` one whose upload will not succeed ``upload_failed`` (whatever ``state`` names)."""
        if state not in ("cancelled", "expired", "superseded"):
            raise ValueError("state")
        with self._journal.transaction() as v:
            e = self._entry(v, request_id)
            if e.state == "decided":
                v.put(replace(e, state="upload_failed"))
            elif e.live:
                v.put(replace(e, state=state))
            else:
                raise QueueRefused("not_live", e.request_id)
