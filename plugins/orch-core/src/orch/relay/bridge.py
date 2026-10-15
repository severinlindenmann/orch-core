"""R2: the bridge loop (contract 11A.4, 11A.5 "Poll and serve", "Epochs", "Chunk POST outcomes"; proto §8.6).

:class:`Bridge` is the responder core, synchronous and testable without threads or network: it takes one polled
request envelope through proto §8.6 in its normative order, enters every refusal after the tag as ``answered``
before it is sent, admits a request (T1), runs it on the workspace's worker thread through :class:`HostPort`, stores
the answer (T2) and POSTs it. :class:`BridgeLoop` adds the threads (poll, responder, two job threads, aux) and the
worker contract R5 needs: ``start()``, ``stop()``, ``join(timeout) -> bool``.

Order (§8.6, with 11A.4/11A.5):

1. shape (drop): size, ORB2 header (magic, version 2, direction 1, flags only STREAM, the suite), this workspace, the
   mailbox rid (and the poll item's device) equal to the header's;
2. key (drop): epoch 0 is a pairing (handed to R6; until R6 exists: dropped), an epoch above the current one or
   whose key is not held;
3. tag (drop);
4. device (a sealed refusal within the host-wide budget of 10 per minute, else dropped): ``revoked``, ``not_member``
   (not in R6's current member list, or its certificate does not verify under its person's key now, or the F1 state
   does not associate exactly this pair), ``bad_signature`` (``dk_sig`` of the certificate);
5. replay: a known ``(device, rid)``: same bytes -> the stored chunk bytes again (no answer while the rid is still
   admitted); other bytes -> the four ``rid_conflict`` cases of 11A.4; 5b quota: ``busy`` (recorded, seq not used),
   or drop above the host-wide record cap;
6. framing: strict meta with a string ``op``, else ``malformed`` (recorded, seq not used);
7. seq window -> ``stale_sequence`` (``high``); from here the seq is consumed;
8. ±300 s -> ``stale_timestamp`` (``host_ms``);
9. epoch != current -> ``stale_epoch`` (``epoch``), sealed under the request's own old key;
10. a stream (R2 serves none) -> ``forbidden_scope``; then the start fence -> ``already_done {status: unknown}``;
11. :func:`orch.relay.framing.check_meta` (``auth`` -> ``req.invalid``, grant shape -> ``grant.secret_in_args``, op
    table -> ``forbidden_scope``, idem rules), the scope in BOTH the certificate and the member list, the write's
    events (:func:`orch.relay.submit.check_events`), then T1 and the run.
"""

from __future__ import annotations

import binascii
import collections
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from orch import canon, crypto
from orch.identity.certs import verify_cert
from orch.identity.errors import Refused as CertRefused

from . import framing, submit, wire
from .framing import OPS, Identity, Refusal
from .http import Transport, TransportError
from .keys import BridgeKeys, KeyMissing, Member, R6View
from .retry import RelayRefused
from .rids import Rid, RidStore, seq_accept
from .session import Fenced, Session, SessionError
from .sign import RelaySigner

__all__ = ["OPS", "Bridge", "BridgeLoop", "HostPort", "Item", "PostResult"]

log = logging.getLogger("orch.relay.bridge")

T = TypeVar("T")
WINDOW_MS = 300_000
RID_RETENTION_MS = 900_000  # the quota's window (proto constants.bridge.rid_retention_ms)
PER_DEVICE = 1024
MAX_RECORDS = 16_384
BUDGET = 10
BUDGET_WINDOW_MS = 60_000
CHUNK_ROOM = wire.MAX_CHUNK - wire.OVERHEAD  # plaintext of one chunk
QUEUE_ITEMS, QUEUE_BYTES = 16, 4 << 20
POLL_WAIT_S, POLL_DEADLINE_S, POLL_CAP = 25, 40.0, 4 << 20
POST_DEADLINE_S, POST_CAP = 30.0, 16 * 1024
RENEW_EVERY_S = 15.0
AUX_HOLD_DEADLINE_S = 10.0
JOBS = 2
_PROTO_FIELDS = {"high", "host_ms", "epoch", "status"}


