"""What the host does with a request envelope (spec §6.1, normative order), and with its answer.

Silence for anything a party without K_ws could have produced; a sealed, host-signed refusal for everything after the
tag verified. Then:

    check(envelope, mailbox id)   steps 1-9 up to "record": accept (recorded, no outcome yet), replay, refuse or drop
    authorize(accepted)           step 9 after the record: route scope (an injected hook), assertion or lease, run
    finish(rids, head, body)      the outcome of a run, stored before the response is sent
    seal_chunk / seal_refusal     the host-signed, sealed response chunks

Durability: the sequence state is persisted before the record, and the record before anything runs or any refusal
after the signature verified is sent. A crash between the two leaves the sequence consumed and no record, so the same
bytes are refused and never run; a crash after the record leaves "no outcome", which is answered `already_done` with
status `unknown` and never runs again.

Streams, leases, open challenges, parked requests and the refusal budgets are in memory: a host restart closes every
stream, ends every lease and forgets every challenge, which only ever refuses more.

Authorisation is decided once, on one reading, and checked again right before anything runs:

- check() reads the registry once (a snapshot with its generation); the decision (Verdict) carries that entry, its
  generation, the device's scope, the host clock at the decision and, for a run a grant allowed, when the grant ends.
- authorize() decides on that same entry, after still_authorized() confirmed it is still the current one; it never
  reads the scope or key a second time.
- Every registry change that affects authorisation (added, scope, revocation, key) moves the generation on, under the
  registry lock its readers take, so a decision taken before it is no longer authorised.
- A record only becomes "running" once the whole decision, scope and assertion included, passed; before that it has
  no outcome, and either way a replay is answered `already_done` / `unknown`, never run.
- revoke() and set_scope() mark the device's records that are not finished as refused, and finish() never replaces
  such a refusal: a request already running then answers the refusal, not its result.
- The dispatcher MUST call still_authorized(run) immediately before it runs anything, and must not run when it is
  False (revoked, rescoped, key replaced, the kill switch, or the fresh or lease grant ended).

What the dispatcher (R3b) must do, beyond calling these functions:

- call Host.still_authorized(run) immediately before running, AND again before sealing every frame of a stream; on
  False it ends the stream with a final chunk carrying the record's stored refusal;
- when finish() returns False, send the record's stored refusal instead of the result (end_run() returns it, and
  stores one first when none was stored yet);
- registry.revoke_everywhere() changes the other workspaces' registries only: their hosts' in-memory streams, leases
  and parked requests stay open until that process checks again (its next still_authorized() or check() sees the
  revocation), so their frames stop at the next frame check, not at once;
- show the owner a damaged registry or request store (Host.health()), not only drop its requests.
"""
from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from filelock import FileLock

from orch.remote.bridge_host import files
from orch.remote.bridge_host.assertion import (CHALLENGE_MS, LEASE_SUBJECT, SUBJECT_KINDS, Issued,
                                               assertion_challenge, verify_assertion)
from orch.remote.bridge_host.budgets import SlidingLimit, fresh_limit, refusal_budget
from orch.remote.bridge_host.envelope import (F_LAST, F_REFUSAL, F_STREAM, MAGIC, MAX_CHUNK, MAX_REQUEST, OVERHEAD,
                                              TO_DEVICE, TO_HOST, VERSION, ZERO_ID, Header, KEY_VERSION, Malformed,
                                              digest, frame, split, unb64u, unframe, unhex)
from orch.remote.bridge_host.keys import InvalidTag, open_sealed, seal
from orch.remote.bridge_host.outcome import CODES, REFUSAL_FIELDS, Verdict, drop, refuse
from orch.remote.bridge_host.pairing import Pairing
from orch.remote.bridge_host.registry import SCOPES, Device, Registry
from orch.remote.bridge_host.replay_store import MAX_REPLAY_BODY, DeviceFull, ReplayStore, StoreFull
from orch.remote.bridge_host.shown import clean_shown
from orch.remote.bridge_host.signatures import from_pem, generate, public_bytes, sign, signed_bytes, to_pem, verify

