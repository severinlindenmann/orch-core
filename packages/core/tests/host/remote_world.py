"""A remote access world for the R3 host tests (#1591): a real served workspace with remote access on, one enrolled
device whose keys the test holds, the dashboard's adapter attached and a fake hub link; the device side of the Noise
handshake, the open message and the app frames are built from ``remote_port`` (the independent reading of the
contract), never from the code under test."""

from __future__ import annotations

import hashlib
from typing import Any

from orch.canon.jcs import dumps, loads_strict
from orch.crypto import suite
from orch.crypto.encoding import b64u, unb64u
from orch.host import remote
from orch.host.remote import adapter, enrollment, gate, pump, session, wrk
from tests.host import remote_port as RP
from tests.host.test_remote_ops import clock_ms, model, run_on, turn_on

HUB = "7b" * 16
KIND = {name: i for i, name in enumerate(RP.KIND) if name}


def frame(kind: str, ident: int, payload: bytes = b"") -> bytes:
    return bytes([KIND[kind]]) + ident.to_bytes(4, "big") + payload


def head(ident: int, path: str = "/api/w/x/runs", method: str = "GET") -> bytes:
    return frame("HEAD", ident, dumps({"headers": [], "method": method, "path": path, "query": ""}))


class Link:
    """The hub link as the pump sees it: frames in, answers and data/close frames out."""

    hub_id = HUB

    def __init__(self) -> None:
        self.frames: list[tuple[str, int, dict[str, Any]]] = []
        self.answers: list[tuple[int, str | None]] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.n = 0

    def next_stream(self, wait: float = 0.0):
        return self.frames.pop(0) if self.frames else None

    def answer_stream_open(self, seq: int, error: str | None = None) -> None:
        self.answers.append((seq, error))

    def send_stream(self, op: str, body: dict[str, Any]) -> None:
        self.sent.append((op, body))

    def data(self, sid: str) -> list[bytes]:
        return [unb64u(b["bytes"]) for op, b in self.sent if op == "stream_data" and b["stream_id"] == sid]

    def closes(self, sid: str) -> list[str]:
        return [b["reason"] for op, b in self.sent if op == "stream_close" and b["stream_id"] == sid]


class Device:
    """The device: an X25519 transport key and a presence key (platform P-256, or FIDO2 with a counter)."""

    def __init__(self, seed: int = 1, kind: str = "platform") -> None:
        self.s, self.e = bytes([seed]) * 32, bytes([seed + 100]) * 32
        self.kind = kind
        self.key = suite.generate_private_key()
        self.ppub = suite.public_bytes(self.key)
        self.count = 0

    def assertion(self, msg: bytes, *, flags: int = 0x05, count: int | None = None, form: str | None = None) -> Any:
        form = form or ("p256" if self.kind == "platform" else "ctap2")
        if form == "p256":
            return {"form": "p256", "sig": b64u(suite.sign(self.key, msg))}
        n = (self.count + 1 if count is None else count).to_bytes(4, "big")
        auth = hashlib.sha256(b"orch-remote").digest() + bytes([flags]) + n
        sig = suite.sign(self.key, auth + hashlib.sha256(msg).digest())
        return {"form": "ctap2", "auth_data": b64u(auth), "sig": b64u(sig)}


