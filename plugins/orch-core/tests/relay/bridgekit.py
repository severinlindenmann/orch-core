"""R2 test kit: a phone that builds and reads ORB2 bridge envelopes, the R6 fixtures (epoch keys, member list), a
durable fake host (outcome store + log, with crash points) and a recording poster. No network."""

from __future__ import annotations

import contextlib
import json
import secrets
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from orch import canon, crypto
from orch.identity import certs
from orch.relay import framing
from orch.relay.bridge import Bridge, Item
from orch.relay.framing import Identity, Refusal
from orch.relay.keys import BridgeKeys, Member, MemberList, SealState
from orch.relay.rids import RidStore
from orch.wsproto import sign, wire
from orch.wsproto.state import StateDir

from .helpers import WS, Key

T0 = 1_790_000_000_000
LIST_HASH = "sha256:" + "4" * 64


_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(ms: int = T0) -> str:
    """A ULID with time ``ms`` (fresh against the kit's clock by default)."""
    t = "".join(_B32[(ms >> (5 * i)) & 31] for i in reversed(range(10)))
    return t + "".join(secrets.choice(_B32) for _ in range(16))


def idem() -> str:
    return ulid()


class Clock:
    def __init__(self, ms: int = T0) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


class Phone:
    """A member device: a person key, a device key, its certificate; builds requests and opens answers."""

    def __init__(
        self, scopes: tuple[str, ...] = ("look", "decide", "operate"), *, expires_ms: int | None = None,
        seed: bytes | None = None,
    ):  # fmt: skip
        self.pk = _key(seed, b"pk")
        self.dk = _key(seed, b"dk")
        self.pk_pub, self.dk_pub = crypto.public_bytes(self.pk), crypto.public_bytes(self.dk)
        self.cert = certs.make_device_cert(
            self.pk_pub, lambda b: crypto.sign(self.pk, b), dk_sig_pub=self.dk_pub,
            dk_kx_pub=crypto.public_bytes(crypto.generate_private_key()), label_sealed=bytes(32),
            created_ms=T0 - 3_600_000, expires_ms=expires_ms, scopes_max=list(scopes),
        )  # fmt: skip
        self.device = self.cert["o"]["device_id"]
        self.person = self.cert["o"]["person_id"]
        self.seq = 0

    def member(self, scopes: tuple[str, ...] | None = None) -> Member:
        return Member(self.device, self.person, tuple(scopes or self.cert["o"]["scopes_max"]), self.cert, self.pk_pub)

    def request(
        self,
        keys: dict[int, bytes],
        meta: dict[str, Any] | bytes,
        *,
        epoch: int = 2,
        seq: int | None = None,
        ts_ms: int = T0,
        rid: bytes | None = None,
        data: bytes = b"",
        flags: int = 0,
        stream: bytes = bytes(16),
        signer: Any = None,
        ws: str = WS,
    ) -> bytes:
        if seq is None:
            self.seq += 1
            seq = self.seq
        h = wire.Header(
            2, 1, flags, wire.SUITE, bytes.fromhex(ws), bytes.fromhex(self.device), rid or secrets.token_bytes(16),
            stream, seq, ts_ms, epoch, secrets.token_bytes(16),
        )  # fmt: skip
        hb = h.pack()
        pt = meta if isinstance(meta, bytes) else wire.build_plaintext(meta, data)
        chan = (
            wire.k_pair(keys[0], bytes.fromhex(ws), stream)
            if epoch == 0
            else wire.k_bridge(keys[epoch], bytes.fromhex(ws), epoch)
        )
        body = wire.seal_body(chan, hb, pt)
        sig = crypto.sign(signer or self.dk, wire.bridge_signed_bytes(hb, body))
        return hb + body + sig

    def event(self, log: str, typ: str, *, eid: str | None = None, **fields: Any) -> dict[str, Any]:
        e = {
            "v": 2, "id": eid or ulid(), "type": typ, "actor": {"kind": "person", "id": "p_" + self.person,
            "device": "d_" + self.device}, "auth": "passphrase", "hash_v": 1, "roster_v": 1, "based_on": None,
            **fields,
        }  # fmt: skip
        e["sig"] = crypto.b64u(crypto.sign(self.dk, canon.person_signing_bytes(WS, log, e)))
        return e


