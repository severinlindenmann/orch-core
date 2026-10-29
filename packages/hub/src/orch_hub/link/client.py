"""Reference host-side link client (what orch-core C5 implements; also the fake host of the tests).

Steps: dial and handshake TLS 1.3, compare the hub certificate's SPKI hash with the hub card's ``tls_spki_sha256``
(:class:`~orch_hub.link.tls.PinMismatch` otherwise, nothing sent), read the challenge frame (``hub_id`` must be the
card's), sign ``link_auth`` over challenge + this connection's TLS exporter with ``WSK``, send the auth frame, wait for
``ready``. Then :meth:`LinkClient.request` sends a signed w2h message and verifies the signed h2w reply with the
contract's ``verify_link`` under the card's ``HK_op``. See :mod:`orch_hub.link.server` for the frames.
"""

from __future__ import annotations

import time
from typing import Any

from ..contract import hub as H
from ..contract.canon import b64u, unb64u
from ..contract.keystore import WskSigner
from . import framing, tls


class LinkError(Exception):
    """The hub refused: ``code`` is the refusal code (link-level frame, or an op reply's ``{"error"}``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LinkClient:
    def __init__(
        self,
        conn: tls.TlsConn,
        *,
        hub_id: str,
        workspace_id: str,
        wsk: WskSigner,
        hk_op_pub: bytes,
        now_ms=lambda: int(time.time() * 1000),
        timeout: float = 10.0,
        mode: str = "selective",
    ) -> None:
        self._c, self.hub_id, self.workspace_id, self._wsk = conn, hub_id, workspace_id, wsk
        self._hk_op, self._now, self._t, self.mode = hk_op_pub, now_ms, timeout, mode
        self.seq = 0
        self.last_in = 0  # R1: the h2w seq of the previous frame on this link (the first must be 1)
        self.inbox: list[dict[str, Any]] = []  # h2w ``route_in`` requests that arrived while waiting for a reply

    @staticmethod
    def open(
        host: str,
        port: int,
        *,
        tls_spki_sha256: str,
        hub_id: str,
        workspace_id: str,
        wsk: WskSigner,
        hk_op_pub: bytes,
        send_wsk_pub: bool = False,
        timeout: float = 10.0,
        now_ms=lambda: int(time.time() * 1000),
        mode: str = "selective",
    ) -> LinkClient:
        """``send_wsk_pub`` is for the first link of a join (the workspace is not pinned at the hub yet)."""
        conn = tls.connect_pinned(host, port, tls_spki_sha256, timeout=timeout)
        try:
            ch = framing.read_frame(conn, first_timeout=timeout, rest_timeout=timeout)
            if set(ch) != {"t", "hub_id", "challenge"} or ch["t"] != "challenge" or ch["hub_id"] != hub_id:
                raise LinkError(ch.get("error", "other_hub") if ch.get("t") == "error" else "other_hub")
            sig = wsk.link_auth(hub_id, workspace_id, unb64u(ch["challenge"], 32), conn.exporter())
            auth: dict[str, Any] = {"t": "auth", "workspace_id": workspace_id, "sig": b64u(sig)}
            if send_wsk_pub:
                auth["wsk_pub"] = b64u(wsk.public)
            framing.write_frame(conn, auth, timeout=timeout)
            r = framing.read_frame(conn, first_timeout=timeout, rest_timeout=timeout)
            if r.get("t") == "error":
                raise LinkError(str(r.get("error")))
            if r != {"t": "ready", "hub_id": hub_id}:
                raise LinkError("malformed")
        except BaseException:
            conn.close()
            raise
        return LinkClient(
            conn,
            hub_id=hub_id,
            workspace_id=workspace_id,
            wsk=wsk,
            hk_op_pub=hk_op_pub,
            now_ms=now_ms,
            timeout=timeout,
            mode=mode,
        )

    def build(self, op: str, body: dict[str, Any], **override: Any) -> dict[str, Any]:
        self.seq += 1
        o = {
            "v": 2,
            "suite": 2,
            "kind": "hub.link",
            "hv": H.HV,
            "hub_id": self.hub_id,
            "workspace_id": self.workspace_id,
            "dir": "w2h",
            "seq": self.seq,
            "ts_ms": self._now(),
            "op": op,
            "re": None,
            "body": body,
            **override,
        }
        return self._wsk.sign_link(o, self._c.exporter())

    def send_raw(self, obj: dict[str, Any]) -> None:
        framing.write_frame(self._c, obj, timeout=self._t)

    def recv_reply(self) -> dict[str, Any]:
        """The next frame as a verified reply body; an unsigned error frame or a refused signature raises."""
        r = framing.read_frame(self._c, first_timeout=self._t, rest_timeout=self._t)
        if r.get("t") == "error":
            raise LinkError(str(r.get("error")))
        try:
            o = H.verify_link(
                r,
                direction="h2w",
                hub_id_=self.hub_id,
                workspace_id=self.workspace_id,
                mode=self.mode,
                pub=self._hk_op,
                now_ms=self._now(),
                exporter=self._c.exporter(),
            )
        except H.Refused as e:
            raise LinkError(e.code) from None
        if o["seq"] != self.last_in + 1:
            raise LinkError("malformed")
        self.last_in = o["seq"]
        return o

    def request(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        """Send one request; returns the reply body, or raises :class:`LinkError` for ``{"error": code}``."""
        req = self.build(op, body)
        self.send_raw(req)
        o = self.recv_reply()
        while o["re"] is None and o["op"] == "route_in":  # the hub pushed an envelope in between: keep it
            self.inbox.append(o)
            o = self.recv_reply()
        if o["re"] != req["o"]["seq"] or o["op"] != op:
            raise LinkError("malformed")
        if set(o["body"]) == {"error"}:
            raise LinkError(str(o["body"]["error"]))
        return o["body"]

    def route_out(self, envelope: bytes) -> str:
        """Send a ws->ws envelope (the sender's exact bytes). Returns ``"forwarded"`` or ``"queued"``; raises
        :class:`LinkError` with the hub's code (``not_allowed``, ``queue_full``, ``too_large`` ...)."""
        return self.request("route_out", {"envelope": b64u(envelope)})["state"]

    def next_route_in(self) -> bytes:
        """The next envelope the hub delivers (``route_in``), the exact bytes the sender signed. Call :meth:`ack_route_in`
        after the host has stored it; the hub resends an unacknowledged one after a reconnect."""
        o = self.inbox.pop(0) if self.inbox else self.recv_reply()
        if o["re"] is not None or o["op"] != "route_in":
            raise LinkError("malformed")
        self._pending_in = o
        return unb64u(o["body"]["envelope"])

    def ack_route_in(self, error: str | None = None) -> None:
        """Answer the last delivered ``route_in`` (w2h reply, same op, ``re`` = its seq). The hub then sends the next."""
        o = self._pending_in
        self.seq += 1
        reply = {
            "v": 2,
            "suite": 2,
            "kind": "hub.link",
            "hv": H.HV,
            "hub_id": self.hub_id,
            "workspace_id": self.workspace_id,
            "dir": "w2h",
            "seq": self.seq,
            "ts_ms": self._now(),
            "op": "route_in",
            "re": o["seq"],
            "body": {} if error is None else {"error": error},
        }
        self.send_raw(self._wsk.sign_link(reply, self._c.exporter()))

    def close(self) -> None:
        self._c.close()
