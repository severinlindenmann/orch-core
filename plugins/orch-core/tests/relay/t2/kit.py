"""T2 setup against the real relay: the owner account, the devices, the card, the member list and the device side.

**Ref-built, not R6.** R6 (pairing, membership, WK custody) does not exist at R2 time. What R6 will do is played here
by the relay's reference client (``ref/orch_protocol_ref.py`` of the pinned checkout, imported, never vendored): the
member list, the WK grant, the device's request envelopes and the device-side checks of the answers (§8.8
``device_check``). The identities are the corpus' vector identities (person alice, devices ``primary`` and
``phone``, workspace A and its WSK), whose certificates do not expire.

This is the MINIMAL owner helper R2's T2 needs (account from an operator invite, a device registration, the first
card), not the test owner client of 11A.15.11 (``tests/relay/owner.py``, R6e: sandbox manifest, cert requests,
journal), which R5/R6 build. Every request goes to the loopback relay the harness started.
"""

from __future__ import annotations

import http.client
import json
import secrets
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any

from orch import canon, crypto
from orch.net import http as rhttp
from orch.relay.bridge import Bridge, BridgeLoop
from orch.relay.keys import BridgeKeys
from orch.relay.rids import RidStore
from orch.relay.session import Session
from orch.wsproto import sign, wire
from orch.wsproto.state import StateDir

from .. import corpus
from ..bridgekit import R6, Epochs, FakeHost, Phone
from ..helpers import OPEN_CONNS, WS, Alarms, Holder, Key, make_card
from .harness import HOST, RelayProcess

PERSON = corpus.IDS["person_alice"]
PRIMARY = corpus.IDS["device_primary"]
DEVICE = corpus.IDS["device_phone"]
CERTS = corpus.S2["certs"]["signed"]
SCOPES = ("look", "decide")  # the 14.8 phone profile; a prefix of the vector certificate's scopes_max


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class Http:
    """Plain JSON/octet calls to the loopback relay (the device and owner side; the host side uses core's client)."""

    def __init__(self, relay: RelayProcess) -> None:
        self.port = relay.port

    def call(
        self, method: str, path: str, body: Any = None, *, token: str | None = None, raw: bytes | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, Any]:  # fmt: skip
        headers = {}
        data = None
        if raw is not None:
            data, headers["Content-Type"] = raw, "application/octet-stream"
        elif body is not None:
            data, headers["Content-Type"] = canon.dumps(body), "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        c = http.client.HTTPConnection(HOST, self.port, timeout=timeout)
        try:
            c.request(method, path, body=data, headers=headers)
            r = c.getresponse()
            out = r.read(8 << 20)
            return r.status, json.loads(out) if out else None
        finally:
            c.close()

    def ok(self, method: str, path: str, body: Any = None, **kw: Any) -> Any:
        status, obj = self.call(method, path, body, **kw)
        assert 200 <= status < 300, (method, path, status, obj)
        return obj

    def challenge(self) -> bytes:
        return crypto.unb64u(self.ok("POST", "/v2/session/challenge", {})["challenge"])


class Owner:
    """The owner's side: an operator invite (the relay's admin CLI on its data directory), the account, the device
    registrations, the first card."""

    def __init__(self, relay: RelayProcess) -> None:
        self.relay, self.http = relay, Http(relay)

    def invite(self) -> str:
        code = self.relay.admin("invite", "create", "--ttl", "1h").strip()
        assert code and "\n" not in code
        return code

    def create_account(self) -> str:
        """POST /v2/accounts with the invite, the person key and the primary's PK-signed certificate, proved by the
        primary's device key over ``auth_bytes(origin, challenge, "device", primary)``."""
        self.invite_code = self.invite()
        self.account_body = self.account_request(self.invite_code)
        obj = self.http.ok("POST", "/v2/accounts", self.account_body)
        assert obj == {"person_id": PERSON}, obj
        return obj["person_id"]

    def account_request(self, invite: str) -> dict[str, Any]:
        ch = self.http.challenge()
        sig = crypto.sign(corpus.priv("primary.sig"), wire.auth_bytes(self.relay.origin, ch, "device", PRIMARY))
        return {
            "invite": invite, "pk_pub": crypto.b64u(corpus.pub("person_alice.sig")), "device_cert": CERTS["primary"],
            "challenge": crypto.b64u(ch), "sig": crypto.b64u(sig),
        }  # fmt: skip

    def register(self, name: str) -> None:
        cert = CERTS[name]
        self.http.ok("PUT", f"/v2/devices/{cert['o']['device_id']}", {"cert": cert})


def session(relay: RelayProcess, *, origin: str | None = None, holder: Holder | None = None) -> Session:
    """A core R1 Session for workspace A against the loopback relay, real clocks; ``origin`` is the origin the host
    SIGNS (``link.json``'s), the transport always goes to the relay."""
    conn = rhttp.RelayConn(relay.origin)
    OPEN_CONNS.append(conn)
    signer = sign.RelaySigner(Key(), sign.LinkContext(workspace_id=WS, origin=origin or relay.origin))
    return Session(conn, signer, holder=holder if holder is not None else Holder(), alarm=Alarms(),
                   stop=threading.Event())  # fmt: skip