def open_chunk(env: bytes, keys: dict[int, bytes], wsk_pub: bytes) -> tuple[wire.Header, dict[str, Any], bytes]:
    """What the phone does with a chunk (proto §8.8, the parts a test needs): WSK signature, then the tag."""
    h, body, sig = wire.parse_envelope(env, direction=2)
    hb = env[: wire.HEADER_LEN]
    assert crypto.verify(wsk_pub, sig, wire.bridge_signed_bytes(hb, body)), "WSK signature"
    pt = wire.open_body(wire.k_bridge(keys[h.epoch], h.workspace, h.epoch), hb, body)
    meta, data = wire.parse_plaintext(pt)
    return h, meta, data


def _key(seed: bytes | None, what: bytes):
    """A fresh key, or a deterministic one from ``seed`` (a child process rebuilds the same world)."""
    if seed is None:
        return crypto.generate_private_key()
    d = int.from_bytes(crypto.sha256(seed + what), "big") % (crypto.P256_N - 1) + 1
    return crypto.private_key_from_scalar(d)


class SealLockFixture:
    """The seal lock an :class:`orch.relay.keys.EpochKeySource` owns (R6a: the source owns it, not ``BridgeKeys``),
    for fixture sources: one ``RLock``, ``seal_snapshot`` takes ``seal_state()`` under it."""

    def _lock(self) -> threading.RLock:
        lock = self.__dict__.get("_seal_lock")
        if lock is None:
            lock = self.__dict__.setdefault("_seal_lock", threading.RLock())
        return lock

    @contextlib.contextmanager
    def seal_snapshot(self) -> Iterator[SealState]:
        with self._lock():
            yield self.seal_state()  # type: ignore[attr-defined]

    @contextlib.contextmanager
    def serialised(self) -> Iterator[None]:
        with self._lock():
            yield


class Epochs(SealLockFixture):
    """R6's key custody, fixture: committed epochs ``1..cur`` (``keys``), burned epochs above ``cur`` (``burned``:
    their keys are in ``keys`` too, so a phone fixture can seal under them), an optional pending epoch and the
    rotation gate."""

    def __init__(self, current: int = 2, *, seed: bytes | None = None) -> None:
        self.cur = current
        self.seed = seed
        self.keys = {e: self._new(e) for e in range(1, current + 1)}
        self.burned: set[int] = set()
        self.pending: int | None = None
        self.gated = False

    def _new(self, e: int) -> bytes:
        return secrets.token_bytes(32) if self.seed is None else crypto.sha256(self.seed + b"wk%d" % e)

    def current_epoch(self) -> int:
        return self.cur

    def wk(self, epoch: int) -> bytes | None:
        return self.keys.get(epoch) if epoch <= self.cur else None

    def refusal_key(self, epoch: int) -> bytes | None:
        return self.keys.get(epoch) if epoch in self.burned and epoch > self.cur else None

    def is_burned(self, epoch: int) -> bool:
        return epoch in self.burned

    def seal_state(self) -> SealState:
        ehw = max([self.cur, *self.burned, *([self.pending] if self.pending else [])])
        return SealState(self.cur, self.gated, self.pending if self.pending is not None else ehw + 1)

    def rotate(self) -> None:
        self.cur = max([self.cur, *self.burned]) + 1
        self.keys[self.cur] = self._new(self.cur)
        self.pending, self.gated = None, False

    def burn(self) -> int:
        """An exposed body introduced epoch EHW + 1 and was abandoned (proto §7.2 H4): that epoch is burned."""
        e = max([self.cur, *self.burned]) + 1
        self.keys[e] = self._new(e)
        self.burned.add(e)
        return e


class R6:
    def __init__(self, epochs: Epochs) -> None:
        self.epochs = epochs
        self.members: dict[str, Member] = {}
        self.hash = LIST_HASH

    def add(self, phone: Phone, scopes: tuple[str, ...] | None = None) -> None:
        self.members[phone.device] = phone.member(scopes)

    def member_list(self) -> MemberList | None:
        return MemberList(self.epochs.cur, self.hash, dict(self.members))


