"""R2: the bridge loop (contract 11A.4, 11A.5 "Poll and serve", "Epochs", "Chunk POST outcomes"; proto §8.6).

:class:`Bridge` is the responder core, synchronous and testable without threads or network: it takes one polled
request envelope through proto §8.6 in its normative order, enters every refusal after the tag as ``answered``
before it is sent, admits a request (T1), runs it on the workspace's worker thread through :class:`HostPort`, stores
the answer (T2) and POSTs it. :class:`BridgeLoop` adds the threads (poll, responder, two job threads, aux) and the
worker contract R5 needs: ``start()``, ``stop()``, ``join(timeout) -> bool``.

Every chunk is sealed inside :meth:`BridgeKeys.sealing` (G1): one snapshot of the seal state, and the bytes are
committed (T2 or the refusal record) before the span ends. Data is sealed only under the snapshot's committed epoch
with the rotation gate down; anything else becomes a refusal (``already_done {status: unknown}`` for an answer that
may not be sealed), built by :meth:`keys._Sealer.seal_refusal` from the closed refusal fields.

Order (§8.6, with 11A.4/11A.5):

1. shape (drop): size, ORB2 header (magic, version 2, direction 1, flags only STREAM, the suite), this workspace, the
   mailbox rid (and the poll item's device) equal to the header's;
2. key (drop): epoch 0 is a pairing (handed to R6; until R6 exists: dropped), an epoch whose key is neither held nor
   burned; a BURNED epoch above the committed one (C5, proto §8.5 G2b) is opened only to answer its signed
   ``stale_epoch`` at step 9 (every other outcome under it is a drop);
3. tag (drop);
4. device (a sealed refusal within the host-wide budget of 10 per minute, else dropped): ``revoked``, ``not_member``
   (not in R6's current member list, or its certificate does not verify under its person's key now, or the F1 state
   does not associate exactly this pair), ``bad_signature`` (``dk_sig`` of the certificate). The certificate and the
   signature are checked before the host's standing; an unsigned request reaches the store only within the budget.
   Recorded keyed ``u:<device>`` (R2-11): a verified request of that device never matches it by rid alone; only the
   SAME bytes (same digest, so the device's own signature) replay it, so a definite refusal stays definite (D2-2).
   Once its bytes are swept a step-4 refusal is silence (D2-1);
5. replay: a known ``(device, rid)``: same bytes -> the stored chunk bytes again (no answer while the rid is still
   admitted; ``already_done {status: unknown}`` at the next index once the bytes were swept, ruling D); other bytes ->
   the four ``rid_conflict`` cases of 11A.4; the same bytes recorded as a step-4 refusal ``u:<device>`` replay that
   refusal and never run (D2-2); 5b quota: the row caps (drop), then ``busy`` (recorded within
   ``PER_DEVICE + BUSY_ALLOWANCE``, seq not used) above ``PER_DEVICE``;
6. framing: strict meta with a string ``op``, else ``malformed`` (recorded, seq not used);
7. seq window -> ``stale_sequence`` (``high``); from here the seq is consumed;
8. ±300 s -> ``stale_timestamp`` (``host_ms``);
9. epoch != the committed one, or the rotation gate up -> ``stale_epoch``: an old epoch names the committed one; a
   burned or gated one names the pending epoch (else ``EHW + 1``); sealed under the request's own key;
10. a stream (R2 serves none) -> ``forbidden_scope``; then the start fence -> ``already_done {status: unknown}``;
11. :func:`orch.relay.framing.check_meta` (``auth`` -> ``req.invalid``, grant shape -> ``grant.secret_in_args``, op
    table -> ``forbidden_scope``, idem rules), the scope in BOTH the certificate and the member list, the write's
    events (:func:`orch.relay.submit.check_events`; freshness is the host's, after dedup: ruling A), ruling B (the
    same idem admitted or running under another rid -> ``pending``, never a second run); a READ over the stored-answer
    byte budgets -> ``busy`` (seq not used; writes are never refused for bytes, D2-3); then T1 and the run.

Robustness (D2-1, D2-7): one poll item, one job or one aux step that raises is logged (bounded) and acknowledged;
it never stops the loop, except the session's own ``Fenced``/``SessionError`` (a takeover, a lost lease), which stop
the worker from any thread (N3-1). The invariant (N4-1): **a fence or session error from any relay POST stops the
worker; it is never counted as an item failure.** It is enforced in one place, :attr:`Bridge.post`: every poster is
wrapped so that a ``SessionError`` becomes :class:`WorkerStop`, a ``BaseException`` that no ``except Exception`` of
an item, a rid or an aux step can catch (only :meth:`BridgeLoop._guard` does), and the poster refuses every later
POST without a request. A refusal code outside the closed table is sealed as ``internal``; a signer
refusal is silence for that chunk. An admitted read is bounded in age (:data:`READ_ADMITTED_MS`, N3-2).
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
from orch.net.http import Transport, TransportError
from orch.wsproto import wire
from orch.wsproto.sign import RelaySigner, SignerRefused

from . import framing, submit
from .framing import OPS, PROTO_CODES, PROTO_FIELDS, Identity, Refusal
from .keys import BridgeKeys, KeyMissing, Member, R6View, SealRefused
from .retry import RelayRefused
from .rids import Rid, RidStore, seq_accept
from .session import Fenced, LoginHeldOff, Session, SessionError

__all__ = ["OPS", "PROTO_CODES", "Bridge", "BridgeLoop", "HostPort", "Item", "PostResult", "WorkerStop"]

log = logging.getLogger("orch.relay.bridge")

T = TypeVar("T")
WINDOW_MS = 300_000
RID_RETENTION_MS = 900_000  # the quota's window (proto constants.bridge.rid_retention_ms)
RID_KEEP_MS = 24 * 3600 * 1000  # a rid record lives 24 h (rids.RID_KEEP_MS): the day caps count over it
PER_DEVICE = 1024  # admitted or refused per device per 15 min before busy
BUSY_ALLOWANCE = 64  # busy (and other) refusals recorded above PER_DEVICE; beyond: dropped (ref busy_allowance)
MAX_RECORDS = 16_384  # host-wide per 15 min
DEVICE_DAY_ROWS = 16_384  # per device per 24 h (R2-3: one device cannot fill the host-wide retained cap)
MAX_RETAINED_ROWS = 262_144  # host-wide non-tombstone rows (24 h)
DEVICE_CHUNK_BYTES = 16 << 20  # stored answer bytes + 2 MiB per admitted read, per device (R2-4)
CHUNK_BYTES = 128 << 20  # the same host-wide
#: D2-3: a device past its fair share (its bytes with this answer above FAIR_CHUNK_BYTES) may fill the host only up to
#: SHARED_CHUNK_BYTES; the rest is reserved for devices within their share, so starving every read on the host takes
#: at least 6 heavy (96 MiB / 16 MiB) plus 8 light (32 MiB / 4 MiB) member devices, and writes are never refused for
#: bytes (their answers are one small chunk, bounded by the row caps)
FAIR_CHUNK_BYTES = 4 << 20
SHARED_CHUNK_BYTES = CHUNK_BYTES - (32 << 20)
BUDGET = 10
BUDGET_WINDOW_MS = 60_000
CHUNK_ROOM = wire.MAX_CHUNK - wire.OVERHEAD  # plaintext of one chunk
QUEUE_ITEMS, QUEUE_BYTES = 16, 4 << 20
POLL_WAIT_S, POLL_DEADLINE_S, POLL_CAP = 25, 40.0, 4 << 20
SEEN_POLL_PAUSE_S = 0.25  # a poll that returned only already-queued items waits this long before the next
POST_DEADLINE_S, POST_CAP = 30.0, 16 * 1024
RENEW_EVERY_S = 15.0
AUX_HOLD_DEADLINE_S = 10.0
RENEW_BACKOFF_S = 5.0  # a refused renewal (no Retry-After) is tried again after this
RENEW_MAX_BACKOFF_S = 15.0  # never later: the lease is 40 s and a renewal is due every 15 s
AUX_TICK_S = 1.0  # the aux thread's cadence: a failed renewal is tried again at the next tick, until the lease lapses
JOBS = 2
HANDOFF_S = 0.25  # the responder's and job threads' queue wait: they see stop() within this
REPLAY_EVERY_MS, REPLAY_MAX, REPLAY_TRACKED = 10_000, 8, 4096
#: an admitted read older than this can no longer store an answer the relay takes (its 900 s deadline, plus a margin):
#: it no longer counts against the byte budgets, and :meth:`Bridge.expire_reads` closes it (review N3-2)
READ_ADMITTED_MS = RID_RETENTION_MS + 60_000
ALARM_CLASSES = 64  # distinct (step, exception class) pairs that each raised one alarm; beyond: logged only
#: R2-6: a write whose run failed with something that is not a refusal is retried (recover_one) after these delays;
#: after the last one it is answered ``already_done {status: unknown}`` (B: outcome.get settles it). 370 s in all,
#: inside the pending bound (framing.PENDING_NO_RECORD_MS), so ``pending`` never outlives it.
STUCK_BACKOFF_MS = (10_000, 60_000, 300_000)
#: a chunk POST answered "retry" (429, 503, a transport error) waits before the aux flush posts it again: 1 s doubling
#: to 60 s per rid (11A.7 backoff), so a relay answering 429 forever cannot make the host re-POST every second
POST_BACKOFF_MS, POST_BACKOFF_MAX_MS = 1_000, 60_000


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
_UNKNOWN: dict[str, Any] = {"status": "unknown"}


def _now_ms() -> int:
    return int(time.time() * 1000)


class WorkerStop(BaseException):  # noqa: N818 - not an error of the item: the end of the worker
    """The session ended during a relay POST (``Fenced``: a takeover; any ``SessionError``: a lost lease). N4-1's
    invariant: a fence or session error from any relay POST stops the worker and is never counted as an item
    failure. A ``BaseException``, so no ``except Exception`` (one item, one rid, one aux step) can swallow it; only
    :meth:`BridgeLoop._guard` catches it, and stops the worker with :attr:`cause` as its error."""

    def __init__(self, cause: SessionError) -> None:
        super().__init__(cause)
        self.cause = cause


class _FenceStops:
    """A poster with N4-1's invariant: a ``SessionError`` from it is raised as :class:`WorkerStop`, and once it is
    recorded every POST through it is refused the same way without a request, whatever caught the first. A thread
    already inside the poster may still send one (F5-2); after ``Fenced`` the session itself refuses it."""

    def __init__(self, fn: Poster) -> None:
        self._fn = fn
        self._ended: SessionError | None = None

    def __call__(self, device: str, rid: str, envelope: bytes) -> PostResult:
        if self._ended is not None:
            raise WorkerStop(self._ended)
        try:
            return self._fn(device, rid, envelope)
        except SessionError as e:
            self._ended = e
            raise WorkerStop(e) from e


def _fence_stops(fn: Poster) -> Poster:
    """``fn`` wrapped by :class:`_FenceStops` (once)."""
    return fn if isinstance(fn, _FenceStops) else _FenceStops(fn)


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
        convenience_gate: Callable[[], bool] | None = None,
    ) -> None:
        self.ws_hex, self.ws = workspace_id, bytes.fromhex(workspace_id)
        #: §11A.17.4 P5: the host's lock-free dev gate (``orch.host.devmode.gated``); ``None``: convenience refused
        self.convenience_gate = convenience_gate
        self.rids, self.keys, self.signer, self.r6, self.port = rids, keys, signer, r6, port
        self.job, self.post, self.now_ms, self.alarm = job, post, now_ms, alarm
        self._budget: collections.deque[int] = collections.deque()
        self._start_ms: int | None = rids.start_ms()
        #: (device, rid) -> last ms a replay re-opened it for posting: at most one per 10 s (P3-D27's spacing; a
        #: bounded LRU, so eviction only ever loosens the spacing); the 8 per rid are counted in the rid row itself
        #: (``reopens``, N3-3), so no eviction and no restart lets a replay re-POST or append past them
        self._reopened: collections.OrderedDict[tuple[str, str], int] = collections.OrderedDict()
        #: (device, rid) -> (attempts made, due ms) of writes whose run failed without a refusal (R2-6)
        self._stuck: dict[tuple[str, str], tuple[int, int]] = {}
        self._stuck_lock = threading.Lock()
        #: (device, rid) -> (delay ms, due ms) of a rid whose last POST was "retry"; one entry per open rid
        self._post_wait: dict[tuple[str, str], tuple[int, int]] = {}
        #: guards _reopened and _post_wait (the responder, job and aux threads all change them; review N1)
        self._maps_lock = threading.Lock()
        #: rids being POSTed now: a second _post_rid of the same rid (job thread and aux flush) waits its turn (N1)
        self._posting: set[tuple[str, str]] = set()

    # -- start-up (11A.4 recovery, start fence) -------------------------------------------------------------------

    @property
    def post(self) -> Poster:
        """The chunk poster; whatever is assigned is wrapped by :func:`_fence_stops` (N4-1: the one place)."""
        return self._post

    @post.setter
    def post(self, fn: Poster) -> None:
        self._post = _fence_stops(fn)

    def start(self) -> None:
        """Record the start fence, replay persisted chunks first, then answer every admitted rid: a read gets
        ``already_done {status: unknown}``; a write is rolled forward (:func:`submit.recover`) and answered from its
        outcome (under the seal gate: data only under the committed epoch). Never rebuilds an answer that has a
        chunk."""
        now = self.now_ms()
        self.rids.set_start_ms(now)
        self._start_ms = now
        self.flush()
        self.job(lambda: submit.recover(self.port, self.rids, self._identity_of))
        for rec in self.rids.admitted():
            self._settle(rec)
        self.flush()

    def _settle(self, rec: Rid) -> None:
        """Answer an admitted rid after recovery: a read ``already_done {status: unknown}``; a write from its
        outcome."""
        h = self._header_of(rec)
        if rec.kind == "read":
            self._close_read(rec)
            return
        if rec.idem is None:
            self._answer_refusal(rec, h, "already_done", **_UNKNOWN)
            return
        built = self._identity_of(rec.device, rec.epoch, None)
        if built is None:
            self._answer_refusal(rec, h, "already_done", **_UNKNOWN)
            return
        try:
            got = self.job(lambda b=built, r=rec: self.port.lookup(b[0], r.idem))
        except Exception:  # noqa: BLE001 - a refusal or anything else: the device settles it through outcome.get
            got = {"state": "unknown"}
        self._answer_outcome(rec, h, got)

    def _close_read(self, rec: Rid) -> None:
        """An admitted read that will not run (again): ``already_done {status: unknown}`` (the device re-issues it);
        when nothing can be sealed for it any more (its epoch key is gone), a tombstone, so it holds no byte reserve
        and no admitted row for ever (review N3-2). A resend then gets silence; its relay deadline is long past."""
        if not self._answer_refusal(rec, self._header_of(rec), "already_done", **_UNKNOWN):
            if self.rids.abandon(rec.device, rec.rid):
                log.info("relay read closed without an answer device=%s rid=%s", rec.device, rec.rid)

    def expire_reads(self) -> None:
        """The aux thread's bound on admitted reads (N3-2): one admitted past :data:`READ_ADMITTED_MS` (its run
        failed silently, or it waits on a key that is gone) is closed by :meth:`_close_read`."""
        for rec in self.rids.stale_reads(self.now_ms() - READ_ADMITTED_MS):
            try:
                self._close_read(rec)
            except Exception:  # noqa: BLE001 - one rid never stops the rest (D2-1)
                log.error("relay read expiry failed device=%s rid=%s", rec.device, rec.rid)

    def sweep(self) -> None:
        """The aux thread's retention step: stale admitted reads first, then the rid store's retention."""
        self.expire_reads()
        self.rids.sweep(self.now_ms())

    def _answer_outcome(self, rec: Rid, h: wire.Header, got: dict[str, Any]) -> None:
        state = got.get("state")
        if state == "committed" and isinstance(got.get("result"), dict):
            self._answer_result(rec, h, got["result"])
        elif state in ("failed", "partial") and isinstance(got.get("error"), dict):
            err = got["error"]
            code = err.get("code") if isinstance(err.get("code"), str) else "internal"
            # the stored socket Error's closed per-code detail (contract v1.9, #499): ticket.key_not_next {next_key};
            # _refusal_fields keeps next_key for that code only, so no other code's detail reaches the device
            detail = err.get("detail")
            nk = detail.get("next_key") if isinstance(detail, dict) else None
            r = Refusal(code, seqs=got.get("seqs"), next_key=nk)
            self._answer_refusal(rec, h, **_refusal_fields(r, effects_possible=True))
        else:  # pending or unknown: the device reconciles through outcome.get (11A.4)
            self._answer_refusal(rec, h, "already_done", **_UNKNOWN)

    def _header_of(self, rec: Rid) -> wire.Header:
        """A request header rebuilt from the rid record: enough to address the answer (device, rid, epoch). A step-4
        record is keyed ``u:<device>``: the header names the device itself (D2-1)."""
        device = rec.device[2:] if rec.device.startswith("u:") else rec.device
        return wire.Header(
            2, 1, 0, wire.SUITE, self.ws, bytes.fromhex(device), bytes.fromhex(rec.rid), bytes(16), rec.seq, 0,
            rec.epoch, bytes(16),
        )  # fmt: skip

    def _identity_of(self, device: str, epoch: int, scope: str | None) -> tuple[Identity, Callable[[], bool]] | None:
        """The identity of an admitted rid and its recheck, steps 1 to 3 AND ``scope`` (the op's, R2-10) again."""
        ml = self.r6.member_list()
        m = ml.members.get(device) if ml is not None else None
        if ml is None or m is None:
            return None
        ident = Identity(self.ws_hex, "d_" + device, "p_" + m.person_id, _cert_digest(m), epoch, ml.list_hash)
        return ident, self._recheck(device, m.person_id, scope)

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
        # 2. the key (epoch 0: pairing, R6's; until R6 exists dropped and never answered); a burned epoch: C5
        if h.epoch == 0:
            return None
        burned = self.keys.burned(h.epoch)
        if not burned and not self.keys.serves(h.epoch):
            return None
        # 3. the tag
        try:
            pt = self.keys.open_request(hb, body)
        except (KeyMissing, wire.WireError):
            return None
        device, rid = h.device.hex(), h.rid.hex()
        digest = "sha256:" + crypto.sha256(env).hex()
        now = self.now_ms()
        # 4. the device
        ml = self.r6.member_list()
        member = ml.members.get(device) if ml is not None else None
        code, cert_o = self._device_check(device, member, hb, body, sig, now, rid=rid)
        if code in ("unavailable", "dropped") or (code is not None and burned):
            return None  # the store cannot say now (silence, the device resends); under a burned key only stale_epoch
        known = self.rids.get(device, rid)
        if code is not None:
            if known is not None:
                return None  # a stored answer is never re-posted to a device that fails step 4 now (J2)
            ukey = "u:" + device  # R2-11: never the verified device's own (device, rid)
            known_u = self.rids.get(ukey, rid)
            if known_u is not None:
                if known_u.req_digest == digest and _has_bytes(known_u):
                    self._replay(known_u)  # swept: silence (step 4 needs no answer; D2-1)
                return None
            if not self._spend_budget(now):
                return None
            self._refuse(h, digest, now, code, window=None, kind="unverified", device_key=ukey)
            return None
        assert member is not None and cert_o is not None and ml is not None
        # 5. a known request id
        if known is not None:
            self._known(known, h, digest, now)
            return None
        # D2-2: these exact bytes were refused at step 4 (a definite answer): replay that refusal, never run them
        known_u = self.rids.get("u:" + device, rid)
        if known_u is not None and known_u.req_digest == digest:
            if _has_bytes(known_u):
                self._replay(known_u)
            return None
        # 5b. quota: the row caps (drop), then busy (recorded within the allowance, seq not used)
        if not self._room(device, now, verified=True):
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
        # 9. epoch: old -> the committed one; burned or gated -> the pending one (else EHW + 1), C5 / G1
        st = self.keys.state()
        if h.epoch != st.current or st.gated:
            target = st.current if h.epoch < st.current and not st.gated else st.stale_target
            self._refuse(h, digest, now, "stale_epoch", window=window, epoch=target)
            return None
        # 10. stream: R2 serves no stream, so a stream (flag or id) is never this device's
        if h.flags & wire.FLAG_STREAM or h.stream != bytes(16):
            self._refuse(h, digest, now, "forbidden_scope", window=window)
            return None
        # the start fence (11A.4): stamped before this worker started and unknown here: recorded, not run
        if self._start_ms is not None and h.ts_ms < self._start_ms:
            self._refuse(h, digest, now, "already_done", window=window, **_UNKNOWN)
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
                adm.log, adm.events = submit.check_events(
                    req.op, req.args, ident, dk, idem=req.idem or "", convenience_gate=self.convenience_gate,
                    convenience_refused=self._convenience_refused,
                )  # fmt: skip
                # ruling B: the same idem admitted or running under another rid: pending, never a second run
                if any(r.state == "admitted" for r in self.rids.by_idem(device, req.idem or "")):
                    raise Refusal("pending")
        except Refusal as r:
            self._refuse(h, digest, now, window=window, **_refusal_fields(r))
            return None
        # D2-3: a read may store a whole answer; over the byte budgets: busy, the seq not used (nothing ran)
        if req.spec.kind == "read" and not self._bytes_ok(device):
            self._refuse(h, digest, now, "busy", window=None)
            return None
        # T1
        self.rids.admit(
            device=device, rid=rid, first_ms=now, req_digest=digest, epoch=h.epoch, seq=h.seq,
            principal=ident.principal, idem=req.idem, kind=req.spec.kind, window=window,
            submission=None if adm.events is None else (req.op, adm.log or "", adm.events),
        )  # fmt: skip
        return adm

    def _convenience_refused(self) -> None:
        """§11A.7 F7: a paired device's verified convenience event refused on column R (a Simulator build against a
        real host or workspace): the alarm ``dev_convenience_refused`` (count, first, last). Never raised at 0."""
        self.alarm("dev_convenience_refused", "a convenience-signed event was refused on column R (op.not_allowed)")

    def _device_check(
        self, device: str, member: Member | None, hb: bytes, body: bytes, sig: bytes, now: int, *, rid: str = ""
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Step 4 in its order: revoked, not_member, bad_signature. ``(None, cert)`` when it passes. The certificate
        and the signature (pure) come first; the host's standing (the store lock) is asked for an unsigned request
        only while the refusal budget could still answer it (R2-11)."""
        if member is None:
            return "not_member", None
        try:
            o: dict[str, Any] | None = verify_cert(member.cert, member.pk_pub, now)
            cert_code = None
        except CertRefused as e:
            o, cert_code = None, ("revoked" if e.code == "revoked" else "not_member")
        signed = False
        if o is not None:
            try:
                pub = crypto.unb64u(o["dk_sig_pub"], crypto.PUB_LEN)
                signed = crypto.verify(pub, sig, wire.bridge_signed_bytes(hb, body))
            except (crypto.EncodingError, KeyError, TypeError):
                signed = False
        if not signed and not self._budget_left(now):
            return "dropped", None  # nothing could be answered: the store is not asked for unsigned input
        if signed and not self._room(device, now, verified=True) and self.rids.get(device, rid) is None:
            return "dropped", None  # a signed device over its row caps: dropped at 5b anyway, never the store lock
        standing = self.job(lambda: self.port.standing("d_" + device, "p_" + member.person_id))
        if standing in ("revoked", "unavailable"):
            return standing, None
        if cert_code is not None:
            return cert_code, None
        assert o is not None
        if o.get("device_id") != device or o.get("person_id") != member.person_id or standing != "ok":
            return "not_member", None
        if not signed:
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

    def _budget_left(self, now: int) -> bool:
        while self._budget and now - self._budget[0] >= BUDGET_WINDOW_MS:
            self._budget.popleft()
        return len(self._budget) < BUDGET

    def _spend_budget(self, now: int) -> bool:
        if not self._budget_left(now):
            return False
        self._budget.append(now)
        return True

    def _room(self, device_key: str, now: int, *, verified: bool) -> bool:
        """The row caps (R2-3), checked before any record: host-wide 15 min and 24 h; per verified device 15 min
        (``PER_DEVICE + BUSY_ALLOWANCE``) and 24 h. Beyond any of them the request is dropped, never recorded."""
        if self.rids.live(now - RID_RETENTION_MS) >= MAX_RECORDS:
            return False
        if self.rids.live(now - RID_KEEP_MS) >= MAX_RETAINED_ROWS:
            return False
        if verified:
            if self.rids.live(now - RID_RETENTION_MS, device_key) >= PER_DEVICE + BUSY_ALLOWANCE:
                return False
            if self.rids.live(now - RID_KEEP_MS, device_key) >= DEVICE_DAY_ROWS:
                return False
        return True

    def _bytes_ok(self, device: str) -> bool:
        """The stored-answer byte budgets of a read (R2-4, D2-3): stored chunk bytes plus one whole answer per
        admitted read, per device and host-wide; past its fair share a device sees only the shared part of the host
        budget, so the reserve stays open to the devices within theirs."""
        reserve, since = framing.RELAY_DEVICE_RESPONSE_BYTES, self.now_ms() - READ_ADMITTED_MS
        mine = self.rids.chunk_bytes(device) + self.rids.admitted_reads(device, since_ms=since) * reserve + reserve
        if mine > DEVICE_CHUNK_BYTES:
            return False
        host = self.rids.chunk_bytes() + self.rids.admitted_reads(since_ms=since) * reserve + reserve
        return host <= (CHUNK_BYTES if mine <= FAIR_CHUNK_BYTES else SHARED_CHUNK_BYTES)

    # -- refusals and replays -------------------------------------------------------------------------------------

    def _refuse(
        self,
        h: wire.Header,
        digest: str,
        now: int,
        code: str,
        *,
        window: tuple[int, int] | None,
        kind: str = "refusal",
        device_key: str | None = None,
        **fields: Any,
    ) -> None:
        """Seal the refusal and enter it as ``answered`` (and the window when the step consumed the seq) inside one
        seal span (G1), then POST. Dropped, never recorded, beyond the row caps (R2-3)."""
        dkey = device_key or h.device.hex()
        if not self._room(dkey, now, verified=kind != "unverified"):
            return
        with self.keys.sealing(self.signer) as s:
            chunk = self._seal_refusal(s, h, 0, code, **fields)
            if chunk is None:
                return
            self.rids.record_refusal(
                device=dkey, rid=h.rid.hex(), first_ms=now, req_digest=digest, epoch=h.epoch, seq=h.seq,
                kind=kind, chunk=chunk, window=window,
            )  # fmt: skip
        log.info("relay request refused code=%s device=%s rid=%s", code, h.device.hex(), h.rid.hex())
        self._post_rid(dkey, h.rid.hex())

    def _replay(self, rec: Rid) -> None:
        """The exact stored chunk bytes again (proto §8.6 step 5, P3-D27); nothing is re-sealed or re-stamped. Once
        the bytes were swept (960 s) while the record lives (24 h), ruling D: ``already_done {status: unknown}`` at
        the next unused index, never silence."""
        if rec.state != "answered":
            return  # admitted: running, or waiting for recovery: no answer now (no interim already_done)
        key, now = (rec.device, rec.rid), self.now_ms()
        with self._maps_lock:
            if now - self._reopened.get(key, -REPLAY_EVERY_MS) < REPLAY_EVERY_MS:
                return  # one per 10 s (P3-D27)
            self._reopened[key] = now
            self._reopened.move_to_end(key)
            while len(self._reopened) > REPLAY_TRACKED:
                self._reopened.popitem(last=False)
        if not self.rids.note_reopen(rec.device, rec.rid, REPLAY_MAX):
            return  # 8 per rid, durable (N3-3): the relay would refuse a ninth re-store anyway
        if rec.chunks and all(c.data is None for c in rec.chunks):
            if rec.device.startswith("u:"):
                return  # a swept step-4 refusal: silence (D2-1)
            idx = max(c.idx for c in rec.chunks) + 1
            with self.keys.sealing(self.signer) as s:
                chunk = self._seal_refusal(s, self._header_of(rec), idx, "already_done", **_UNKNOWN)
                if chunk is None:
                    return  # the epoch's key is gone: nothing can be sealed for this rid any more
                self.rids.append_chunk(rec.device, rec.rid, idx, rec.epoch, chunk)
        else:
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
        elif rec.chunks and all(c.data is not None for c in rec.chunks) and self.keys.serves(rec.epoch):
            self._post_rid(rec.device, rec.rid)  # (c) the original answer, frozen from now on
        else:  # (d) the original is superseded; a write stays retrievable through outcome.get(idem)
            self._supersede(rec.device, rec.rid, h, 0, "rid_conflict")

    def _supersede(self, device: str, rid: str, h: wire.Header, idx: int, code: str, **fields: Any) -> None:
        """Replace the never-attempted chunks of ``rid`` with one closing refusal at ``idx``. A chunk that became
        ``attempted`` meanwhile (a concurrent POST, R2-12) is never replaced: the caller re-reads, nothing stops."""
        with self.keys.sealing(self.signer) as s:
            chunk = self._seal_refusal(s, h, idx, code, **fields)
            if chunk is None:
                return
            try:
                self.rids.replace_unattempted(device, rid, [(idx, h.epoch, chunk)])
            except ValueError:
                log.info("relay chunk attempted meanwhile device=%s rid=%s", device, rid)
                return
        self._post_rid(device, rid)

    # -- run and answer -------------------------------------------------------------------------------------------

    def run(self, adm: Admission) -> None:
        """Run an admitted request on the worker thread, then T2 and POST. A failure that is not a host refusal: a
        read gets a sealed ``internal`` (D); a write stays ``admitted`` and is retried (R2-6,
        :meth:`retry_stuck`)."""
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
        except Refusal as r:
            fields = _refusal_fields(r, effects_possible=req.spec.kind == "write", before_bound=self._before(req))
            self._answer_refusal(rec, adm.header, **fields)
            return
        except Exception:  # noqa: BLE001 - logged by class only (11A.9 redaction)
            log.error("relay run failed device=%s rid=%s", rec.device, rec.rid)
            if req.spec.kind == "write":
                self._stick(rec)
                return
            self._answer_refusal(rec, adm.header, "internal")
            return
        if not self._answer_result(rec, adm.header, result) and req.spec.kind == "read":
            self._close_read(rec)  # nothing could be sealed under its epoch (N3-2): never admitted for ever

    def _before(self, req: framing.Request) -> bool:
        """``outcome.unknown_idem`` is bound-aware (ruling C): before the event id's ULID time + the pending bound."""
        idem = req.args.get("idem") if req.spec.backend == "outcome.get" else None
        if type(idem) is not str or not framing.IDEM_RE.fullmatch(idem):
            return False
        return self.now_ms() < framing.ulid_ms(idem) + framing.PENDING_NO_RECORD_MS

    def _outcome_get(self, adm: Admission, recheck: Callable[[], bool]) -> dict[str, Any]:
        """``outcome.get`` (O1, orch-52 final 15 Oct 2026): the host's record first (a terminal outcome is answered
        as such); ``pending`` while this device's rid for the idem is admitted (bounded: :data:`STUCK_BACKOFF_MS`)
        and, without a record, until the event id's ULID time + :data:`framing.PENDING_NO_RECORD_MS`; only after
        that is "no record" definite (``outcome.unknown_idem``, C-review)."""
        idem = adm.request.args.get("idem")
        try:
            got = self.job(lambda: self.port.read(adm.identity, "outcome.get", adm.request.args, recheck))
            data = got.get("data") if isinstance(got, dict) else None
            if not (isinstance(data, dict) and data.get("state") == "pending"):
                return got
            unknown: Refusal | None = None
        except Refusal as r:
            if r.code != "outcome.unknown_idem":
                raise
            unknown = r
        mine = self.rids.by_idem(adm.member.device_id, idem) if type(idem) is str else []
        if any(r.state == "admitted" for r in mine) or unknown is None or self._before(adm.request):
            return _pending()
        raise unknown

    def _answer_result(self, rec: Rid, h: wire.Header, result: Any) -> bool:
        """T2 of a success answer, under the seal gate (G1): data only under the snapshot's committed epoch with the
        gate down; otherwise ``already_done {status: unknown}`` (a write: outcome.get settles it; a read: re-issue).
        An answer that could never be delivered is ``frame.too_large`` (nothing else stored). ``True`` when an answer
        was stored (T2)."""
        try:
            pts = framing.answer_plaintexts(result, room=CHUNK_ROOM)
        except Refusal as r:
            return self._answer_refusal(rec, h, **_refusal_fields(r))
        with self.keys.sealing(self.signer) as s:
            try:
                if not s.data_ok(h.epoch):
                    ts = self.now_ms()
                    chunks = [(0, s.seal_refusal(request=h, idx=0, ts_ms=ts, code="already_done", **_UNKNOWN))]
                else:
                    n = len(pts)
                    chunks = [
                        (i, s.seal_response(request=h, idx=i, last=i == n - 1, ts_ms=self.now_ms(), plaintext=pt))
                        for i, pt in enumerate(pts)
                    ]
            except (SealRefused, KeyMissing, SignerRefused):
                return False  # the key is gone (or the signer refused): nothing can be sealed for this rid
            stored = self.rids.answer(rec.device, rec.rid, chunks)  # T2, inside the seal span, before any POST
        if stored:
            self._post_rid(rec.device, rec.rid)
        return stored

    def _seal_refusal(self, s: Any, h: wire.Header, idx: int, code: str, **fields: Any) -> bytes | None:
        """``s.seal_refusal`` for this request at ``idx``. ``None`` when nothing may be sealed (the gate, the key, the
        signer). A code or field outside the closed refusal table (a host code the table does not know) is sealed as
        ``internal`` in the same context, never raised into the loop (D2-7)."""
        try:
            return s.seal_refusal(request=h, idx=idx, ts_ms=self.now_ms(), code=code, **fields)
        except (SealRefused, KeyMissing, SignerRefused):
            return None
        except ValueError:
            log.error("relay refusal outside the closed refusal table: sealed as internal")
        try:
            effects = bool(fields.get("effects_possible"))
            return s.seal_refusal(request=h, idx=idx, ts_ms=self.now_ms(), code="internal", effects_possible=effects)
        except (SealRefused, KeyMissing, ValueError):
            return None

    def _answer_refusal(self, rec: Rid, h: wire.Header, code: str, **fields: Any) -> bool:
        """T2 of a one-chunk refusal answer; ``True`` when it was stored."""
        with self.keys.sealing(self.signer) as s:
            chunk = self._seal_refusal(s, h, 0, code, **fields)
            if chunk is None:
                return False
            stored = self.rids.answer(rec.device, rec.rid, [(0, chunk)])
        if stored:
            self._post_rid(rec.device, rec.rid)
        return stored

    # -- R2-6: a write whose run failed without a refusal -----------------------------------------------------------

    def _stick(self, rec: Rid) -> None:
        with self._stuck_lock:
            attempts, _ = self._stuck.get((rec.device, rec.rid), (0, 0))
            self._stuck[(rec.device, rec.rid)] = (attempts, self.now_ms() + STUCK_BACKOFF_MS[min(attempts, 2)])

    def retry_stuck(self) -> None:
        """The aux thread's bounded retry (R2-6): each due write is rolled forward once more
        (:func:`submit.recover_one`) and answered from its outcome; after the last delay it is answered
        ``already_done {status: unknown}`` (B), so an idem never stays admitted (``pending``) beyond the bound."""
        now = self.now_ms()
        with self._stuck_lock:
            due = [(k, v) for k, v in self._stuck.items() if v[1] <= now]
        for (device, rid), (attempts, _) in due:
            rec = self.rids.get(device, rid)
            if rec is None or rec.state != "admitted":
                with self._stuck_lock:
                    self._stuck.pop((device, rid), None)
                continue
            try:
                self.job(lambda r=rec: submit.recover_one(self.port, self.rids, self._identity_of, r))
            except Exception:  # noqa: BLE001 - still failing: once more later, or the definite end
                log.error("relay write retry failed device=%s rid=%s", device, rid)
                with self._stuck_lock:
                    if attempts + 1 < len(STUCK_BACKOFF_MS):
                        self._stuck[(device, rid)] = (attempts + 1, now + STUCK_BACKOFF_MS[attempts + 1])
                        continue
                    self._stuck.pop((device, rid), None)
                self._answer_refusal(rec, self._header_of(rec), "already_done", **_UNKNOWN)
                continue
            with self._stuck_lock:
                self._stuck.pop((device, rid), None)
            try:
                self._settle(rec)
            except Exception:  # noqa: BLE001 - never the aux thread's end (lease renewal, flush; D2-7)
                log.error("relay write settle failed device=%s rid=%s", device, rid)

    # -- posting --------------------------------------------------------------------------------------------------

    def _post_rid(self, device: str, rid: str) -> None:
        key = (device, rid)
        with self._maps_lock:
            if key in self._posting:
                return  # another thread posts this rid now (N1); what it leaves open, the aux flush posts later
            self._posting.add(key)
        try:
            self._post_rid_once(device, rid)
        finally:
            with self._maps_lock:
                self._posting.discard(key)

    def _post_rid_once(self, device: str, rid: str) -> None:
        rec = self.rids.get(device, rid)
        if rec is None:
            return
        for c in rec.chunks:
            if c.state not in ("stored", "attempted") or c.data is None:
                continue
            if c.state == "stored":
                self.rids.mark(device, rid, c.idx, "attempted")  # committed before the first byte of the POST
            got = self.post(device, rid, c.data)
            with self._maps_lock:
                if got == "retry":
                    prev = self._post_wait.get((device, rid), (POST_BACKOFF_MS // 2, 0))[0]
                    delay = min(prev * 2, POST_BACKOFF_MAX_MS)
                    self._post_wait[(device, rid)] = (delay, self.now_ms() + delay)
                else:
                    self._post_wait.pop((device, rid), None)
            if got == "retry":
                return  # this one stays attempted: the same bytes again after the delay
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
        now, open_ = self.now_ms(), self.rids.unposted()
        live = set(open_)
        with self._maps_lock:
            for k in [k for k in self._post_wait if k not in live]:  # bounded by the open rids
                self._post_wait.pop(k, None)
            due = [k for k in open_ if (w := self._post_wait.get(k)) is None or w[1] <= now]
        for device, rid in due:
            self._post_rid(device, rid)

    def rotate(self) -> None:
        """An R6 rotation signal: every open answer under an older epoch is closed (11A.5 "Epochs")."""
        for rec in self.rids.answered_open():
            if rec.device.startswith("u:"):
                continue  # a step-4 refusal: one closing chunk already, never data (D2-1)
            if rec.epoch < self.keys.current:
                try:
                    self.rotate_rid(rec.device, rec.rid)
                except Exception:  # noqa: BLE001 - one rid never stops R6's rotation (D2-1)
                    log.error("relay rotation of one rid failed device=%s rid=%s", rec.device, rec.rid)

    def rotate_rid(self, device: str, rid: str) -> None:
        """An open answer ends with ``LAST | REFUSAL stale_epoch`` at its next unused index; one with no chunk
        attempted becomes ``already_done {status: unknown}``. An attempted chunk is never replaced; a chunk that
        becomes attempted meanwhile (R2-12) makes this re-read, at most 3 times. Only refusals, built by the seal
        gate's refusal path, are sealed under the old epoch, and only while its key is held."""
        for _ in range(3):
            rec = self.rids.get(device, rid)
            if rec is None or not self.keys.serves(rec.epoch):
                return
            attempted = [c.idx for c in rec.chunks if c.state != "stored"]
            if attempted and any(
                c.state != "stored" and c.idx == max(attempted) and _is_last(c.data) for c in rec.chunks
            ):
                return  # the attempted part already ends the answer
            h = self._header_of(rec)
            with self.keys.sealing(self.signer) as s:
                if attempted:
                    idx, code, fields = max(attempted) + 1, "stale_epoch", {"epoch": s.state.current}
                else:
                    idx, code, fields = 0, "already_done", dict(_UNKNOWN)
                chunk = self._seal_refusal(s, h, idx, code, **fields)
                if chunk is None:
                    return
                try:
                    self.rids.replace_unattempted(device, rid, [(idx, rec.epoch, chunk)])
                except ValueError:
                    continue  # a chunk became attempted between the read and the transaction: read again
            self._post_rid(device, rid)
            return


def _has_bytes(rec: Rid) -> bool:
    return any(c.data is not None for c in rec.chunks)


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


def _refusal_fields(r: Refusal, *, effects_possible: bool = False, before_bound: bool = False) -> dict[str, Any]:
    """The arguments of :func:`framing.refusal_meta` for ``r``: ``code`` and exactly the documented fields (§14's own
    for a proto code; ``seqs`` only as landed seqs, ``next_key`` only for ``ticket.key_not_next``, and the retry
    context for a host code). Anything else ``r`` carries is dropped; never a message (it could echo request text)."""
    if r.code in PROTO_CODES:
        return {"code": r.code, **{k: v for k, v in r.fields.items() if k in PROTO_FIELDS[r.code]}}
    out: dict[str, Any] = {"code": r.code, "effects_possible": effects_possible, "before_bound": before_bound}
    seqs = r.fields.get("seqs")
    if type(seqs) is list and 0 < len(seqs) <= 1024 and all(type(s) is int and s > 0 for s in seqs):
        out["seqs"] = seqs
    nk = r.fields.get("next_key")
    if r.code == "ticket.key_not_next" and type(nk) is str and 0 < len(nk) <= 64:
        out["next_key"] = nk  # advisory
    reason = r.fields.get("reason")  # H12d-3: only a member of the code's closed set
    if r.code in framing.REASONS and type(reason) is str and reason in framing.REASONS[r.code]:
        req, dec = r.fields.get("required"), r.fields.get("declared")
        if reason != "reader_too_old":
            out["reason"] = reason
        elif all(type(v) is int and 0 <= v < 2**31 for v in (req, dec)):
            out.update(reason=reason, required=req, declared=dec)
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
        pairing: Any = None,
    ) -> None:
        self.bridge, self.session, self.ws = bridge, session, workspace_id
        #: R6c's ``R6Pairing`` (11A.15.2): the pair thread (``handle``, ``completed``, ``notice``, ``forget_below``,
        #: ``start``/``stop``/``join``), started and stopped with this loop; ``None``: pair items are dropped
        self.pairing = pairing
        self._pair_open: set[int] = set()  # pair items handed over and not yet completed (the cursor waits for them)
        self._handed = 0  # every item at or below this was handed over (bridge items: durable; pair items: queued)
        self._ack_lock = threading.Lock()
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
        self._renew_not_before = 0.0
        self._highest = 0
        self._started = False
        self._failures = 0
        self._alarmed: set[tuple[str, str]] = set()
        self._alarms_exhausted = False
        self.error: BaseException | None = None
        #: extra aux steps ``(name, fn)`` run after the built-in ones, each guarded like them (R5's seam for R4's
        #: unpublished consumer, 11A.9: "R4 runs on the aux thread"); appended before :meth:`start`
        self.aux_steps: list[tuple[str, Callable[[], None]]] = []
        #: R4 (11A.9): the ``notices`` object of the last poll, as received (a copy, or ``None``); read only by R4's
        #: consumer to see ``notices.unpublished``. Nothing in the bridge acts on it.
        self.notices: dict[str, Any] | None = None

    # -- lifecycle ----------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            raise RuntimeError("started twice")
        self._started = True
        if self.pairing is not None:
            self.pairing.start()
            self._threads.append(self.pairing.thread)
            self.aux_steps.append(("pair_ack", self._advance))
        self._spawn("poll", self._poll_main)

    def stop(self) -> None:
        """11A.7 shutdown: stop event, ``shutdown(SHUT_RDWR)`` on every live connection, wake every queue. The
        responder and the job threads wait on their queues at most :data:`HANDOFF_S` at a time, so a full job queue
        never keeps one alive (R2-9); admitted rids left queued are recovered at the next start."""
        self._stop.set()
        if self.pairing is not None:
            self.pairing.stop()
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

    def threads(self) -> list[threading.Thread]:
        """The threads started so far (R5: ``host.status.relay`` reports whether any is alive)."""
        return list(self._threads)

    def _spawn(self, role: str, fn: Callable[[], None]) -> None:
        t = threading.Thread(target=self._guard(fn), name=f"orch-relay-{role}-{self.id8}", daemon=True)
        self._threads.append(t)
        t.start()

    def _guard(self, fn: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                fn()
            except WorkerStop as e:  # a POST met the session's end (N4-1), on whatever thread posted
                self.error = e.cause
                self.stop()
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
            except (TransportError, RelayRefused, LoginHeldOff) as e:  # a login hold-off is transient (as publish.py)
                log.info("relay poll failed: %s", type(e).__name__)
                self._stop.wait(1.0)
                continue
            notices = obj.get("notices")
            self.notices = dict(notices) if isinstance(notices, dict) else None  # R4 reads it (read-only here)
            if self.pairing is not None:
                ids = _cert_request_ids(obj)
                if ids:
                    self.pairing.notice(ids)
            fresh = False
            for item in _items(obj):
                if item.n <= self._highest:
                    continue  # already queued (non-consuming poll)
                fresh = True
                with self._qlock:
                    full = self._queue.qsize() >= QUEUE_ITEMS or self._queued_bytes >= QUEUE_BYTES
                if full:
                    break  # the rest is delivered again by the next poll (non-consuming): the queue stays bounded
                self._highest = item.n
                with self._qlock:
                    self._queued_bytes += len(item.envelope)
                self._queue.put(item)
            if not fresh and _items(obj):
                # only items already queued (not yet answered, so the cursor has not moved): the relay answers a poll
                # that has items at once, so without this the loop spins and its own rate limit refuses the host's
                # other calls (membership, checkpoint, baselines)
                self._stop.wait(SEEN_POLL_PAUSE_S)

    # -- responder and jobs ---------------------------------------------------------------------------------------

    def _resp_main(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=HANDOFF_S)
            except queue.Empty:
                continue
            if item is None:
                return
            with self._qlock:
                self._queued_bytes -= len(item.envelope)
                self._qlock.notify_all()
            adm = None
            if item.route == "pair" and self.pairing is not None:
                with self._ack_lock:
                    self._pair_open.add(item.n)
                self.pairing.handle(item.n, item.envelope, item.rid, item.device or None)
            elif item.route == "bridge":
                try:
                    adm = self.bridge.receive(item)
                except (Fenced, SessionError):
                    raise  # the session's own end (a takeover, a lost lease): the worker stops, the item stays (N3-1)
                except Exception as e:  # noqa: BLE001 - one item never stops the loop; it is acknowledged (D2-1)
                    self._item_failed("receive", e)
            # every outcome above is durable (dropped, refused, replayed or admitted at T1): acknowledge in order; a
            # pair item counts once R6 has completed it (its offer transition durable, or a silent drop: 11A.15.2)
            self._ack(item.n)
            while adm is not None and not self._stop.is_set():
                try:  # at most JOBS in flight: waits while both job threads are busy, never past stop()
                    self._jobs.put(adm, timeout=HANDOFF_S)
                    adm = None
                except queue.Full:
                    continue

    def _ack(self, n: int) -> None:
        with self._ack_lock:
            self._handed = max(self._handed, n)
        self._advance()

    def _advance(self) -> None:
        """The cursor ``after`` advances only over completed items: up to the lowest pair item R6 has not completed
        (11A.15.2 ``completed(n)``, P3-D10). Bridge items are complete when handed over (durable at T1)."""
        with self._ack_lock:
            if self.pairing is not None and self._pair_open:
                self._pair_open = {m for m in self._pair_open if not self.pairing.completed(m)}
            target = min(self._handed, min(self._pair_open) - 1) if self._pair_open else self._handed
            if target > self.bridge.rids.cursor():
                self.bridge.rids.set_cursor(target)
                if self.pairing is not None:
                    self.pairing.forget_below(target)

    def _job_main(self) -> None:
        while not self._stop.is_set():
            try:
                adm = self._jobs.get(timeout=HANDOFF_S)
            except queue.Empty:
                continue
            if adm is None or self._stop.is_set():
                return
            try:
                self.bridge.run(adm)
            except (Fenced, SessionError):
                raise  # the worker stops (N3-1); the rid stays admitted and is recovered by the next holder
            except Exception as e:  # noqa: BLE001 - the rid stays admitted: recovered at the next start (D2-7)
                self._item_failed("run", e)

    # -- aux: lease renewal, retention, re-posts ------------------------------------------------------------------

    def _aux_main(self) -> None:
        last_sweep = 0.0
        conn = self._new_conn()  # the renewal's own connection: never queued behind another thread's call (11A.7)
        while not self._stop.wait(AUX_TICK_S):
            if self._mono() - self._last_poll >= RENEW_EVERY_S:
                self._renew_lease(conn)
            if self._mono() - last_sweep >= 60.0:
                last_sweep = self._mono()
                self._aux_step("sweep", self.bridge.sweep)
            self._aux_step("retry", self.bridge.retry_stuck)
            self._aux_step("flush", self.bridge.flush)
            for what, fn in list(self.aux_steps):
                self._aux_step(what, fn)

    def _renew_lease(self, conn: Transport) -> None:
        """Renew the holder lease; a transport failure or a relay refusal other than ``fenced`` never ends the aux
        thread. The renewal is idempotent (``{gen, take_over: false}``), so a failed attempt is retried at once on a
        fresh connection (a failed request closes its connection; the relay closes an idle keep-alive after a few
        seconds, so the first request after 15 s of silence on a reused connection can fail), then at every tick
        until the lease would lapse. ``Fenced`` (and any other ``SessionError``) still stops the worker."""
        if self._mono() < self._renew_not_before:
            return
        for attempt in (1, 2):
            try:
                self.session.renew(deadline_s=AUX_HOLD_DEADLINE_S, transport=conn)
            except TransportError as e:
                if self._stop.is_set():
                    return
                if attempt == 2:
                    self._item_failed("renew", e)
                continue  # at once, once more: the failed request closed the connection, the next one opens a new one
            except (RelayRefused, LoginHeldOff) as e:
                # the relay's own answer (5xx, 429 with Retry-After) or a login hold-off (60 s after a bad signature,
                # transient like publish.py's renewal): not hammered; tried again after its Retry-After (else
                # RENEW_BACKOFF_S), never later than the lease margin allows
                if not self._stop.is_set():
                    self._item_failed("renew", e)
                    after = e.retry_after if isinstance(e, RelayRefused) else None
                    delay = float(after) if type(after) is int and after > 0 else RENEW_BACKOFF_S
                    self._renew_not_before = self._mono() + min(delay, RENEW_MAX_BACKOFF_S)
                return
            self._last_poll = self._mono()
            self._renew_not_before = 0.0
            return

    def _aux_step(self, what: str, fn: Callable[[], None]) -> None:
        """One aux step; an exception is logged (bounded) and the aux thread goes on: lease renewal never stops for
        a retention or posting failure (D2-7). The session's own errors (fenced, lost) still stop the worker."""
        try:
            fn()
        except (Fenced, SessionError):
            raise
        except Exception as e:  # noqa: BLE001 - logged by class only
            self._item_failed(what, e)

    def _item_failed(self, what: str, e: BaseException) -> None:
        """A bounded log line (the first 10, then every 1000th) and one alarm per class of failure: the step and
        the exception class, at most :data:`ALARM_CLASSES` of them (N3-1: a harmless flush error never hides a
        poisoned-item crash loop)."""
        cls = (what, type(e).__name__)
        with self._qlock:
            self._failures += 1
            n = self._failures
            first = cls not in self._alarmed and len(self._alarmed) < ALARM_CLASSES
            if first:
                self._alarmed.add(cls)
            # the first new class past the cap raises one alarm of its own, so the cap never hides silently (N4-3)
            exhausted = not first and cls not in self._alarmed and not self._alarms_exhausted
            if exhausted:
                self._alarms_exhausted = True
        if n <= 10 or n % 1000 == 0 or exhausted:
            log.error("relay %s failed (%d so far): %s", what, n, type(e).__name__)
        if first:
            self.bridge.alarm("relay_item_failed", f"{what}: {type(e).__name__}")
        elif exhausted:
            self.bridge.alarm("relay_alarm_classes_exhausted", f"{what}: {type(e).__name__}")

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
                except (TransportError, LoginHeldOff):
                    return "retry"  # a login hold-off is transient: the same bytes again later (11A.7 backoff)
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


def _cert_request_ids(obj: dict[str, Any]) -> list[str]:
    """``notices.cert_requests``: the request ids the relay says moved (a wake for the pair thread; it re-reads each
    request itself and trusts nothing here)."""
    notices = obj.get("notices")
    ids = notices.get("cert_requests") if type(notices) is dict else None
    if type(ids) is not list:
        return []
    return [i for i in ids[:64] if type(i) is str and len(i) == 32 and all(c in "0123456789abcdef" for c in i)]


def _items(obj: dict[str, Any]) -> list[Item]:
    """The poll page's items, shape-checked; a malformed item is skipped (logged by class only)."""
    out: list[Item] = []
    items = obj.get("items")
    if type(items) is not list:
        return out
    skipped = 0
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
            skipped += 1
    if skipped:  # one line per page, never one per item (a relay-sized log flood otherwise)
        log.info("relay poll items skipped (malformed): %d", skipped)
    out.sort(key=lambda i: i.n)
    return out