WINDOW_MS = 300_000  # each way; exactly 300,000 ms is inside
RUNNING = "_running"  # the stored outcome of a request whose whole decision passed and that has not finished
SEQ_WINDOW = 64
LEASE_MS = 15 * 60_000
_DIGEST = re.compile(r"(?:[0-9a-f]{64})?")


@dataclass(frozen=True)
class Requirement:
    """What a route needs, from the injected route hook (R2's tags): a scope, and whether it needs a typing lease
    (input to a terminal stream this device opened) or a fresh assertion over `subject`, which the host builds from
    its own data: {"kind": one of SUBJECT_KINDS, "shown": text, "digest": hex of the full artefact or ""}."""
    scope: str
    assertion: str = "none"  # none | lease | fresh
    subject: dict | None = None


RouteHook = Callable[[dict, bytes], "Requirement | None"]  # (meta, data) -> what the route needs; None: never remote


def seq_accept(high: int, bitmap: int, seq: int) -> tuple[bool, int, int]:
    """The 64-wide anti-replay window (§5.2): bit i of the bitmap set means seq high - i was accepted. Returns
    (accepted, new high, new bitmap); a refused seq leaves the state unchanged."""
    if seq > high:
        shift = seq - high
        return True, seq, ((bitmap << shift) | 1) & ((1 << SEQ_WINDOW) - 1) if shift < SEQ_WINDOW else 1
    i = high - seq
    if seq < 1 or i >= SEQ_WINDOW or bitmap >> i & 1:
        return False, high, bitmap
    return True, high, bitmap | 1 << i


def load_host_key(root: Path, rand: Callable[[int], bytes] = os.urandom):
    """The workspace's host signing key, created on first use (exclusively, owner-only) in its bridge directory.
    files.Damaged when the stored key cannot be read; it is never replaced silently (that would unpin every device)."""
    path = files.ensure_dir(Path(root)) / "host-key.pem"
    raw = files.read(path, 4096)
    if raw is None:
        key = generate(rand)
        try:
            files.create_exclusive(path, to_pem(key))
            return key
        except FileExistsError:
            raw = files.read(path, 4096)
    try:
        return from_pem(raw or b"")
    except (ValueError, TypeError) as e:
        raise files.Damaged("the host key cannot be read") from e