class FakeHost:
    """A durable fake of the host side of the relay entry (``HostPort``): an outcome store (``outcomes.json``) and a
    log (``log.json``), each written atomically, with crash points: ``after_begin`` (pending record durable, nothing
    appended) and ``after_append`` (the event landed, the record still pending). Semantics mirror
    ``OutcomeStore.execute`` + ``relay.submit``: a duplicate idem with the same binding replays ``duplicate: true``,
    another binding is ``idem.conflict``; a pending record reconciles from the log."""

    def __init__(
        self, root: Path, *, fault: Callable[[str], None] | None = None, clock: Callable[[], int] | None = None
    ) -> None:
        self.root, self.fault = root, fault or (lambda p: None)
        self.clock = clock or (lambda: T0)  # the HOST clock (ms): the event window is checked against it
        self.standings: dict[tuple[str, str], str] = {}
        self.runs: list[tuple[str, str]] = []  # (op, idem) each time an op body ran
        self.reads: list[str] = []

    def _load(self, name: str) -> Any:
        p = self.root / name
        return json.loads(p.read_text()) if p.exists() else {}

    def _save(self, name: str, obj: Any) -> None:
        tmp = self.root / (name + ".tmp")
        tmp.write_text(json.dumps(obj, sort_keys=True))
        tmp.replace(self.root / name)

    @property
    def log(self) -> dict[str, Any]:
        return self._load("log.json")

    def standing(self, device_id: str, person_id: str) -> str:
        return self.standings.get((device_id, person_id), "ok")

    def read(self, identity: Identity, op: str, args: dict[str, Any], recheck: Callable[[], bool]) -> dict:
        if not recheck():
            raise Refusal("op.not_allowed", retryable=False)
        self.reads.append(op)
        if op == "outcome.get":
            return _ok(self.lookup(identity, args.get("idem", "")))
        if op == "ticket.get" and args.get("ref") == "missing":
            raise Refusal("not_found", retryable=False)
        if op == "ticket.text" and "size" in args:
            return _ok({"text": "x" * args["size"]})
        # H12d-3 (the host's check_rd and reader.declare in column R; only the framing is normative here)
        if op == "ticket.get" and args.get("ref") == "rd-unknown":
            raise Refusal("sign.convenience_readers", reason="declaration_unknown")
        if op == "ticket.get" and args.get("ref") == "rd-old":
            raise Refusal("sign.convenience_readers", reason="reader_too_old", required=1, declared=0)
        if op == "reader.declare":
            return _ok({"device": identity.device_id, "reader": 1, "level": 1, "digest": "sha256:" + "cd" * 32})
        return _ok({"op": op, "viewer": identity.person_id, "principal": identity.principal})

    def lookup(self, identity: Identity, idem: str) -> dict[str, Any]:
        rec = self._load("outcomes.json").get(f"{identity.principal}|{idem}")
        if rec is None:
            raise Refusal("outcome.unknown_idem", retryable=False)
        if rec["step"] == "pending":
            rec = self._reconcile(f"{identity.principal}|{idem}", rec)
            if rec is None:
                raise Refusal("outcome.unknown_idem", retryable=False)
        if rec["step"] == "pending":
            return {"state": "pending"}
        if rec["step"] == "tombstone":
            raise Refusal("idem.expired")
        out = {"state": rec["step"], "exit": rec.get("exit", 0)}
        for k in ("result", "error", "seqs"):
            if k in rec:
                out[k] = rec[k]
        return out

    def _reconcile(self, key: str, rec: dict[str, Any]) -> dict[str, Any] | None:
        outcomes = self._load("outcomes.json")
        ev = rec["events"][0]
        logged = self.log.get(ev["id"])
        if logged is None:
            outcomes.pop(key, None)
            self._save("outcomes.json", outcomes)
            return None
        rec = {**rec, "step": "committed", "result": _submit_result(rec["log"], [logged["seq"]])}
        outcomes[key] = rec
        self._save("outcomes.json", outcomes)
        return rec

    def submit(
        self, identity: Identity, idem: str, op: str, log: str, events: list[dict], recheck: Callable[[], bool]
    ) -> dict[str, Any]:
        if not recheck():
            raise Refusal("op.not_allowed", retryable=False)
        key = f"{identity.principal}|{idem}"
        unsigned = [{k: v for k, v in e.items() if k != "sig"} for e in events]  # event identity, not sig bytes
        binding = canon.dumps({"log": log, "events": unsigned}).decode()
        outcomes = self._load("outcomes.json")
        rec = outcomes.get(key)
        if rec is not None:
            if rec["binding"] != binding:
                raise Refusal("idem.conflict", retryable=False)
            if rec["step"] == "pending":
                rec = self._reconcile(key, rec)
            if rec is not None:
                if rec["step"] == "committed":
                    return {**rec["result"], "duplicate": True}
                if rec["step"] == "tombstone":
                    raise Refusal("idem.expired")
                raise Refusal(rec["error"]["code"], **({"seqs": rec["seqs"]} if rec.get("seqs") else {}))
            outcomes = self._load("outcomes.json")
        outcomes[key] = {"binding": binding, "step": "pending", "log": log, "events": events}
        self._save("outcomes.json", outcomes)
        self.fault("after_begin")
        logd = self.log
        ev = events[0]
        ident = canon.dumps({"log": log, "event": unsigned[0]}).decode()
        if ev["id"] in logd:  # log dedup by event identity BEFORE the window (ruling A)
            if logd[ev["id"]].get("ident") != ident:
                return self._fail(key, binding, "event.duplicate_id")
            result = {**_submit_result(log, [logd[ev["id"]]["seq"]]), "duplicate": True}
            outcomes[key] = {**outcomes[key], "step": "committed", "result": result}
            self._save("outcomes.json", outcomes)
            return result
        born, now = framing.ulid_ms(ev["id"]), self.clock()
        if not now - framing.FRESH_PAST_MS <= born <= now + framing.FRESH_FUTURE_MS:
            return self._fail(key, binding, "event.expired")  # a never-seen event only, against HOST time
        self.runs.append((op, idem))
        if ev.get("title") == "refuse me":
            return self._fail(key, binding, "ticket.exists")
        if ev.get("key") == "DEMO-0007":  # the kit's taken key
            return self._fail(key, binding, "ticket.key_not_next", next_key="DEMO-42")
        logd[ev["id"]] = {"seq": len(logd) + 1, "type": ev["type"], "ident": ident}
        self._save("log.json", logd)
        self.fault("after_append")
        result = _submit_result(log, [logd[ev["id"]]["seq"]])
        outcomes[key] = {**outcomes[key], "step": "committed", "result": result}
        self._save("outcomes.json", outcomes)
        return result

    def _fail(self, key: str, binding: str, code: str, **fields: Any) -> dict[str, Any]:
        outcomes = self._load("outcomes.json")
        outcomes[key] = _fail_record(binding, code, **fields)
        self._save("outcomes.json", outcomes)
        raise Refusal(code, **fields)

    def set_outcome(self, principal: str, idem: str, step: str, **fields: Any) -> None:
        """A stored outcome for ``(principal, idem)`` (a vector's setup): ``failed``/``partial`` with ``error`` and
        ``seqs``, or ``tombstone``."""
        outcomes = self._load("outcomes.json")
        outcomes[f"{principal}|{idem}"] = {"binding": "", "step": step, "exit": 5 if step != "tombstone" else 0,
                                           **fields}  # fmt: skip
        self._save("outcomes.json", outcomes)


