"""R3 T2 (#1591): a click signer over a served workspace that is called the way the dashboard's adapter calls it from a
remote-device stream. The host's ``remote.stream`` (T1) and ``remote.presence_used`` are faked here; the interface
assumed (see ``Streams.facts``): ``remote.stream {stream_id}`` answers ``{workspace_id, device_id, person, level, hh
(b64u 32 bytes), presence_kind, presence_pub (b64u 65 bytes), counter (int)}``, a closed or unknown stream is
``remote.no_stream``; ``remote.presence_used {stream_id, device_id, count, n}`` answers ``{}`` or ``remote.presence_replay``."""

from __future__ import annotations

import hashlib
from typing import Any

from orch.click import presence as P
from orch.click.protocol import ClickError
from orch.crypto import suite
from orch.crypto.encoding import b64u

SID = "5" * 32
HH = bytes(range(1, 33))
DEVICE = "rd_" + "6" * 32


class Streams:
    """The host's view of the streams (the seam ``ClickSigner(streams=...)``) and of ``remote.presence_used``."""

    def __init__(self, person: str, workspace_id: str, kind: str = "platform") -> None:
        self.key = suite.generate_private_key()
        self.person, self.workspace_id, self.kind = person, workspace_id, kind
        self.open: dict[str, dict[str, Any]] = {}
        self.used: list[dict[str, Any]] = []
        self.counter = 0
        self.asked: list[str] = []
        self.add(SID)

    def add(self, sid: str, **over: Any) -> None:
        self.open[sid] = {
            "workspace_id": self.workspace_id, "device_id": DEVICE, "person": self.person, "level": "work",
            "hh": b64u(HH), "presence_kind": self.kind, "presence_pub": b64u(suite.public_bytes(self.key)),
            "counter": self.counter, **over,
        }  # fmt: skip

    def facts(self, entry: dict[str, Any], sid: str) -> dict[str, Any]:
        self.asked.append(sid)
        if sid not in self.open:
            raise ClickError("remote.no_stream", "no such stream")
        return dict(self.open[sid], counter=self.counter)

    def used_call(self, args: dict[str, Any]) -> dict[str, Any]:
        if args["count"] <= self.counter and self.kind == "fido2":
            raise ClickError("remote.presence_replay", "replayed")
        self.counter = args["count"]
        self.used.append(args)
        return {}

    # -- the device -----------------------------------------------------------------------------------------------
    def assertion(self, nonce: bytes, digests: list[bytes], *, hh: bytes = HH, count: int = 1, flags: int = 0x05) -> dict:
        msg = P.presence_bytes(self.workspace_id, DEVICE, hh, nonce, digests)
        if self.kind == "platform":
            return {"form": "p256", "sig": b64u(suite.sign(self.key, msg))}
        auth = hashlib.sha256(b"orch-remote").digest() + bytes([flags]) + count.to_bytes(4, "big")
        return {
            "form": "ctap2", "auth_data": b64u(auth),
            "sig": b64u(suite.sign(self.key, auth + hashlib.sha256(msg).digest())),
        }  # fmt: skip


def digest_text(raw: bytes) -> str:
    return "sha256:" + raw.hex()


class World:
    """The Click helper of ``test_click`` with the stream seams faked, and calls as the adapter makes them."""

    def __init__(self, served: Any, click: Any, streams: Streams) -> None:
        self.served, self.click, self.streams = served, click, streams
        self.signer = click.signer
        self.wid = served.wid

    def call(self, op: str, /, *, sid: str = SID, **args: Any) -> dict[str, Any]:
        return self.signer.handle(op, {"workspace_id": self.wid, "remote": True, "stream": sid, **args})

    def refusal(self, op: str, /, **args: Any) -> ClickError:
        try:
            self.call(op, **args)
        except ClickError as exc:
            return exc
        raise AssertionError(f"{op} was not refused")

    def prepare(self, op: str, inner: dict[str, Any], **kw: Any) -> dict[str, Any]:
        from orch.host.control_client import new_idem

        return self.call("prepare", op=op, args=inner, idem=new_idem(), **kw)

    def review(self, rid: str) -> str:
        v = self.call("view", request_id=rid)
        self.click.mono.t += 2
        return v["view_digest"]

    def sign(self, rid: str, digest: str, **kw: Any) -> dict[str, Any]:
        return self.call("sign", request_id=rid, view_digest=digest, **kw)

    def present(self, digests: list[str], *, sid: str = SID, **kw: Any) -> dict[str, Any]:
        """The device's side of a presence: ask for a nonce, sign the sorted batch, post it."""
        nonce = self.call("presence_nonce", sid=sid)["nonce"]
        raw = sorted({bytes.fromhex(d[7:]) for d in digests})
        return self.call(
            "presence", sid=sid, nonce=nonce, digests=[digest_text(d) for d in raw],
            assertion=self.streams.assertion(__import__("base64").urlsafe_b64decode(nonce + "=="), raw, **kw),
        )  # fmt: skip


def make_world(served: Any, kind: str = "platform") -> World:
    from tests.host.test_click import Click

    click = Click(served)
    streams = Streams(served.lab.ws.owner.ref, served.wid, kind)
    click.signer._streams = streams.facts  # noqa: SLF001
    real = click.signer._call  # noqa: SLF001

    def call(entry: dict[str, Any], op: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
        if op == "remote.presence_used":
            return {"data": streams.used_call(args)}
        return real(entry, op, args, **kw)

    click.signer._call = call  # noqa: SLF001
    return World(served, click, streams)