@dataclass
class Host:
    """One workspace's bridge host. `root` is files.bridge_dir(config dir, workspace hex); `clock` returns the host's
    time in ms; `route` is the scope hook; `phone_key(phone_id)` returns the key of an existing phone pairing or
    None; `rp_id` and `origin` are the relay app's host name and origin. `rand(n)` returns n bytes from the operating
    system's CSPRNG (os.urandom, the default) for every salt, nonce, challenge, pairing id and secret: callers MUST NOT
    pass anything else outside tests.

    Every method that reads or changes the in-memory state (check, authorize, finish, offer, approve, reject, revoke,
    set_scope, stop, still_authorized) takes the host lock; call those, never the Pairing or Registry objects directly."""
    workspace: bytes
    k_ws: bytes
    host_key: object
    root: Path
    clock: Callable[[], int]
    route: RouteHook
    phone_key: Callable[[str], bytes | None]
    rp_id: str
    origin: str
    rand: Callable[[int], bytes] = os.urandom
    key_version: int = KEY_VERSION
    max_records: int | None = None
    per_device: int | None = None
    registry: Registry = field(init=False)
    store: ReplayStore = field(init=False)
    pairing: Pairing = field(init=False)
    budget: SlidingLimit = field(default_factory=refusal_budget)
    streams: dict[str, str] = field(default_factory=dict)  # stream rid hex -> the device that opened it
    leases: dict[str, int] = field(default_factory=dict)  # device -> lease end (ms)
    challenges: dict[str, Issued] = field(default_factory=dict)  # challenge hex -> what it allows
    parked: dict[str, Verdict] = field(default_factory=dict)  # R1 rid -> its accepted verdict
    fresh_limits: dict[str, SlidingLimit] = field(default_factory=dict)
    stopped: bool = False

    def __post_init__(self):
        if len(self.workspace) != 16 or len(self.k_ws) != 32:
            raise ValueError("the workspace id is 16 bytes and K_ws 32")
        self.root = files.ensure_dir(Path(self.root))
        self.registry = Registry(self.root, self.workspace)
        caps = {k: v for k, v in (("max_records", self.max_records), ("per_device", self.per_device)) if v}
        self.store = ReplayStore(self.root, **caps)
        self.pairing = Pairing(self.workspace)
        self._lock = FileLock(str(self.root / "host.lock"), timeout=10)

    @property
    def host_pub(self) -> bytes:
        return public_bytes(self.host_key)

    # -- §6.1 steps 1-9 (up to the record) -----------------------------------------------------------------------------

    def check(self, env: bytes, mailbox_id: str) -> Verdict:
        """`mailbox_id` is the cleartext id the mailbox delivered the envelope under."""
        now = self.clock()
        # 1. shape: nothing here needs a key, so nothing here may answer
        if not isinstance(env, bytes) or not OVERHEAD <= len(env) <= MAX_REQUEST:
            return drop("size")
        hb, body, sig = split(env)
        h = Header.decode(hb)
        if h.magic != MAGIC or h.version != VERSION:
            return drop("version")
        if h.direction != TO_HOST or h.flags & ~F_STREAM:
            return drop("direction_or_flags")
        if h.key_version != self.key_version or h.workspace != self.workspace:
            return drop("workspace")
        if mailbox_id != h.rid.hex():
            return drop("mailbox_mismatch")
        # 2. the tag: from here on the sender holds K_ws
        try:
            pt = open_sealed(self.k_ws, hb, body)
        except InvalidTag:
            return drop("tag")
        with self._lock:
            try:
                v = self._check(env, h, hb, body, sig, pt, now)
            except (files.Damaged, StoreFull, DeviceFull, OSError, ValueError) as e:  # cannot record: never runs
                return drop(f"store: {type(e).__name__}")
        return v if v.result == "drop" else dataclasses.replace(v, header=h, device=v.device or h.device.hex())

    def _unverified(self, now: int, code: str, **extra) -> Verdict:
        """A refusal before any registered signature verified: the host-wide budget, dropped once spent."""
        return refuse(code, **extra) if self.budget.take(now) else drop("budget")

    def _recorded(self, h: Header, did: str, dig: str, now: int, code: str, **extra) -> Verdict:
        """Persisted as the rid's outcome before it is sent."""
        self.store.record(h.rid.hex(), did, dig, now, {"refusal": code, **extra})
        return refuse(code, **extra)

    def _check(self, env, h: Header, hb, body, sig, pt, now) -> Verdict:
        did = h.device.hex()
        # 3. who signed it
        dev = self.registry.devices().get(did)
        if dev is None:
            try:
                meta, _ = unframe(pt)
            except Malformed:
                return self._unverified(now, "malformed")
            if meta.get("op") == "pair":
                return self.pairing.pair(h, hb, body, sig, meta, now, self.budget, self._phone_key)
            v = self.pairing.pending_request(h, hb, body, sig, meta, now, self.budget, self.rand, self.rp_id,
                                             self.origin)
            return v if v is not None else self._unverified(now, "not_paired")
        if dev.revoked:
            return self._unverified(now, "revoked")
        if not verify(dev.pub, sig, signed_bytes(hb, body)):
            return self._unverified(now, "bad_signature")
        # the signature verified: from here nothing is dropped for the budget
        rid, dig = h.rid.hex(), digest(env).hex()
        # 4. a known request id: never runs twice, a stored refusal stays a refusal
        try:
            known = self.store.get(rid, now)
        except files.Damaged:
            return refuse("already_done", why="damaged_record", status="unknown")
        if known is not None:
            if known.device != did or known.digest != dig:
                return refuse("rid_conflict")
            out = known.outcome
            if out is None or RUNNING in out:  # not finished (still deciding or running, or the host stopped)
                return refuse("already_done", status="unknown")
            if "refusal" in out:  # re-sent with the host's CURRENT clock and high
                extra = {k: v for k, v in out.items() if k != "refusal"}
                if "host_ms" in extra:
                    extra["host_ms"] = now
                if "high" in extra:
                    extra["high"] = self.store.seq_state(did)[0]
                return refuse(out["refusal"], why="replay", **extra)
            return Verdict("replay", device=did, scope=dev.scope, outcome=out, body=known.body, rid=rid)
        # the device's quota in the request store: a device over it is refused `busy`, recorded (its allowance of
        # busy records is the store's own), and never takes room from another device; it does not consume its seq
        if self.store.count_for(did, now) >= self.store.per_device:
            try:
                return self._recorded(h, did, dig, now, "busy")
            except DeviceFull:
                return drop("busy_unrecordable")  # a refusal that cannot be recorded is not sent
        # 5. framing; before the sequence, so a malformed envelope does not consume its seq
        try:
            meta, data = unframe(pt)
        except Malformed:
            return self._recorded(h, did, dig, now, "malformed")
        # 6. sequence BEFORE time: a stale_timestamp refusal consumes its seq, so the same bytes can never run
        high, bitmap = self.store.seq_state(did)
        ok, new_high, new_bitmap = seq_accept(high, bitmap, h.seq)
        if not ok:
            return self._recorded(h, did, dig, now, "stale_sequence", high=high)
        self.store.save_seq(did, new_high, new_bitmap)
        # 7. time
        if abs(now - h.ts_ms) > WINDOW_MS:
            return self._recorded(h, did, dig, now, "stale_timestamp", host_ms=now)
        # 8. a stream belongs to the device that opened it
        if h.stream != ZERO_ID and self.streams.get(h.stream.hex()) != did:
            return self._recorded(h, did, dig, now, "forbidden_scope")
        # 9. recorded (no outcome yet) and persisted before anything runs
        self.store.record(rid, did, dig, now, None)
        if h.flags & F_STREAM:
            self.streams[rid] = did
        return Verdict("accept", device=did, scope=dev.scope, meta=meta, data=data, rid=rid, entry=dev, gen=dev.gen,
                       at_ms=now)

    def _phone_key(self, phone_id: str) -> bytes | None:
        try:
            key = self.phone_key(phone_id)
        except Exception:  # noqa: BLE001 - an unreadable phone pairing is no phone pairing
            return None
        return key if isinstance(key, bytes) and key else None

    # -- §6.1 step 9 after the record: scope, assertion, run -----------------------------------------------------------

    def authorize(self, acc: Verdict) -> Verdict:
        """For an `accept` from check(): run, or a refusal that replaces the record's outcome before it is sent."""
        if acc.result != "accept" or acc.rid is None or acc.header is None:
            raise ValueError("authorize takes an accepted request")
        with self._lock:
            try:
                return self._authorize(acc, self.clock())
            except (files.Damaged, OSError, LookupError, ValueError):
                # the record (no outcome) stays: a retry gets already_done/unknown and nothing runs
                return drop("store")

    def _final(self, rid: str, now: int, code: str, why: str = "", **fields) -> Verdict:
        self.store.set_outcome(rid, {"refusal": code, **fields}, now)
        if self.streams.get(rid) is not None:  # a stream whose opening was refused is not open
            self.streams.pop(rid, None)
        return refuse(code, why=why, **fields)

    def _authorize(self, acc: Verdict, now: int) -> Verdict:
        rid, did = acc.rid, acc.device
        if self.stopped:
            return self._final(rid, now, "stopped")
        current = self.registry.devices().get(did)
        if current is None or current.revoked:
            return self._final(rid, now, "revoked")
        if not _same_entry(acc, current):
            return self._final(rid, now, "scope_changed")
        dev = acc.entry  # the entry the signature was verified with, confirmed to be the current one
        op = acc.meta.get("op")
        if op == "cancel":
            if acc.header.stream == ZERO_ID:
                return self._final(rid, now, "malformed")
            stream = acc.header.stream.hex()
            self.streams.pop(stream, None)
            self.store.set_outcome(rid, {"cancelled": stream}, now)
            return dataclasses.replace(acc, result="cancel", fields={"stream": stream})
        if op == "pair_status":  # a registered device: its pairing was approved
            self.store.set_outcome(rid, {"state": "approved"}, now)
            return dataclasses.replace(acc, result="pair_status", fields={"state": "approved"})
        if op == "assert":
            return self._assert(acc, dev, now)
        if op != "http":
            return self._final(rid, now, "malformed")
        req = self._requirement(acc)
        if req is None or SCOPES[req.scope] > dev.level:
            return self._final(rid, now, "forbidden_scope")
        if req.assertion == "none":
            return self._run(acc, dev, (rid,), now)
        stream = acc.header.stream
        lease_class = req.assertion == "lease" and stream != ZERO_ID and self.streams.get(stream.hex()) == did
        if lease_class and self.leases.get(did, 0) > now:
            return self._run(acc, dev, (rid,), now, until_ms=self.leases[did])
        if lease_class:
            purpose, subject = "lease", dict(LEASE_SUBJECT)
        else:
            purpose = "fresh"
            subject = _subject(req.subject)
            if subject is None:
                return self._final(rid, now, "assertion_failed", why="no_subject")
            if not self.fresh_limits.setdefault(did, fresh_limit()).take(now):  # D9, checked before any challenge
                return self._final(rid, now, "assertion_failed", why="rate_limited")
        nonce, expires = self.rand(32), now + CHALLENGE_MS
        ch = assertion_challenge(self.workspace, acc.header.device, acc.header.rid, purpose, req.scope, expires, nonce,
                                 subject)
        self.challenges = {k: c for k, c in self.challenges.items() if now < c.expires_ms}
        self.challenges[ch.hex()] = Issued(did, expires, rid, purpose, req.scope, subject)
        self.parked[rid] = acc
        code = "assertion_required" if purpose == "fresh" else "lease_required"
        return self._final(rid, now, code, purpose=purpose, scope=req.scope, expires_ms=expires, nonce=nonce.hex(),
                           subject=subject)

    def _requirement(self, acc: Verdict) -> Requirement | None:
        try:
            req = self.route(acc.meta, acc.data)
        except Exception:  # noqa: BLE001 - a hook that fails means never remote
            return None
        if (not isinstance(req, Requirement) or req.scope not in SCOPES
                or req.assertion not in ("none", "lease", "fresh")):
            return None
        return req

    def _run(self, acc: Verdict, dev: Device, answer_rids: tuple[str, ...], now: int, fresh: bool = False,
             until_ms: int | None = None) -> Verdict:
        """The whole decision passed: the records become running, and the run carries what it was decided on."""
        for r in answer_rids:
            self.store.set_outcome(r, {RUNNING: dev.gen}, now)
        return dataclasses.replace(acc, result="run", scope=dev.scope, fresh=fresh, answer_rids=answer_rids,
                                   entry=dev, gen=dev.gen, at_ms=now, until_ms=until_ms)

    def offer(self, scope: str):
        """A new pairing offer and its link fragment (the Remote tab), under the host lock."""
        with self._lock:
            return self.pairing.offer(scope, self.clock(), self.rand, self.host_pub)

    def approve(self, did: str, scope: str | None = None) -> Device:
        """The owner approved a pending pairing (scope only lowered); writes and audits the registry entry."""
        with self._lock:
            return self.pairing.approve(did, self.registry, self.clock(), scope)

    def reject(self, did: str) -> None:
        with self._lock:
            self.pairing.reject(did, self.registry, self.clock())

    def health(self) -> dict:
        """What the owner must be shown: damaged request records (kept, counted, never pruned) and whether the
        registry can be read. R3b shows this at start and in the Remote tab rather than only dropping."""
        with self._lock:
            try:
                self.registry.devices()
                registry_ok = True
            except files.Damaged:
                registry_ok = False
            return {"damaged_records": sorted(self.store.damaged), "registry_readable": registry_ok}

    def still_authorized(self, decision: Verdict) -> bool:
        """Whether `decision` (a run) may run now: not stopped, and still_authorized() for its entry and grant."""
        with self._lock:
            return not self.stopped and still_authorized(decision, self.registry, self.clock())

    def _assert(self, r2: Verdict, dev: Device, now: int) -> Verdict:
        """R2 (op=assert) for a parked R1 (§9.4, §9.5). Every outcome is written to the audit log."""
        m, rid2, did = r2.meta, r2.rid, r2.device
        try:
            for_rid = unhex(m["for"], 16).hex()
            cid, ad, cdj, sig = (unb64u(m[k]) for k in ("credential_id", "authenticator_data", "client_data_json",
                                                         "signature"))
        except (KeyError, TypeError, ValueError):
            return self._final(rid2, now, "assertion_failed", why="malformed")
        if dev.credential is None:
            return self._final(rid2, now, "assertion_failed", why="no_credential")
        v = verify_assertion(dev.credential, did, self.challenges, did, cid, ad, cdj, sig, now)
        issued = v.issued
        why = v.why or ("" if issued is None or issued.rid == for_rid else "other_request")
        self.registry.audit(now, "assertion", device=did, ok=v.ok and not why, why=why or None, rid=for_rid,
                            purpose=issued.purpose if issued else None, scope=issued.scope if issued else None,
                            subject=issued.subject if issued else None, counter_warning=v.counter_warning or None)
        if issued is not None and not (v.ok and not why):
            self.parked.pop(issued.rid, None)  # its challenge is gone: R1 can only be sent again as a new request
        if not v.ok or why:
            return self._final(rid2, now, "assertion_failed", why=why)
        if v.sign_count is not None and v.sign_count != dev.credential.sign_count:
            self.registry.set_sign_count(did, v.sign_count, now)
        r1 = self.parked.pop(for_rid, None)
        rec = self.store.get(for_rid, now) if r1 is not None else None
        if r1 is None or r1.device != did or rec is None:
            return self._final(rid2, now, "assertion_failed", why="nothing_parked")
        if r1.gen != dev.gen:  # the registry entry changed between R1 and R2
            self.store.set_outcome(for_rid, {"refusal": "scope_changed"}, now)
            return self._final(rid2, now, "scope_changed")
        req = self._requirement(r1)  # R1 runs exactly once, after its scope is checked again
        stream = r1.header.stream
        if req is None or req.scope != issued.scope or SCOPES[req.scope] > dev.level or (
                issued.purpose == "lease" and (stream == ZERO_ID or self.streams.get(stream.hex()) != did)):
            self.store.set_outcome(for_rid, {"refusal": "forbidden_scope"}, now)
            return self._final(rid2, now, "forbidden_scope")
        if issued.purpose == "lease":  # opened only once R1 passed its check again
            self.leases[did] = now + LEASE_MS
        until = self.leases[did] if issued.purpose == "lease" else issued.expires_ms  # the grant behind this run
        return self._run(r1, dev, (for_rid, rid2), now, fresh=issued.purpose == "fresh", until_ms=until)

    def finish(self, rids, head: dict, body: bytes = b"") -> bool:
        """Store the result of a run as the outcome of every rid in `rids` (Verdict.answer_rids), before the
        response is sent. Only a running record takes it: when one was refused meanwhile (the device was revoked or
        rescoped while it ran), nothing is replaced and this returns False, and the caller sends that refusal
        instead. A body over 64 KiB raises ValueError: what a replay of a larger response answers is the caller's
        decision, the record then stays running (a retry gets already_done/unknown)."""
        if len(body) > MAX_REPLAY_BODY or RUNNING in head:
            raise ValueError("a stored replay body is at most 64 KiB, and a head has no running marker")
        with self._lock:
            now = self.clock()
            recs = [self.store.get(rid, now) for rid in rids]
            if any(r is None or r.outcome is None or RUNNING not in r.outcome for r in recs):
                return False
            devs = self.registry.devices()  # a change made elsewhere (the Remote tab, another workspace's revoke)
            for r in recs:
                cur = devs.get(r.device)
                if cur is None or cur.revoked or cur.gen != r.outcome[RUNNING]:
                    code = "revoked" if cur is None or cur.revoked else "scope_changed"
                    for rid in rids:
                        self.store.set_outcome(rid, {"refusal": code}, now)
                    return False
            for rid in rids:
                self.store.set_outcome(rid, dict(head), now, body)
            return True

    def end_run(self, run: Verdict, code: str | None = None) -> Verdict:
        """The refusal that ends `run` when it may not go on (still_authorized() answered False, finish() returned
        False, or the caller refuses it itself with `code`, such as `busy`), carrying the request's header, ready for
        seal_refusal(). A refusal already stored for the record (revoke, set_scope, finish) always wins; otherwise
        `code`, else `stopped` after the kill switch, else `revoked` when the registry no longer holds the device
        (a change made elsewhere), else `scope_changed` (the entry or the grant the run was decided on no longer
        holds), is stored first for every rid of the run that has not finished. Never replaces a finished
        outcome."""
        with self._lock:
            now = self.clock()
            stored = None
            try:
                rec = self.store.get(run.rid, now)
                stored = rec.outcome if rec is not None else None
            except files.Damaged:
                pass
            if isinstance(stored, dict) and stored.get("refusal") in CODES:
                c = stored["refusal"]
                return dataclasses.replace(refuse(c, **{k: v for k, v in stored.items()
                                                        if k in REFUSAL_FIELDS.get(c, ())}),
                                           header=run.header, device=run.device)
            c = code or ("stopped" if self.stopped else None)
            if c is None:
                try:
                    cur = self.registry.devices().get(run.device)
                    c = "revoked" if cur is None or cur.revoked else "scope_changed"
                except files.Damaged:
                    c = "scope_changed"
            for rid in run.answer_rids or (run.rid,):
                try:
                    r = self.store.get(rid, now)
                    if r is not None and (r.outcome is None or RUNNING in r.outcome):
                        self.store.set_outcome(rid, {"refusal": c}, now)
                except (files.Damaged, LookupError, OSError, ValueError):
                    pass  # unrecordable: the record stays unfinished, so a retry is answered already_done/unknown
            return dataclasses.replace(refuse(c), header=run.header, device=run.device)

    # -- streams, leases, revocation, the kill switch ------------------------------------------------------------------

    def close_stream(self, rid: str) -> None:
        self.streams.pop(rid, None)

    def end_lease(self, did: str) -> None:
        self.leases.pop(did, None)

    def _end_device(self, did: str, code: str) -> list[str]:
        now = self.clock()
        for rid in self.store.in_flight(did, now, RUNNING):  # not finished: refused, so nothing of it is answered
            self.store.set_outcome(rid, {"refusal": code}, now)
        ended = [s for s, d in self.streams.items() if d == did]
        for s in ended:
            del self.streams[s]
        self.leases.pop(did, None)
        self.challenges = {k: c for k, c in self.challenges.items() if c.device != did}
        self.parked = {k: p for k, p in self.parked.items() if p.device != did}
        return ended

    def revoke(self, did: str) -> list[str]:
        """Revoke (audited) and end the device's streams, lease and waiting actions; returns the streams to close
        with a final `revoked` refusal chunk."""
        with self._lock:
            self.registry.revoke(did, self.clock())
            return self._end_device(did, "revoked")

    def set_scope(self, did: str, scope: str) -> list[str]:
        """Change the scope (audited); returns the streams to close with `scope_changed`."""
        with self._lock:
            self.registry.set_scope(did, scope, self.clock())
            return self._end_device(did, "scope_changed")

    def stop(self) -> list[str]:
        """The kill switch: everything after it is refused `stopped`; returns every stream to close with `stopped`."""
        with self._lock:
            self.stopped = True
            ended = list(self.streams)
            self.streams.clear()
            self.leases.clear()
            self.challenges.clear()
            self.parked.clear()
            return ended

    # -- answers -------------------------------------------------------------------------------------------------------

    def seal_chunk(self, req: Header, idx: int, meta: dict, data: bytes = b"", *, last: bool, stream: bool = False,
                   refusal: bool = False) -> bytes:
        """One response chunk for request `req`: index `idx`, the host's clock, a fresh salt, sealed and signed with
        the host key. A stream's frames carry the stream's rid in `stream`."""
        if refusal and not last:
            raise ValueError("a refusal is a single LAST chunk")
        flags = (F_LAST if last else 0) | (F_STREAM if stream else 0) | (F_REFUSAL if refusal else 0)
        h = Header(TO_DEVICE, flags, req.workspace, req.device, req.rid, req.rid if stream else ZERO_ID, idx,
                   self.clock(), self.rand(16), req.key_version, req.version)
        hb = h.encode()
        body = seal(self.k_ws, hb, frame(meta, data))
        env = hb + body + sign(self.host_key, signed_bytes(hb, body))
        if len(env) > MAX_CHUNK:
            raise ValueError("a response chunk is at most 256 KiB")
        return env

    def seal_refusal(self, v: Verdict, *, stream: bool = False) -> bytes:
        """The sealed, signed refusal for a `refuse` verdict: meta {"refusal": code, ...} and nothing from the
        request."""
        if v.result != "refuse" or v.header is None:
            raise ValueError("not a refusal")
        return self.seal_chunk(v.header, 0, {"refusal": v.code, **v.fields}, last=True, stream=stream, refusal=True)


