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
    "org_skills_get": None,
    "mcp_servers_get": None,
    "org_secrets_get": None,
    "secret_use": None,
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
    "hub_where": None,
    "agent_events": "full",
    "hook_put": None,
    "hook_revoke": None,
    "hook_pull": None,
    "hook_ack": None,
    "status_put": None,
    "addon_request": None,
    "profile_put": None,
    "directory_get": None,
    "audience_get": None,
    "shared_get": None,
    "share_put": "selective",
    "share_revoke": "selective",
    "wk_seal": "full",
    "log_sync": "full",
}


class HubTransport(Protocol):
    def send_next(self, op: str, build: Callable[[int, bytes], dict[str, Any]]) -> dict[str, Any]:
        """Number one w2h request (the link's own counter, 1 on a new connection), let ``build(seq, exporter)`` sign it
        over the connection's TLS exporter, deliver it and return the signed h2w reply. ``OSError`` if unreachable."""
        ...


#: link failures that carry their own code (``hub.`` + one of these): see :mod:`orch.hubclient.tlslink.errors`
NET_CODES = frozenset({"hub.link_inspected", "hub.proxy_auth", "hub.proxy_refused", "hub.proxy_unreachable"})


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

        def build(seq: int, exporter: bytes) -> dict[str, Any]:
            o = self._request(op, body, seq)
            sent["o"], sent["cb"] = o, exporter
            return {"o": o, "sig": b64u(self._sign(wire.link_signed_bytes(wire.SIG_LINK, exporter, o)))}

        try:
            reply = self._t.send_next(op, build)  # the link's ONE counter numbers every frame (calls and acks alike)
        except OSError as exc:
            net = getattr(exc, "code", None)  # R6: why the network failed (inspected, proxy), not just "unreachable"
            raise HubRefused(net[4:] if net in NET_CODES else "hub_unreachable") from None
        r = self._open(reply, sent["o"], sent["cb"])
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

    def _open(self, reply: Any, want: dict[str, Any], exporter: bytes) -> dict[str, Any]:
        r = wire._envelope(reply, "hub.link")
        wire._link_signature(reply, wire.SIG_ROUTE, exporter, self._hk_op)
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