def publish_card(sess: Session, relay: RelayProcess) -> int | None:
    from ..helpers import directory

    return directory(sess).put_card(make_card(relay.origin), first=True)


class Members:
    """The ref-built member list and WK grant for epoch ``epoch`` (NOT R6): WSK-signed, PUT under W(g)."""

    def __init__(self, R: ModuleType) -> None:
        self.R = R
        self.suite = R.SUITES[2]
        self.wsk = self.suite.sig_key(bytes.fromhex(corpus.KEYS["wsk_a.sig"]["seed"]))

    def body(self, epoch: int, wk: bytes, *, list_seq: int = 1) -> dict[str, Any]:
        R, ws = self.R, bytes.fromhex(WS)
        members = [{"device_id": DEVICE, "person_id": PERSON, "scopes": list(SCOPES)}]
        ml = R.make_member_list(self.suite, self.wsk, ws, epoch, list_seq, now_ms(), wk, members)
        cert = CERTS["phone"]["o"]
        grant = R.make_wk_grant(self.suite, self.wsk, ws, bytes.fromhex(DEVICE), crypto.unb64u(cert["dk_kx_pub"]),
                                epoch, wk, secrets.token_bytes(32))  # fmt: skip
        return {"member_list": ml, "grants": [grant]}

    def publish(self, sess: Session, epoch: int, wk: bytes) -> dict[str, Any]:
        return sess.call(
            "PUT", f"/v2/workspaces/{WS}/membership", "PUT /v2/workspaces/{ws}/membership", body=self.body(epoch, wk),
            holder=True, request_cap=2 << 20,
        )  # fmt: skip


class RefDevice:
    """The phone, as the relay's reference client: device login, its grant, request envelopes (§8.1) and the §8.8
    checks of every answer chunk (``device_check``)."""

    def __init__(self, relay: RelayProcess, R: ModuleType) -> None:
        self.relay, self.R, self.http = relay, R, Http(relay)
        self.suite = R.SUITES[2]
        self.sig = self.suite.sig_key(bytes.fromhex(corpus.KEYS["phone.sig"]["seed"]))
        self.kx = self.suite.kx_key(bytes.fromhex(corpus.KEYS["phone.kx"]["seed"]))
        self.wk: dict[int, bytes] = {}
        self.token: str | None = None
        self.seq = 0

    def login(self) -> None:
        ch = self.http.challenge()
        # ref has no auth_bytes; core's is byte-equal to the relay's proto.auth_bytes (tests/relay/test_wire.py)
        sig = self.suite.sign(self.sig, wire.auth_bytes(self.relay.origin, ch, "device", DEVICE))
        obj = self.http.ok("POST", "/v2/session/login", {
            "challenge": crypto.b64u(ch), "kind": "device", "id": DEVICE, "sig": crypto.b64u(sig),
        })  # fmt: skip
        self.token = obj["token"]

    def fetch_grants(self) -> dict[int, bytes]:
        grants = self.http.ok("GET", f"/v2/workspaces/{WS}/grants", token=self.token)["grants"]
        for g in grants:
            opened = self.R.open_wk_grant(self.suite, corpus.pub("wsk_a.sig"), g, bytes.fromhex(WS),
                                          bytes.fromhex(DEVICE), self.kx)  # fmt: skip
            assert "ok" in opened, opened
            self.wk[opened["ok"]["epoch"]] = bytes.fromhex(opened["ok"]["wk"])
        return self.wk

    def request(
        self, meta: dict[str, Any], *, epoch: int, seq: int | None = None, rid: bytes | None = None,
        ts_ms: int | None = None,
    ) -> tuple[bytes, bytes]:  # fmt: skip
        R = self.R
        if seq is None:
            self.seq += 1
            seq = self.seq
        rid = rid or secrets.token_bytes(16)
        h = R.Header(R.TO_HOST, 0, 2, bytes.fromhex(WS), bytes.fromhex(DEVICE), rid, bytes(16), seq,
                     ts_ms if ts_ms is not None else now_ms(), epoch, secrets.token_bytes(16))  # fmt: skip
        env = R.envelope(self.suite, R.k_bridge(self.wk[epoch], bytes.fromhex(WS), epoch), self.sig, h, R.frame(meta))
        return rid, env

    def post(self, rid: bytes, env: bytes) -> tuple[int, Any]:
        return self.http.call("POST", f"/v2/workspaces/{WS}/bridge/{rid.hex()}", raw=env, token=self.token)

    def answer(self, rid: bytes, epoch: int, *, timeout_s: float = 15.0) -> tuple[list[dict[str, Any]], list[bytes]]:
        """Long-poll this rid's mailbox until a LAST chunk; every chunk goes through ``device_check`` in order.
        Returns ``(accepted results, raw envelopes)``."""
        ctx = {
            "suite": 2, "workspace": WS, "device": DEVICE, "wsk_pub": crypto.b64u(corpus.pub("wsk_a.sig")),
            "wk": {str(e): k.hex() for e, k in self.wk.items()},
            "pending": {rid.hex(): {"next": 0, "stream": False, "epoch": epoch}}, "offset_ms": 0,
        }  # fmt: skip
        results: list[dict[str, Any]] = []
        raws: list[bytes] = []
        after: int | None = None
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            wait = max(1, min(5, int(end - time.monotonic())))
            q = f"wait={wait}" if after is None else f"after={after}&wait={wait}"
            obj = self.http.ok("GET", f"/v2/workspaces/{WS}/bridge/{rid.hex()}?{q}", token=self.token)
            for ch in obj["chunks"]:
                env = crypto.unb64u(ch["envelope"])
                mailbox = {"id": ch["id"], "idx": ch["idx"], "last": ch["last"], "stream": ch["stream"]}
                res = self.R.device_check(env, ctx, mailbox, now_ms())
                assert res.get("result") == "accept", res
                results.append(res)
                raws.append(env)
                after = ch["idx"]
                if res["last"]:
                    return results, raws
        raise AssertionError(f"no complete answer for rid {rid.hex()} within {timeout_s} s ({len(results)} chunks)")