def _same_entry(decision: Verdict, current: Device) -> bool:
    """The registry entry a decision was taken on is still the current one: same generation, key and scope."""
    entry = decision.entry
    return (isinstance(entry, Device) and decision.gen is not None and current.gen == decision.gen == entry.gen
            and current.pub == entry.pub and current.scope == entry.scope and not current.revoked)


def still_authorized(decision: Verdict, registry: Registry, now_ms: int) -> bool:
    """Whether a decision may still be acted on at `now_ms`: its device is registered, not revoked, under the same
    registry generation, key and scope it was decided on, and any fresh-assertion or lease grant behind it has not
    ended. Reads the registry once; any error answers False. The dispatcher MUST call this (or
    Host.still_authorized) immediately before running anything."""
    try:
        current = registry.devices().get(decision.device)
    except Exception:  # noqa: BLE001 - cannot tell: not authorised
        return False
    if current is None or not _same_entry(decision, current):
        return False
    return decision.until_ms is None or now_ms < decision.until_ms


def _subject(subject) -> dict | None:
    """The subject of a fresh assertion with its `shown` cleaned (§9.3); None when it is not a valid one (a lone
    surrogate included): then no challenge is issued."""
    if not isinstance(subject, dict) or set(subject) != {"kind", "shown", "digest"}:
        return None
    if subject["kind"] not in SUBJECT_KINDS or subject["kind"] == "lease" or not isinstance(subject["digest"], str) \
            or not _DIGEST.fullmatch(subject["digest"]):
        return None
    try:
        return {"kind": subject["kind"], "shown": clean_shown(subject["shown"]), "digest": subject["digest"]}
    except ValueError:
        return None
