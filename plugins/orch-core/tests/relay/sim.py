"""A small stateful relay for the R1 adversarial tests: sessions, challenges, the holder lease, the card and the genesis
pin, with orch-relay's check order (``directory.login``, ``bridge.hold``, ``directory.card_put``). It runs behind
:class:`tests.relay.mock.MockRelay` as its fallback handler.

Its login check is an **independent** copy of orch-relay's ``proto.auth_bytes`` (not ``orch.wsproto.wire``), so the
origin-binding tests compare core against the relay's construction, not against itself."""

from __future__ import annotations

import secrets
import threading
from typing import Any

from orch import canon, crypto
from orch.wsproto import card as cards

from .mock import Reply, Seen, json_reply


def relay_auth_bytes(origin: str, challenge: bytes, kind: str, ident: str) -> bytes:  # orch-relay proto.auth_bytes
    o = origin.encode("ascii")
    return (
        b"orch/v2/sig/relay-auth|"
        + b"\x02"
        + len(o).to_bytes(2, "big")
        + o
        + challenge
        + bytes([{"device": 1, "workspace": 2}[kind]])
        + bytes.fromhex(ident)
    )


def _err(status: int, code: str, retry_after: int | None = None) -> Reply:
    return json_reply(status, {"error": code}, {"Retry-After": str(retry_after)} if retry_after is not None else None)