class Client:
    """One stream from the device's side."""

    def __init__(self, w: World, sid: str, dev: Device | None = None, did: str | None = None) -> None:
        self.w, self.sid = w, sid
        self.dev, self.did = dev or w.dev, did or w.did
        self.seq = 0
        self.taken = 0
        self.ini: Any = None
        self.hh = b""
        self.challenge = b""

    def open_stream(self, kind: str = "session") -> str | None:
        n = len(self.w.link.answers)
        self.w.link.frames.append(
            ("stream_open", 100 + n, {"stream_id": self.sid, "device_id": self.did, "kind": kind})
        )
        self.w.pump()
        return self.w.link.answers[-1][1]

    def send(self, raw: bytes) -> None:
        self.seq += 1
        self.w.link.frames.append(("stream_data", 0, {"stream_id": self.sid, "seq": self.seq, "bytes": b64u(raw)}))
        self.w.pump()

    def inbox(self) -> list[bytes]:
        out = self.w.link.data(self.sid)[self.taken :]
        self.taken += len(out)
        return out

    def msg1(self, *, ws: str | None = None, sid: str | None = None, hub: str = HUB, did: str | None = None) -> None:
        pro = RP.prologue("session", ws or self.w.ws, did or self.did, sid or self.sid, hub)
        self.ini, m1 = RP.initiator1(pro, self.dev.s, self.dev.e, self.w.wrk_pub)
        self.send(m1)

    def handshake(self) -> None:
        self.msg1()
        (m2,) = self.inbox()
        self.challenge = RP.initiator2(self.ini, m2)
        self.hh = self.ini.hh

    def open_msg(self, **over: Any) -> dict[str, Any]:
        bytes_ = RP.open_bytes(self.w.ws, self.did, self.hh, self.challenge)
        return {"t": "open", "api": [1, 3], "presence": self.dev.assertion(bytes_), **over}

    def say(self, plain: bytes) -> None:
        self.send(self.ini.c_i2r.seal(b"", plain))

    def hear(self) -> list[bytes]:
        return [self.ini.c_r2i.open(b"", ct) for ct in self.inbox()]

    def connect(self) -> dict[str, Any]:
        """Open the stream, handshake, open: returns the host's ``opened`` reply."""
        assert self.open_stream() is None
        self.handshake()
        self.say(dumps(self.open_msg()))
        (reply,) = self.hear()
        return loads_strict(reply)


class World:
    def __init__(self, human, served, *, kind: str = "platform", level: str = "work", cap: str = "work") -> None:
        from tests.host.test_remote_policy import remote_args

        self.served, self.human = served, human
        self.service = served.service
        self.ws = self.service.workspace_id
        turn_on(human, **remote_args(level=level))
        self.dev = Device(1, kind)
        self.did = self.enroll(self.dev, cap)
        self.wrk_pub = RP.xpub(run_on(served, wrk.create, self.service))
        self.person = model(served)["devices"][self.did]["person"]
        self.link = Link()
        self.ad = adapter.claim(self.service, self.person, None)
        self.n = 0
        self.keep_lease = True

    def enroll(self, dev: Device, cap: str = "work") -> str:
        """Enroll through the real pieces: a pending device in memory with ``dev``'s keys, then the confirmation."""
        n = dev.s[0]
        pid, code = "rp_" + f"{n:032x}", "rc_" + f"{n:032x}"
        enrollment._ws(self.service).pending[pid] = enrollment.Pending(  # noqa: SLF001
            pid=pid, workspace_id=self.ws, code_id=code, stream_id="s" * 32, name=f"Laptop {n}",
            presence_kind=dev.kind, transport_pub=b64u(RP.xpub(dev.s)), presence_pub=b64u(dev.ppub),
            fingerprint="0" * 64, words="", sign_count=0, expires_ms=clock_ms(self.served) + 600_000,
        )  # fmt: skip
        _, done, _ = self.human.run("remote.device_confirm", {"pending": pid, "level_cap": cap, "days": 30})
        return done["data"]["device_id"]

    def sid(self) -> str:
        self.n += 1
        return f"{self.n:032x}"

    def client(self, **kw: Any) -> Client:
        return Client(self, self.sid(), **kw)

    def pump(self) -> None:
        while pump.pump(self.service, self.link):
            pass

    def log_types(self) -> list[str]:
        return [e["type"] for e, _ in self.service.store.events_after("workspace", 0, 1 << 30)]

    def log(self, typ: str) -> list[dict[str, Any]]:
        return [e for e, _ in self.service.store.events_after("workspace", 0, 1 << 30) if e["type"] == typ]

    def sess(self, sid: str):
        return session.registry(self.service).get(sid)

    def table(self):
        return gate.table(self.service)

    def advance(self, seconds: float) -> None:
        """Move the clock; the dashboard keeps its lease (it polls every few seconds) unless the test lets it lapse."""
        if not self.keep_lease:
            self.served.lab.ws.clock[0] += seconds
            return
        while seconds > 0:
            step = min(seconds, 10)
            self.served.lab.ws.clock[0] += step
            adapter.claim(self.service, self.person, self.ad.attach_id)
            seconds -= step

    def app(self, c: Client) -> list[dict[str, Any]]:
        """The messages the adapter has been handed so far (taken off its queue)."""
        return self.ad.take(1000)


__all__ = ["HUB", "Client", "Device", "Link", "World", "frame", "head", "remote"]