def answer_of(results: list[dict[str, Any]]) -> tuple[dict[str, Any], Any]:
    """``(meta of chunk 0, decoded data)`` of a complete, device-checked answer."""
    data = b"".join(bytes.fromhex(r["data"]) for r in results)
    return results[0]["meta"], (json.loads(data) if data else None)


def vector_phone() -> Phone:
    """The bridge kit's Phone (event signing, the R6 fixture's Member) over the vector phone identity."""
    p = Phone.__new__(Phone)
    p.pk, p.dk = corpus.priv("person_alice.sig"), corpus.priv("phone.sig")
    p.pk_pub, p.dk_pub = corpus.pub("person_alice.sig"), corpus.pub("phone.sig")
    p.cert = CERTS["phone"]
    p.device, p.person, p.seq = DEVICE, PERSON, 0
    return p


class CountingLoop(BridgeLoop):
    """The R2 loop, recording every chunk its poster sends (rid hex, envelope) before the real POST."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.posted: list[tuple[str, bytes]] = []

    def _poster(self, conn: Any) -> Any:
        inner = super()._poster(conn)

        def post(device: str, rid: str, env: bytes) -> str:
            self.posted.append((rid, env))
            return inner(device, rid, env)

        return post

    def for_rid(self, rid: bytes) -> list[bytes]:
        return [e for r, e in self.posted if r == rid.hex()]


class HostSide:
    """The host: a core R1 Session holding workspace A, the R2 Bridge over a fresh state directory with the bridge
    kit's durable FakeHost (real clock), the R6 FIXTURE view (epochs and the member list), and the loop."""

    def __init__(self, relay: RelayProcess, tmp: Path, sess: Session, *, epochs: Epochs | None = None) -> None:
        self.relay, self.sess = relay, sess
        self.epochs = epochs or Epochs()
        self.phone = vector_phone()
        self.r6 = R6(self.epochs)
        self.r6.add(self.phone, SCOPES)
        (tmp / "host").mkdir(exist_ok=True)
        self.host = FakeHost(tmp / "host", clock=now_ms)
        self.state = StateDir(tmp / "relay")
        self.rids = RidStore(self.state)
        self.alarms: list[tuple[str, str]] = []
        keys = BridgeKeys(self.epochs, bytes.fromhex(WS))
        signer = sign.RelaySigner(Key(), sign.LinkContext(WS, relay.origin), epochs=keys)
        self.bridge = Bridge(
            workspace_id=WS, rids=self.rids, keys=keys, signer=signer, r6=self.r6, port=self.host,
            job=lambda fn: fn(), post=_no_post, now_ms=now_ms, alarm=lambda c, d: self.alarms.append((c, d)),
        )  # fmt: skip

        def conn() -> rhttp.RelayConn:
            c = rhttp.RelayConn(relay.origin)
            OPEN_CONNS.append(c)
            return c

        self.loop = CountingLoop(self.bridge, sess, conn=conn, workspace_id=WS)

    def start(self) -> None:
        """Start the loop and wait until the bridge has started (its start fence is stamped once the hold succeeded,
        before the responder thread is spawned): a request stamped after this returns is not fenced."""
        self.loop.start()
        resp = f"orch-relay-resp-{self.loop.id8}"
        assert wait_for(lambda: any(t.name == resp for t in threading.enumerate()), timeout=10), self.loop.error
        time.sleep(0.002)  # the fence is `ts_ms < start_ms`, in whole milliseconds

    def close(self) -> None:
        self.loop.stop()
        joined = self.loop.join(10)
        self.rids.close()
        self.state.close()
        assert joined, "a relay thread outlived stop()"


def _no_post(device: str, rid: str, env: bytes) -> str:
    raise AssertionError("posted before the loop installed its poster")


def wait_for(pred: Any, timeout: float = 15.0, step: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())