class HostPort(submit.SubmitPort, Protocol):
    """The host side of the relay entry (11A.4), implemented by ``orch/host/relayentry.py`` (R5 wires it). Every
    method runs on the workspace's worker thread (the bridge calls it through ``job``); a host refusal is raised as
    :class:`orch.relay.framing.Refusal` with the §0.6 code."""

    def standing(self, device_id: str, person_id: str) -> str:
        """``ok``, ``revoked`` (F1 or HostPins evidence for exactly this pair), ``not_member`` (the F1 state does
        not associate exactly this pair, or the person is not a current member) or ``unavailable`` (no store)."""
        ...

    def read(self, identity: Identity, op: str, args: dict[str, Any], recheck: Callable[[], bool]) -> dict: ...


@dataclass(frozen=True)
class Item:
    n: int
    route: str
    device: str
    rid: str
    envelope: bytes


@dataclass
class Admission:
    header: wire.Header
    identity: Identity
    request: framing.Request
    member: Member
    log: str | None = None
    events: list[dict] | None = None


PostResult = str  # posted | retry | expired | exhausted | conflict | stale_epoch | dropped
Poster = Callable[[str, str, bytes], PostResult]
Job = Callable[[Callable[[], Any]], Any]


def _now_ms() -> int:
    return int(time.time() * 1000)