class SimRelay:
    def __init__(self, origin: str, *, owner_pk: bytes, now_ms: int = 1_790_000_000_000) -> None:
        self.origin, self.owner_pk, self.now = origin, owner_pk, now_ms
        self.lock = threading.Lock()
        self.challenges: dict[bytes, int] = {}
        self.issued: list[bytes] = []
        self.tokens: dict[str, tuple[str, int]] = {}
        self.cards: dict[str, dict[str, Any]] = {}
        self.holders: dict[str, tuple[int, int]] = {}  # ws -> (gen, lease_until_ms)
        self.genesis: dict[str, bytes] = {}
        self.logins = 0
        self.login_bodies: list[bytes] = []
        self.repeat_challenge: bytes | None = None  # a misbehaving relay serving one challenge twice

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.now += int(seconds * 1000)

    def __call__(self, s: Seen) -> Reply:
        with self.lock:
            return self._route(s)

    # -- routing ------------------------------------------------------------------------------------------------

    def _route(self, s: Seen) -> Reply:
        p = s.path.split("/")
        if s.method == "POST" and s.path == "/v2/session/challenge":
            return self._challenge()
        if s.method == "POST" and s.path == "/v2/session/login":
            return self._login(s)
        if s.method == "POST" and s.path == "/v2/session/logout":
            self.tokens.pop(self._bearer(s) or "", None)
            return json_reply(200, {})
        if len(p) >= 5 and p[1:3] == ["v2", "workspaces"]:
            ws, rest = p[3], "/".join(p[4:])
            if s.method == "POST" and rest == "host/hold":
                return self._hold(s, ws)
            if rest == "card":
                return self._card_put(s, ws) if s.method == "PUT" else self._card_get(s, ws)
            if s.method == "PUT" and rest == "genesis":
                return self._genesis(s, ws)
            if s.method == "GET" and rest in ("members", "checkpoint"):
                return self._auth(s, ws) or _err(404, "not_found")
        return _err(404, "not_found")

    def _bearer(self, s: Seen) -> str | None:
        a = s.headers.get("authorization", "")
        return a[7:] if a.startswith("Bearer ") else None

    def _auth(self, s: Seen, ws: str) -> Reply | None:
        t = self.tokens.get(self._bearer(s) or "")
        if t is None or t[1] <= self.now or t[0] != ws:
            return _err(401, "unauthenticated")
        return None

    def _fence(self, s: Seen, ws: str) -> Reply | None:
        h = self.holders.get(ws)
        g = s.headers.get("orch-holder")
        if h is None or g is None or not g.isdigit() or int(g) != h[0] or h[1] <= self.now:
            return _err(409, "fenced")
        return None

    # -- sessions -----------------------------------------------------------------------------------------------

    def _challenge(self) -> Reply:
        ch = self.repeat_challenge or secrets.token_bytes(32)
        self.challenges[ch] = self.now + 60_000
        self.issued.append(ch)
        return json_reply(200, {"challenge": crypto.b64u(ch), "expires_ms": self.now + 60_000})

    def _login(self, s: Seen) -> Reply:
        self.login_bodies.append(s.body)
        body = canon.loads_strict(s.body)
        try:
            ch = crypto.unb64u(body["challenge"], 32)
        except (crypto.EncodingError, KeyError):
            return _err(400, "malformed")
        expires = self.challenges.pop(ch, None)  # removed whatever happens next
        if expires is None:
            return _err(401, "unknown_challenge")
        if self.now >= expires:
            return _err(401, "expired")
        if body.get("kind") not in ("device", "workspace") or set(body) != {"challenge", "kind", "id", "sig"}:
            return _err(400, "malformed")
        stored = self.cards.get(body["id"])
        pub = crypto.unb64u(stored["o"]["wsk_pub"]) if stored else crypto.public_bytes(crypto.generate_private_key())
        sig = crypto.unb64u(body["sig"])
        if not crypto.verify(pub, sig, relay_auth_bytes(self.origin, ch, body["kind"], body["id"])) or not stored:
            return _err(401, "bad_signature")
        self.logins += 1
        token = crypto.b64u(secrets.token_bytes(32))
        self.tokens[token] = (body["id"], self.now + 900_000)
        return json_reply(200, {"token": token, "expires_ms": self.now + 900_000})

    def expire_tokens(self) -> None:
        with self.lock:
            self.tokens.clear()

    # -- holder -------------------------------------------------------------------------------------------------

    def _hold(self, s: Seen, ws: str) -> Reply:
        if (r := self._auth(s, ws)) is not None:
            return r
        body = canon.loads_strict(s.body)
        gen, take_over = body["gen"], body["take_over"]
        row = self.holders.get(ws)
        if gen is not None:
            if row is None or row[0] != gen or row[1] <= self.now:
                return _err(409, "fenced")
        elif row and row[1] > self.now and not take_over:
            return _err(409, "holder_conflict")
        else:
            gen = row[0] + 1 if row else 1
        self.holders[ws] = (gen, self.now + 40_000)
        return json_reply(200, {"gen": gen, "lease_s": 40, "cursor": 0})

    # -- card and genesis ---------------------------------------------------------------------------------------

    def _card_put(self, s: Seen, ws: str) -> Reply:
        c = canon.loads_strict(s.body)["card"]
        stored = self.cards.get(ws)
        if stored is not None:
            if (r := self._auth(s, ws) or self._fence(s, ws)) is not None:
                return r
        prev = {"o": stored["o"], "delegation": stored["delegation"]["o"]} if stored else None
        try:
            o = cards.accept_card(prev, c, self.owner_pk)
        except cards.CardRefused as e:
            return _err(409 if e.code in ("stale_card", "wsk_changed", "delegation_changed") else 400, e.code)
        if o["workspace_id"] != ws:
            return _err(409, "other_workspace")
        self.cards[ws] = c
        return json_reply(200, {"card_seq": o["card_seq"]})

    def _card_get(self, s: Seen, ws: str) -> Reply:
        if (r := self._auth(s, ws)) is not None:
            return r
        c = self.cards.get(ws)
        return json_reply(200, {"card": c}) if c else _err(404, "not_found")

    def _genesis(self, s: Seen, ws: str) -> Reply:
        if (r := self._auth(s, ws) or self._fence(s, ws)) is not None:
            return r
        held = self.genesis.get(ws)
        if held is not None and held != s.body:
            return _err(409, "genesis_mismatch")
        self.genesis[ws] = s.body
        return json_reply(200, {})
