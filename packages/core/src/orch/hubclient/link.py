"""The host end of the hub link (protocol section 10.2): signed ``hub.link`` requests out, verified replies in.

:class:`HubTransport` is what C5 (the real TLS link) implements; tests use an in-process one. A transport moves one
signed message and returns the signed reply, or raises ``OSError`` when the hub cannot be reached. The ops this
module lets a host send are closed; ``wk_seal`` exists only in full mode, so a selective link refuses it here
(``wrong_mode``) and the hub never sees WK material (spec 8a)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from orch.crypto.encoding import b64u

from . import wire
from .errors import HubRefused

__all__ = ["HubLink", "HubTransport", "LINK_WINDOW_MS", "W2H_OPS"]

LINK_WINDOW_MS = 120_000
#: w2h op -> the mode it exists in (None: both). Anything else is ``unknown_op`` and is never sent.
W2H_OPS: dict[str, str | None] = {
    "join": None,
    "card_put": None,
    "binding_put": None,
    "snapshot_get": None,
    "publish_out": None,
    "route_out": None,
    "audit_ack": None,
    "space_list": None,
    "space_accept": None,
    "space_put": None,
    "space_revoke": None,
    "space_get": None,
    "switch_ticket": None,
    "switch_redeem": None,
    "rail_get": None,
    "owner_get": None,
    "share_put": "selective",
    "share_revoke": "selective",
    "wk_seal": "full",
    "log_sync": "full",
}


class HubTransport(Protocol):
    def send(self, message: dict[str, Any]) -> dict[str, Any]:
        """Deliver one signed w2h ``hub.link`` object; return the signed h2w reply. ``OSError`` if unreachable."""
        ...


class HubLink:
    def __init__(
        self,
        transport: HubTransport,
        *,
        hub_id: str,
        workspace_id: str,
        hk_op_pub: bytes,
        sign: wire.Sign,
        mode: str,
        now_ms: Callable[[], int],
    ) -> None:
        if mode not in wire.MODES:
            raise HubRefused("malformed")
        self._t, self.hub_id, self.workspace_id = transport, hub_id, workspace_id
        self._hk_op, self._sign, self.mode, self._now = hk_op_pub, sign, mode, now_ms
        self._seq = 0

    def set_hk_op(self, hk_op_pub: bytes) -> None:
        """Verify replies from now on under another op key (the host adopted a rotated hub card)."""
        self._hk_op = hk_op_pub

    def call(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        """Send one request and return the reply body. A hub refusal raises ``HubRefused(code)``; an unreachable hub
        raises ``HubRefused("hub_unreachable")``."""
        if op not in W2H_OPS:
            raise HubRefused("unknown_op")
        if W2H_OPS[op] is not None and W2H_OPS[op] != self.mode:
            raise HubRefused("wrong_mode")
        sent: dict[str, Any] = {}

        def build(seq: int) -> dict[str, Any]:
            sent["o"] = o = self._request(op, body, seq)
            return {"o": o, "sig": b64u(self._sign(wire.SIG_LINK + wire.cj(o)))}

        try:
            send_next = getattr(self._t, "send_next", None)
            if send_next is not None:  # a real link: ITS counter numbers every frame (calls and acks alike)
                reply = send_next(op, build)
            else:  # a bare transport (fakes): this link object's own counter
                self._seq += 1
                reply = self._t.send(build(self._seq))
        except OSError:
            raise HubRefused("hub_unreachable") from None
        r = self._open(reply, sent["o"])
        if "error" in r["body"]:
            code = r["body"]["error"]
            raise HubRefused(code if type(code) is str and 0 < len(code) <= 40 else "malformed")
        return r["body"]

    def _request(self, op: str, body: dict[str, Any], seq: int) -> dict[str, Any]:
        return {
            "v": 2,
            "suite": wire.SUITE_ID,
            "kind": "hub.link",
            "hv": wire.HV,
            "hub_id": self.hub_id,
            "workspace_id": self.workspace_id,
            "dir": "w2h",
            "seq": seq,
            "ts_ms": self._now(),
            "op": op,
            "re": None,
            "body": body,
        }

    def _open(self, reply: Any, want: dict[str, Any]) -> dict[str, Any]:
        r = wire._envelope(reply, "hub.link")
        wire._signature(reply, wire.SIG_ROUTE, self._hk_op)
        wire._fields(r, "hub.link")
        if r["dir"] != "h2w" or r["re"] != want["seq"] or r["op"] not in (want["op"], "error"):
            raise HubRefused("malformed")
        if r["hub_id"] != self.hub_id:
            raise HubRefused("other_hub")
        if r["workspace_id"] != self.workspace_id:
            raise HubRefused("other_workspace")
        if abs(self._now() - r["ts_ms"]) > LINK_WINDOW_MS:
            raise HubRefused("stale_timestamp")
        return r
