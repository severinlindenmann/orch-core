"""An in-process fake relay for the relay-client tests: no sockets. It checks every signed request with the contract's
reference verifier (``check_relay_request``, the same logic orch-relay's ``HubAccounts.authenticate`` ports, §10.8, in
the same order), then the route and its body with ``relayroutes.route`` (§10.8.1), then keeps the state the routes
describe. ``faults`` is a queue of (status, headers, body) answered instead of the real handling, or an exception."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from orch_hub.contract import hub as H
from orch_hub.contract import phase4 as P4
from orch_hub.contract import relayroutes as RR
from orch_hub.contract.canon import b64u, cj, unb64u

STATUS = {
    "malformed": 400,
    "other_hub": 403,
    "stale_timestamp": 401,
    "unknown_hub": 401,
    "stale_op_cert": 401,
    "bad_hub_signature": 401,
    "replay": 401,
    "not_found": 404,
    "too_large": 413,
    "message_conflict": 409,
}


class FakeRelay:
    def __init__(self, clock, *, dev_mode: bool = True) -> None:
        self.clock, self.dev_mode = clock, dev_mode
        self.cards: dict[str, Any] = {}
        self.nonces: dict[str, dict[str, int]] = {}
        self.targets: dict[str, dict[str, Any]] = {}  # channel_id -> {env, topic, token}
        self.mailbox: dict[tuple[str, str], dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []  # the APNs payloads: cj(outer) bytes as the phone would get them
        self.seen_ids: set[tuple[str, str]] = set()
        self.log: list[tuple[str, str]] = []
        self.faults: list[Any] = []
        self.nonce_log: list[str] = []
        self.push_configured = True  # False: a relay without a complete push configuration (§10.8.1, E5R-1)
        self.revoked: set[str] = set()  # channels the hub's mailbox purge recorded as revoked (§10.8.4)

    def _json(self, status: int, o: Mapping[str, Any], headers: Mapping[str, str] | None = None):
        return status, dict(headers or {}), cj(dict(o))

    def __call__(self, method: str, target: str, headers: Mapping[str, str], body: bytes, timeout: float):
        self.log.append((method, target))
        if self.faults:
            f = self.faults.pop(0)
            if isinstance(f, BaseException):
                raise f
            return f
        now = self.clock()
        if (method, target) == ("POST", "/v2/hubs"):
            card = json.loads(body)
            try:
                H.verify_card(card, now_ms=now, dev_mode=self.dev_mode)
            except H.Refused as r:
                return self._json(400, {"error": r.code})
            hid = card["o"]["hub_id"]
            created = hid not in self.cards
            self.cards[hid] = card
            return self._json(200, {"hub_id": hid, "op_cert_id": card["op_cert"]["o"]["cert_id"], "created": created})
        req = {"method": method, "target": target, "headers": dict(headers), "body": body}
        st = {"hub_cards": self.cards, "nonces": self.nonces}
        try:
            ok = P4.check_relay_request(req, st, now_ms=now, dev_mode=self.dev_mode)
        except H.Refused as r:
            return self._json(STATUS[r.code], {"error": r.code, **r.extra})
        self.nonces.setdefault(ok["hub"], {})[ok["nonce"]] = now
        self.nonce_log.append(ok["nonce"])
        try:
            r = RR.route(method, target, ok["hub"], body, push_configured=self.push_configured)
        except H.Refused as e:
            return self._json(STATUS[e.code], {"error": e.code})
        return self._run(ok["hub"], r, body)

    def _run(self, hub: str, r: dict[str, Any], body: bytes):
        ch, name = r["channel_id"], r["route"]
        o = json.loads(body) if body else {}
        if name == "push_put":
            self.targets[ch] = {"env": o["env"], "topic": o["topic"], "token": unb64u(o["token"])}
            return self._json(200, {"channel_id": ch, "registered": True})
        if name == "push_delete":
            self.targets.pop(ch, None)
            return self._json(200, {"channel_id": ch, "registered": False})
        if name == "push_send":
            key = (ch, o["outer"]["id"])
            if ch not in self.targets or key in self.seen_ids:
                return self._json(200, {"accepted": 0})
            self.seen_ids.add(key)
            self.sent.append(
                {"channel_id": ch, "outer": cj(o["outer"]), "priority": o["priority"], "ttl_s": o["ttl_s"]}
            )
            return self._json(200, {"accepted": 1})
        if name == "mailbox_put":
            key = (ch, r["id"])
            held = self.mailbox.get(key)
            if held is not None and held["c"] != o["c"]:
                return self._json(409, {"error": "message_conflict"})
            self.mailbox.setdefault(key, {"c": o["c"], "expires_ms": self.clock() + o["ttl_s"] * 1000})
            return self._json(200, {"id": r["id"], "stored": True, "expires_ms": self.mailbox[key]["expires_ms"]})
        if name == "mailbox_get":
            m = self.mailbox.get((ch, r["id"]))
            if m is None:
                return self._json(404, {"error": "not_found"})
            return self._json(200, {"id": r["id"], **m})
        if name == "mailbox_delete":
            self.mailbox.pop((ch, r["id"]), None)
            return self._json(200, {"id": r["id"], "deleted": True})
        self.revoked.add(ch)  # mailbox_purge: the revocation record (§10.8.4)
        gone = [k for k in self.mailbox if k[0] == ch]
        for k in gone:
            del self.mailbox[k]
        return self._json(200, {"channel_id": ch, "deleted": len(gone)})


__all__ = ["FakeRelay", "b64u"]