def _fail_record(binding: str, code: str, **detail: Any) -> dict[str, Any]:
    """A stored failed outcome as the host stores it: the socket ``Error`` with its closed per-code ``detail``
    (contract v1.9, #499: ``ticket.key_not_next {next_key}``)."""
    error: dict[str, Any] = {"code": code}
    if detail:
        error["detail"] = detail
    return {"binding": binding, "step": "failed", "error": error, "exit": 5}


def _ok(data: Any) -> dict[str, Any]:
    return {"data": data, "key": None, "seq": 0, "cursor": 0, "hints": [], "lines": [], "duplicate": False, "exit": 0,
            "head": None}  # fmt: skip


def _submit_result(log: str, seqs: list[int]) -> dict[str, Any]:
    from orch.relay.submit import result

    return result(log, seqs)


class Poster:
    """Records every POST; ``script`` maps a call number (0-based) to a :data:`PostResult` (default ``posted``)."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str, bytes]] = []
        self.script: dict[int, str] = {}
        self.default = "posted"

    def __call__(self, device: str, rid: str, env: bytes) -> str:
        n = len(self.posts)
        self.posts.append((device, rid, env))
        return self.script.get(n, self.default)

    def for_rid(self, rid: bytes | str) -> list[bytes]:
        r = rid if isinstance(rid, str) else rid.hex()
        return [e for _, x, e in self.posts if x == r]


class World:
    """One bridge over a fresh state directory, with its fakes; :meth:`reopen` simulates a restart."""

    def __init__(
        self, tmp: Path, *, fault: Callable[[str], None] | None = None, start: bool = True, seed: bytes | None = None
    ) -> None:
        self.tmp = tmp
        self.epochs = Epochs(seed=seed)
        self.r6 = R6(self.epochs)
        self.phone = Phone(seed=seed)
        self.r6.add(self.phone)
        self.clock = Clock()
        self.key = Key()
        self.alarms: list[tuple[str, str]] = []
        (tmp / "host").mkdir(exist_ok=True)
        self.host = FakeHost(tmp / "host", fault=fault, clock=self.clock)
        self.post = Poster()
        self.fault = fault
        self.state: StateDir | None = None
        self.rids: RidStore | None = None
        self.n = 0
        self._open(fault)
        if start:
            self.bridge.start()

    def _open(self, fault: Callable[[str], None] | None) -> None:
        self.state = StateDir(self.tmp / "relay")
        self.rids = RidStore(self.state, fault=fault)
        keys = BridgeKeys(self.epochs, bytes.fromhex(WS))
        signer = sign.RelaySigner(self.key, sign.LinkContext(WS, "https://relay.example"), epochs=keys)
        self.bridge = Bridge(
            workspace_id=WS, rids=self.rids, keys=keys, signer=signer, r6=self.r6, port=self.host,
            job=lambda fn: fn(), post=self.post, now_ms=self.clock, alarm=lambda c, d: self.alarms.append((c, d)),
        )  # fmt: skip

    def close(self) -> None:
        if self.rids is not None:
            self.rids.close()
        if self.state is not None:
            self.state.close()

    def reopen(self, *, fault: Callable[[str], None] | None = None, start: bool = True) -> None:
        self.close()
        self.host.fault = fault or (lambda p: None)
        self._open(fault)
        if start:
            self.bridge.start()

    @property
    def wk(self) -> dict[int, bytes]:
        return self.epochs.keys

    def item(self, env: bytes, *, device: str | None = None) -> Item:
        self.n += 1
        h = wire.parse_header(env[: wire.HEADER_LEN])
        return Item(self.n, "bridge", device if device is not None else h.device.hex(), h.rid.hex(), env)

    def send(self, env: bytes, *, run: bool = True) -> list[bytes]:
        """Deliver one request; run it if admitted; return the chunks POSTed for its rid during this call."""
        h = wire.parse_header(env[: wire.HEADER_LEN])
        before = len(self.post.posts)
        adm = self.bridge.receive(self.item(env))
        if adm is not None and run:
            self.bridge.run(adm)
        return [e for _, r, e in self.post.posts[before:] if r == h.rid.hex()]

    def answer(self, chunks: list[bytes]) -> tuple[dict[str, Any], Any]:
        """``(meta of chunk 0, decoded data)`` of a complete answer."""
        metas, data = [], b""
        for c in chunks:
            h, meta, d = open_chunk(c, self.wk, self.key.public_key)
            metas.append((h, meta))
            data += d
        return metas[0][1], (json.loads(data) if data else None)

    def call(self, meta: dict[str, Any], **kw: Any) -> tuple[dict[str, Any], Any]:
        return self.answer(self.send(self.phone.request(self.wk, meta, **kw)))

    def new_ticket(self, *, key: str | None = None, title: str = "From the phone", **kw: Any) -> dict[str, Any]:
        log = ulid(self.clock.ms)
        ev = self.phone.event(log, "ticket.created", eid=ulid(self.clock.ms),
                              key=key or f"DEMO-{secrets.randbelow(998_000) + 1000}", ticket_type="bug", title=title,
                              owner="p_" + self.phone.person)  # fmt: skip
        return {
            "op": "new_ticket",
            "args": {"log": log, "events": [ev]},
            "idem": kw.pop("idem", None) or ev["id"],
            **kw,
        }


def apply_setup(w: World, action: dict[str, Any]) -> None:
    """A framing vector's setup step (the generator and the replay test both use it): ``clock`` (+ms), ``restart``,
    ``burn`` (an epoch above the committed one), ``outcome`` (a stored outcome for the phone's own principal)."""
    do = action["do"]
    if do == "clock":
        w.clock.ms += action["ms"]
    elif do == "restart":
        w.reopen()
    elif do == "burn":
        w.epochs.burn()
    elif do == "outcome":
        fields = {k: v for k, v in action.items() if k not in ("do", "idem", "step")}
        w.host.set_outcome("r:d_" + w.phone.device, action["idem"], action["step"], **fields)
    else:
        raise ValueError(do)


__all__ = [
    "Clock",
    "apply_setup",
    "Epochs",
    "FakeHost",
    "Phone",
    "Poster",
    "R6",
    "World",
    "framing",
    "idem",
    "open_chunk",
    "ulid",
]