class Bridge:
    """The responder core. ``post(device_hex, rid_hex, envelope) -> PostResult``; ``job(fn)`` runs ``fn`` on the
    workspace worker thread and returns its result (R5: ``RelayWorker.submit``)."""

    def __init__(
        self,
        *,
        workspace_id: str,
        rids: RidStore,
        keys: BridgeKeys,
        signer: RelaySigner,
        r6: R6View,
        port: HostPort,
        job: Job,
        post: Poster,
        now_ms: Callable[[], int] = _now_ms,
        alarm: Callable[[str, str], None] = lambda code, detail: None,
    ) -> None:
        self.ws_hex, self.ws = workspace_id, bytes.fromhex(workspace_id)
        self.rids, self.keys, self.signer, self.r6, self.port = rids, keys, signer, r6, port
        self.job, self.post, self.now_ms, self.alarm = job, post, now_ms, alarm
        self._budget: collections.deque[int] = collections.deque()
        self._start_ms: int | None = rids.start_ms()

    # -- start-up (11A.4 recovery, start fence) -------------------------------------------------------------------

    def start(self) -> None:
        """Record the start fence, replay persisted chunks first, then answer every admitted rid: a read gets
        ``already_done {status: unknown}``; a write is rolled forward (:func:`submit.recover`) and answered from its
        outcome. Never rebuilds an answer that has a chunk."""
        now = self.now_ms()
        self.rids.set_start_ms(now)
        self._start_ms = now
        self.flush()
        self.job(lambda: submit.recover(self.port, self.rids, self._identity_of))
        for rec in self.rids.admitted():
            h = self._header_of(rec)
            if rec.kind == "read" or rec.idem is None:
                self._answer(rec, h, [framing.refusal_plaintext("already_done", status="unknown")], refusal=True)
                continue
            built = self._identity_of(rec.device, rec.epoch)
            if built is None:
                self._answer(rec, h, [framing.refusal_plaintext("already_done", status="unknown")], refusal=True)
                continue
            try:
                got = self.job(lambda b=built, r=rec: self.port.lookup(b[0], r.idem))
            except Refusal:
                got = {"state": "unknown"}
            self._answer_outcome(rec, h, got)
        self.flush()

    def _answer_outcome(self, rec: Rid, h: wire.Header, got: dict[str, Any]) -> None:
        state = got.get("state")
        if state == "committed" and isinstance(got.get("result"), dict):
            self._answer(rec, h, framing.answer_plaintexts(got["result"], room=CHUNK_ROOM), refusal=False)
        elif state in ("failed", "partial") and isinstance(got.get("error"), dict):
            err = got["error"]
            code = err.get("code") if isinstance(err.get("code"), str) else "internal"
            fields: dict[str, Any] = {"retryable": bool(err.get("retryable", False))}
            if isinstance(got.get("seqs"), list):
                fields["seqs"] = got["seqs"]
            self._answer(rec, h, [framing.refusal_plaintext(code, **fields)], refusal=True)
        else:  # pending or unknown: the device reconciles through outcome.get (11A.4)
            self._answer(rec, h, [framing.refusal_plaintext("already_done", status="unknown")], refusal=True)

    def _header_of(self, rec: Rid) -> wire.Header:
        """A request header rebuilt from the rid record: enough to address the answer (device, rid, epoch)."""
        return wire.Header(
            2, 1, 0, wire.SUITE, self.ws, bytes.fromhex(rec.device), bytes.fromhex(rec.rid), bytes(16), rec.seq, 0,
            rec.epoch, bytes(16),
        )  # fmt: skip

    def _identity_of(self, device: str, epoch: int) -> tuple[Identity, Callable[[], bool]] | None:
        ml = self.r6.member_list()
        m = ml.members.get(device) if ml is not None else None
        if ml is None or m is None:
            return None
        ident = Identity(self.ws_hex, "d_" + device, "p_" + m.person_id, _cert_digest(m), epoch, ml.list_hash)
        return ident, self._recheck(device, m.person_id, None)

    # -- receive: proto §8.6 steps 1 to 11, up to T1 --------------------------------------------------------------

    def receive(self, item: Item) -> Admission | None:
        """One polled request. Returns the :class:`Admission` to run (T1 done), or ``None`` (dropped, replayed or
        refused; a refusal is durable before this returns, so the item may be acknowledged)."""
        env = item.envelope
        # 1. shape: nothing here may answer
        if type(env) is not bytes or not wire.OVERHEAD <= len(env) <= wire.MAX_REQUEST:
            return None
        try:
            h, body, sig = wire.parse_envelope(env, direction=1)
        except wire.WireError:
            return None
        hb = env[: wire.HEADER_LEN]
        if h.workspace != self.ws or h.rid.hex() != item.rid or (item.device and h.device.hex() != item.device):
            return None
        # 2. the key (epoch 0: pairing, R6's; until R6 exists dropped and never answered)
        if h.epoch == 0 or h.epoch > self.keys.current or not self.keys.holds(h.epoch):
            return None
        # 3. the tag
        try:
            pt = self.keys.open_request(hb, body)
        except (KeyMissing, wire.WireError):
            return None
        device, rid = h.device.hex(), h.rid.hex()
        digest = "sha256:" + crypto.sha256(env).hex()
        now = self.now_ms()
        known = self.rids.get(device, rid)
        # 4. the device
        ml = self.r6.member_list()
        member = ml.members.get(device) if ml is not None else None
        code, cert_o = self._device_check(device, member, hb, body, sig, now)
        if code == "unavailable":
            return None  # the store cannot say now: silence, the device resends
        if code is not None:
            if known is not None:
                if known.kind == "unverified" and known.req_digest == digest:
                    self._replay(known)
                return None
            if not self._spend_budget(now):
                return None
            self._refuse(h, digest, now, code, kind="unverified", window=None)
            return None
        assert member is not None and cert_o is not None and ml is not None
        # 5. a known request id
        if known is not None:
            self._known(known, h, digest, now)
            return None
        # 5b. quota
        if self.rids.live(now - RID_RETENTION_MS) >= MAX_RECORDS:
            return None
        if self.rids.live(now - RID_RETENTION_MS, device) >= PER_DEVICE:
            self._refuse(h, digest, now, "busy", window=None)
            return None
        # 6. framing
        try:
            meta, data = wire.parse_plaintext(pt)
            if type(meta.get("op")) is not str:
                raise wire.WireError("malformed", "op")
        except wire.WireError:
            self._refuse(h, digest, now, "malformed", window=None)
            return None
        # 7. the seq window (consumed from here on)
        high, bitmap = self.rids.window(device)
        ok, high2, bitmap2 = seq_accept(high, bitmap, h.seq)
        if not ok:
            self._refuse(h, digest, now, "stale_sequence", window=None, high=high)
            return None
        window = (high2, bitmap2)
        # 8. time
        if abs(now - h.ts_ms) > WINDOW_MS:
            self._refuse(h, digest, now, "stale_timestamp", window=window, host_ms=now)
            return None
        # 9. epoch
        if h.epoch != self.keys.current:
            self._refuse(h, digest, now, "stale_epoch", window=window, epoch=self.keys.current)
            return None
        # 10. stream: R2 serves no stream, so a stream (flag or id) is never this device's
        if h.flags & wire.FLAG_STREAM or h.stream != bytes(16):
            self._refuse(h, digest, now, "forbidden_scope", window=window)
            return None
        # the start fence (11A.4): stamped before this worker started and unknown here: recorded, not run
        if self._start_ms is not None and h.ts_ms < self._start_ms:
            self._refuse(h, digest, now, "already_done", window=window, status="unknown")
            return None
        # 11. the request itself, before any column is resolved
        try:
            req = framing.check_meta(meta, data)
            if not _scoped(req.spec.scope, member.scopes, cert_o.get("scopes_max")):
                raise Refusal("forbidden_scope")
            ident = Identity(
                self.ws_hex, "d_" + device, "p_" + member.person_id, _cert_digest(member), h.epoch, ml.list_hash,
                req.rd,
            )  # fmt: skip
            adm = Admission(h, ident, req, member)
            if req.spec.kind == "write":
                dk = crypto.unb64u(cert_o["dk_sig_pub"], crypto.PUB_LEN)
                adm.log, adm.events = submit.check_events(req.op, req.args, ident, dk, idem=req.idem or "", now_ms=now)
        except Refusal as r:
            self._refuse(h, digest, now, r.code, window=window, **_host_fields(r))
            return None
        # T1
        self.rids.admit(
            device=device, rid=rid, first_ms=now, req_digest=digest, epoch=h.epoch, seq=h.seq,
            principal=ident.principal, idem=req.idem, kind=req.spec.kind, window=window,
            submission=None if adm.events is None else (req.op, adm.log or "", adm.events),
        )  # fmt: skip
        return adm

    def _device_check(
        self, device: str, member: Member | None, hb: bytes, body: bytes, sig: bytes, now: int
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Step 4 in its order: revoked, not_member, bad_signature. ``(None, cert)`` when it passes."""
        if member is None:
            return "not_member", None
        standing = self.job(lambda: self.port.standing("d_" + device, "p_" + member.person_id))
        if standing in ("revoked", "unavailable"):
            return standing, None
        try:
            o = verify_cert(member.cert, member.pk_pub, now)
        except CertRefused as e:
            return ("revoked" if e.code == "revoked" else "not_member"), None
        if o.get("device_id") != device or o.get("person_id") != member.person_id or standing != "ok":
            return "not_member", None
        try:
            pub = crypto.unb64u(o["dk_sig_pub"], crypto.PUB_LEN)
        except crypto.EncodingError:
            return "not_member", None
        if not crypto.verify(pub, sig, wire.bridge_signed_bytes(hb, body)):
            return "bad_signature", None
        return None, o

    def _recheck(self, device: str, person: str, scope: str | None) -> Callable[[], bool]:
        """Steps 1 to 3 and the scopes again, inside the op's own job on the worker thread (11A.4): a device removed,
        revoked, expired or reduced in scope between receipt and run is refused there."""

        def recheck() -> bool:
            ml = self.r6.member_list()
            m = ml.members.get(device) if ml is not None else None
            if m is None or m.person_id != person:
                return False
            try:
                o = verify_cert(m.cert, m.pk_pub, self.now_ms())
            except CertRefused:
                return False
            if scope is not None and not _scoped(scope, m.scopes, o.get("scopes_max")):
                return False
            return self.port.standing("d_" + device, "p_" + person) == "ok"

        return recheck

    def _spend_budget(self, now: int) -> bool:
        while self._budget and now - self._budget[0] >= BUDGET_WINDOW_MS:
            self._budget.popleft()
        if len(self._budget) >= BUDGET:
            return False
        self._budget.append(now)
        return True

    # -- refusals and replays -------------------------------------------------------------------------------------

    def _seal(self, h: wire.Header, idx: int, plaintext: bytes, *, last: bool, refusal: bool) -> bytes:
        flags = (wire.FLAG_LAST if last else 0) | (wire.FLAG_REFUSAL if refusal else 0)
        return self.keys.seal_response(
            self.signer, request=h, idx=idx, flags=flags, ts_ms=self.now_ms(), plaintext=plaintext
        )

    def _refuse(
        self,
        h: wire.Header,
        digest: str,
        now: int,
        code: str,
        *,
        window: tuple[int, int] | None,
        kind: str = "refusal",
        **fields: Any,
    ) -> None:
        """Seal the refusal, enter it as ``answered`` (and the window when the step consumed the seq), then POST."""
        chunk = self._seal(h, 0, framing.refusal_plaintext(code, **fields), last=True, refusal=True)
        self.rids.record_refusal(
            device=h.device.hex(), rid=h.rid.hex(), first_ms=now, req_digest=digest, epoch=h.epoch, seq=h.seq,
            kind=kind, chunk=chunk, window=window,
        )  # fmt: skip
        log.info("relay request refused code=%s device=%s rid=%s", code, h.device.hex(), h.rid.hex())
        self._post_rid(h.device.hex(), h.rid.hex())

    def _replay(self, rec: Rid) -> None:
        """The exact stored chunk bytes again (proto §8.6 step 5, P3-D27); nothing is re-sealed or re-stamped."""
        if rec.state != "answered":
            return  # admitted: running, or waiting for recovery: no answer now (no interim already_done)
        self.rids.reopen_posting(rec.device, rec.rid)
        self._post_rid(rec.device, rec.rid)

    def _known(self, rec: Rid, h: wire.Header, digest: str, now: int) -> None:
        if rec.state == "tombstone":
            return  # its bytes are gone, and the relay's deadline for this rid passed long ago
        if rec.req_digest == digest:
            self._replay(rec)
            return
        # rid_conflict: the same (device, rid) with other bytes never runs (11A.4, the four cases)
        if any(c.state != "stored" for c in rec.chunks):
            self._replay(rec)  # (a) an attempted original: re-POST those exact bytes
        elif rec.state == "admitted":
            return  # (b) no answer now; the original's answer goes out when it exists
        elif rec.chunks and all(c.data is not None for c in rec.chunks) and self.keys.holds(rec.epoch):
            self._post_rid(rec.device, rec.rid)  # (c) the original answer, frozen from now on
        else:  # (d) the original is superseded; a write stays retrievable through outcome.get(idem)
            chunk = self._seal(h, 0, framing.refusal_plaintext("rid_conflict"), last=True, refusal=True)
            self.rids.replace_unattempted(rec.device, rec.rid, [(0, h.epoch, chunk)])
            self._post_rid(rec.device, rec.rid)

    # -- run and answer -------------------------------------------------------------------------------------------

    def run(self, adm: Admission) -> None:
        """Run an admitted request on the worker thread, then T2 and POST. A failure that is not a host refusal
        leaves a write ``admitted`` (recovery decides it); a read gets a sealed ``internal``."""
        req, ident = adm.request, adm.identity
        recheck = self._recheck(adm.member.device_id, adm.member.person_id, req.spec.scope)
        rec = self.rids.get(ident.device_id[2:], adm.header.rid.hex())
        if rec is None or rec.state != "admitted":
            return
        try:
            if req.spec.kind == "write":
                assert adm.log is not None and adm.events is not None and req.idem is not None
                result = self.job(
                    lambda: self.port.submit(ident, req.idem or "", req.op, adm.log or "", adm.events or [], recheck)
                )
            elif req.spec.backend == "outcome.get":
                result = self._outcome_get(adm, recheck)
            else:
                result = self.job(lambda: self.port.read(ident, req.spec.backend, req.args, recheck))
            pts, refusal = framing.answer_plaintexts(result, room=CHUNK_ROOM), False
        except Refusal as r:
            pts, refusal = [framing.refusal_plaintext(r.code, **_host_fields(r))], True
        except Exception:  # noqa: BLE001 - logged by class only (11A.9 redaction)
            log.error("relay run failed device=%s rid=%s", rec.device, rec.rid)
            if req.spec.kind == "write":
                return
            pts, refusal = [framing.refusal_plaintext("internal", retryable=True)], True
        if adm.header.epoch != self.keys.current:  # rotated while it ran: no new data under the old epoch
            pts, refusal = [framing.refusal_plaintext("already_done", status="unknown")], True
        self._answer(rec, adm.header, pts, refusal=refusal)

    def _outcome_get(self, adm: Admission, recheck: Callable[[], bool]) -> dict[str, Any]:
        """``outcome.get`` (O1, orch-52 final 15 Oct 2026): ``pending`` for an idem this device has admitted and not
        answered, and for an idem (= event id) without an outcome record until its ULID time +
        :data:`framing.PENDING_NO_RECORD_MS`; only after that is "no record" definite."""
        idem = adm.request.args.get("idem")
        mine = self.rids.by_idem(adm.member.device_id, idem) if type(idem) is str else []
        if any(r.state == "admitted" for r in mine):
            return _pending()
        try:
            return self.job(lambda: self.port.read(adm.identity, "outcome.get", adm.request.args, recheck))
        except Refusal as r:
            if r.code == "outcome.unknown_idem" and type(idem) is str and framing.IDEM_RE.fullmatch(idem):
                if self.now_ms() < framing.ulid_ms(idem) + framing.PENDING_NO_RECORD_MS:
                    return _pending()
            raise

    def _answer(self, rec: Rid, h: wire.Header, pts: list[bytes], *, refusal: bool) -> None:
        chunks = [(i, self._seal(h, i, pt, last=i == len(pts) - 1, refusal=refusal)) for i, pt in enumerate(pts)]
        if self.rids.answer(rec.device, rec.rid, chunks):  # T2, before any POST
            self._post_rid(rec.device, rec.rid)

    # -- posting --------------------------------------------------------------------------------------------------

    def _post_rid(self, device: str, rid: str) -> None:
        rec = self.rids.get(device, rid)
        if rec is None:
            return
        for c in rec.chunks:
            if c.state not in ("stored", "attempted") or c.data is None:
                continue
            if c.state == "stored":
                self.rids.mark(device, rid, c.idx, "attempted")  # committed before the first byte of the POST
            got = self.post(device, rid, c.data)
            if got == "posted":
                self.rids.mark(device, rid, c.idx, "posted")
                continue
            if got in ("expired", "exhausted", "conflict", "dropped"):
                self.rids.mark(device, rid, c.idx, got)
                if got == "conflict":
                    self.alarm("chunk_conflict", f"device={device} rid={rid} idx={c.idx}")
                if got == "dropped":
                    for later in rec.chunks:
                        if later.idx > c.idx and later.state in ("stored", "attempted"):
                            self.rids.mark(device, rid, later.idx, "dropped")
            elif got == "stale_epoch":
                self.rotate_rid(device, rid)
            return  # retry (or a terminal answer): later chunks wait; "retry" keeps this one attempted

    def flush(self) -> None:
        """POST every persisted chunk not yet posted (recovery first, then after transient failures)."""
        seen: set[tuple[str, str]] = set()
        for device, rid, _ in self.rids.unposted():
            if (device, rid) not in seen:
                seen.add((device, rid))
                self._post_rid(device, rid)

    def rotate(self) -> None:
        """An R6 rotation signal: every open answer under an older epoch is closed (11A.5 "Epochs")."""
        for rec in self.rids.answered_open():
            if rec.epoch < self.keys.current:
                self.rotate_rid(rec.device, rec.rid)

    def rotate_rid(self, device: str, rid: str) -> None:
        """An open answer ends with ``LAST | REFUSAL stale_epoch`` at its next unused index; one with no chunk
        attempted becomes ``already_done {status: unknown}``. An attempted chunk is never replaced. Only refusals are
        sealed under the old epoch, and only while its key is held."""
        rec = self.rids.get(device, rid)
        if rec is None or not self.keys.holds(rec.epoch):
            return
        attempted = [c.idx for c in rec.chunks if c.state != "stored"]
        if attempted and any(c.state != "stored" and c.idx == max(attempted) and _is_last(c.data) for c in rec.chunks):
            return  # the attempted part already ends the answer
        h = self._header_of(rec)
        if attempted:
            idx = max(attempted) + 1
            pt = framing.refusal_plaintext("stale_epoch", epoch=self.keys.current)
        else:
            idx, pt = 0, framing.refusal_plaintext("already_done", status="unknown")
        chunk = self._seal(h, idx, pt, last=True, refusal=True)
        self.rids.replace_unattempted(device, rid, [(idx, rec.epoch, chunk)])
        self._post_rid(device, rid)


def _is_last(data: bytes | None) -> bool:
    try:
        return data is not None and bool(wire.parse_header(data[: wire.HEADER_LEN]).flags & wire.FLAG_LAST)
    except wire.WireError:
        return False


def _pending() -> dict[str, Any]:
    return {
        "data": {"state": "pending"}, "key": None, "seq": 0, "cursor": 0, "hints": [], "lines": [],
        "duplicate": False, "exit": 0, "head": None,
    }  # fmt: skip


def _host_fields(r: Refusal) -> dict[str, Any]:
    """The documented fields a refusal may carry: §14's for the proto codes; ``retryable``, ``seqs`` and (for
    ``ticket.key_not_next``) the advisory ``next_key`` for a host code. Never a message (it could echo request text)."""
    if "." not in r.code:
        return {k: v for k, v in r.fields.items() if k in _PROTO_FIELDS}
    out: dict[str, Any] = {"retryable": bool(r.fields.get("retryable", False))}
    seqs = r.fields.get("seqs")
    if type(seqs) is list and all(type(s) is int for s in seqs):
        out["seqs"] = seqs
    nk = r.fields.get("next_key")
    if r.code == "ticket.key_not_next" and type(nk) is str and len(nk) <= 64:
        out["next_key"] = nk  # advisory
    return out


def _scoped(scope: str, listed: tuple[str, ...], cert_scopes: object) -> bool:
    return scope in listed and type(cert_scopes) is list and scope in cert_scopes


def _cert_digest(m: Member) -> str:
    try:
        return "sha256:" + crypto.sha256(canon.dumps(dict(m.cert))).hex()
    except canon.JcsError:
        return "sha256:" + "0" * 64


# -- the threads ------------------------------------------------------------------------------------------------------


class BridgeLoop:
    """The poll, responder, job and aux threads of one workspace (11A.4, 11A.5, 11A.7 shutdown). R5's
    ``RelayWorker`` owns one; its contract is ``start()``, ``stop()`` and ``join(timeout) -> bool``.

    ``conns()`` makes one transport per thread (11A.7); the :class:`Session` is shared (one per state directory)."""

    def __init__(
        self,
        bridge: Bridge,
        session: Session,
        *,
        conn: Callable[[], Transport],
        workspace_id: str,
        mono: Callable[[], float] = time.monotonic,
        id8: str | None = None,
    ) -> None:
        self.bridge, self.session, self.ws = bridge, session, workspace_id
        self._conn, self._mono = conn, mono
        self.id8 = id8 or workspace_id[:8]
        self._stop = threading.Event()
        self._queue: queue.Queue[Item | None] = queue.Queue()
        self._queued_bytes = 0
        self._qlock = threading.Condition()
        self._jobs: queue.Queue[Admission | None] = queue.Queue(maxsize=JOBS)
        self._threads: list[threading.Thread] = []
        self._conns: list[Transport] = []
        self._last_poll = 0.0
        self._highest = 0
        self._started = False
        self.error: BaseException | None = None

    # -- lifecycle ----------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            raise RuntimeError("started twice")
        self._started = True
        self._spawn("poll", self._poll_main)

    def stop(self) -> None:
        """11A.7 shutdown: stop event, ``shutdown(SHUT_RDWR)`` on every live connection, wake every queue."""
        self._stop.set()
        for c in list(self._conns):
            try:
                c.shutdown()
            except Exception:  # noqa: BLE001 - best effort
                pass
        self._queue.put(None)
        with self._qlock:
            self._qlock.notify_all()
        for _ in range(JOBS):
            try:
                self._jobs.put_nowait(None)
            except queue.Full:
                pass

    def join(self, timeout: float) -> bool:
        """True when every relay thread ended within ``timeout`` (seconds, shared)."""
        end = self._mono() + timeout
        for t in list(self._threads):
            t.join(max(0.0, end - self._mono()))
        alive = [t.name for t in self._threads if t.is_alive()]
        if alive:
            log.error("relay_thread_stuck: %s", ",".join(alive))
        return not alive

    def _spawn(self, role: str, fn: Callable[[], None]) -> None:
        t = threading.Thread(target=self._guard(fn), name=f"orch-relay-{role}-{self.id8}", daemon=True)
        self._threads.append(t)
        t.start()

    def _guard(self, fn: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                fn()
            except Fenced as e:
                self.error = e
                self.stop()
            except SessionError as e:
                self.error = e
                self.stop()
            except BaseException as e:  # noqa: BLE001 - a thread never dies silently; the worker stops
                if not self._stop.is_set():
                    log.error("relay thread %s failed: %s", fn, type(e).__name__)
                    self.error = e
                self.stop()

        return run

    def _new_conn(self) -> Transport:
        c = self._conn()
        self._conns.append(c)
        return c

    # -- poll ---------------------------------------------------------------------------------------------------

    def _poll_main(self) -> None:
        conn = self._new_conn()
        self.session.hold()  # the holder generation first (R1: preflight, takeover rules)
        self.bridge.post = self._poster(self._new_conn())
        self.bridge.start()
        self._spawn("resp", self._resp_main)
        for i in range(JOBS):
            self._spawn(f"job{i}", self._job_main)
        self._spawn("aux", self._aux_main)
        while not self._stop.is_set():
            with self._qlock:
                while not self._stop.is_set() and (
                    self._queue.qsize() >= QUEUE_ITEMS or self._queued_bytes >= QUEUE_BYTES
                ):
                    self._qlock.wait(1.0)
            if self._stop.is_set():
                return
            self._last_poll = self._mono()
            after = self.bridge.rids.cursor()
            try:
                obj = self.session.call(
                    "GET", f"/v2/workspaces/{self.ws}/host/poll?after={after}&wait={POLL_WAIT_S}",
                    "GET /v2/workspaces/{ws}/host/poll", holder=True, cap=POLL_CAP, deadline_s=POLL_DEADLINE_S,
                    keys={"items", "notices", "next"}, transport=conn,
                )  # fmt: skip
            except (TransportError, RelayRefused) as e:
                log.info("relay poll failed: %s", type(e).__name__)
                self._stop.wait(1.0)
                continue
            for item in _items(obj):
                if item.n <= self._highest:
                    continue  # already queued (non-consuming poll)
                self._highest = item.n
                with self._qlock:
                    self._queued_bytes += len(item.envelope)
                self._queue.put(item)

    # -- responder and jobs ---------------------------------------------------------------------------------------

    def _resp_main(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                return
            with self._qlock:
                self._queued_bytes -= len(item.envelope)
                self._qlock.notify_all()
            adm = None
            if item.route == "bridge":
                adm = self.bridge.receive(item)
            # every outcome above is durable (dropped, refused, replayed or admitted at T1): acknowledge in order
            self.bridge.rids.set_cursor(max(item.n, self.bridge.rids.cursor()))
            if adm is not None:
                self._jobs.put(adm)  # at most JOBS in flight: put blocks while both job threads are busy

    def _job_main(self) -> None:
        while not self._stop.is_set():
            adm = self._jobs.get()
            if adm is None:
                return
            self.bridge.run(adm)

    # -- aux: lease renewal, retention, re-posts ------------------------------------------------------------------

    def _aux_main(self) -> None:
        last_sweep = 0.0
        while not self._stop.wait(1.0):
            if self._mono() - self._last_poll >= RENEW_EVERY_S:
                self.session.hold(deadline_s=AUX_HOLD_DEADLINE_S)
                self._last_poll = self._mono()
            if self._mono() - last_sweep >= 60.0:
                self.bridge.rids.sweep(self.bridge.now_ms())
                last_sweep = self._mono()
            self.bridge.flush()

    def _poster(self, conn: Transport) -> Poster:
        lock = threading.Lock()

        def post(device: str, rid: str, envelope: bytes) -> PostResult:
            with lock:  # one transport, used by the threads that post
                try:
                    self.session.call(
                        "POST", f"/v2/workspaces/{self.ws}/bridge/{rid}", "POST /v2/workspaces/{ws}/bridge/{rid}",
                        holder=True, raw=envelope, request_cap=wire.MAX_CHUNK, cap=POST_CAP,
                        deadline_s=POST_DEADLINE_S, keys=set(), transport=conn,
                    )  # fmt: skip
                    return "posted"
                except TransportError:
                    return "retry"
                except RelayRefused as e:
                    return classify_post(e)

        return post


def classify_post(e: RelayRefused) -> PostResult:
    """The chunk POST outcomes of 11A.5."""
    if e.status == 410:
        return "expired"
    if e.status == 409 and e.code == "retry_exhausted":
        return "exhausted"
    if e.status == 409 and e.code == "chunk_conflict":
        return "conflict"
    if e.status == 409 and e.code == "stale_epoch":
        return "stale_epoch"
    if e.status == 403:
        return "dropped"
    return "retry"  # 429, 503 and the rest: 11A.7 backoff, same bytes


def _items(obj: dict[str, Any]) -> list[Item]:
    """The poll page's items, shape-checked; a malformed item is skipped (logged by class only)."""
    out = []
    items = obj.get("items")
    if type(items) is not list:
        return out
    for it in items:
        try:
            if type(it) is not dict or set(it) != {"n", "route", "device", "rid", "offer", "envelope"}:
                raise ValueError("item shape")
            n = it["n"]
            if type(n) is not int or n < 1 or it["route"] not in ("bridge", "pair"):
                raise ValueError("item fields")
            for k in ("device", "rid"):
                if type(it[k]) is not str or len(it[k]) != 32:
                    raise ValueError("item id")
            env = wire.b64_field(it["envelope"], max_len=wire.MAX_REQUEST)
            out.append(Item(n, it["route"], it["device"], it["rid"], env))
        except (ValueError, wire.WireError, binascii.Error):
            log.info("relay poll item skipped (malformed)")
    out.sort(key=lambda i: i.n)
    return out
