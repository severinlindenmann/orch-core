"""A web API on real parts (keys, access, channels, audit chain, custodial engine, wk seals); the registry, the shares
reader and the workspace view are small fakes (their owners are other tasks)."""

from __future__ import annotations

import json
from typing import Any

from channels_helpers import Env as ChEnv
from channels_helpers import new_channel, prove
from custodial_helpers import WK, WS, StubView, core_seal

from orch_hub.contract import hub as H
from orch_hub.contract import keystore as KS
from orch_hub.contract.canon import b64u
from orch_hub.custodial import CustodialEngine
from orch_hub.link.wkseal import WkSeals
from orch_hub.webapi import SessionBridge, WebApi, WebDeps

ORIGIN = "http://127.0.0.1:9000"
WS_SEL = "cd" * 16
WS_OTHER = "ef" * 16


class FakeRegistry:
    def __init__(self) -> None:
        self.ws: dict[str, dict[str, Any]] = {}

    def add(
        self,
        ws: str,
        mode: str,
        owner: str | None = None,
        status: str = "bound",
        name: str | None = None,
        left_ms: int | None = None,
    ) -> None:
        self.ws[ws] = {"mode": mode, "owner": owner, "status": status, "name": name, "left_ms": left_ms}

    def profile(self, ws):
        return self.ws.get(ws, {}).get("profile")

    def status(self, ws):
        return {"version": None, "commit": None, "host": None, "address": "", "last_seen_ms": None}

    def unbound_ms(self, ws):
        r = self.ws.get(ws)
        return r["left_ms"] if r and r["status"] == "unbound" else None

    def forget(self, ws):
        self.ws.pop(ws, None)

    def pins(self):
        return {w: {} for w in self.ws}

    def binding_state(self, ws):
        r = self.ws.get(ws)
        return None if r is None else {"status": r["status"], "binding": {"mode": r["mode"]}}

    def last_mode(self, ws):
        return self.ws[ws]["mode"] if ws in self.ws else None

    def mode(self, ws):
        r = self.ws.get(ws)
        return r["mode"] if r and r["status"] == "bound" else None

    def owner_alias(self, ws):
        return self.ws[ws]["owner"]

    def card(self, ws):
        return {"name": self.ws[ws]["name"]} if self.ws[ws]["name"] else {}


class FakeShares:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], list[dict]] = {}

    def list_shared(self, user_id, workspace_id):
        return self.items.get((user_id, workspace_id), [])


class FakeView:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.detail: dict | None = None

    def needs_you(self, workspace_id, user_id):
        return self.rows

    def item(self, workspace_id, user_id, item_id):
        return self.detail


def shared_item(
    item_id: str,
    kind: str = "question",
    title: str = "CSV or API?",
    *,
    chain: dict | None = None,
    f1: dict | None = None,
) -> dict:
    content = {"needs_you": {"type": "question", "title": title, "deadline": None}}.get(
        kind, {"key": "T-1", "type": "task", "id": "Q1", "title": title, "text": "x", "deadline": None}
    )
    o = {"item_id": item_id, "item_version": 1, "item_kind": kind, "content": content}
    if chain is not None:
        o["chain"] = chain
    if f1 is not None:
        o["f1"] = f1
    return {
        "item": o,
        "hash": "sha256:" + "11" * 32,
    }


class Web:
    def __init__(self, tmp_path) -> None:
        self.env = ChEnv(tmp_path)
        self.clock = self.env.clock
        e = self.env
        d = tmp_path / "seal"
        d.mkdir(mode=0o700)
        self.seals = WkSeals(
            str(d / "wk-seal.db"), hub_id=e.keys.hub_id, hxk_pub=e.keys.hxk_pub, exchange=e.keys.hxk_exchange
        )
        self.seals.accept(WS, 1, core_seal(WK, e.keys.hub_id, WS, 1, e.keys.hxk_pub))
        self.reg, self.shares, self.view, self.stub = FakeRegistry(), FakeShares(), FakeView(), StubView()
        self.reg.add(WS, "full")
        self.delivered: list[Any] = []
        self.engine = CustodialEngine(
            keys=e.keys,
            snapshots=e.snaps,
            seals=self.seals,
            view=self.stub,
            channels=e.svc,
            sessions=SessionBridge(e.svc),
            audit=e.audit,
            deliver=lambda ws, log, ev: self.delivered.append((ws, log, ev)),
            mode_of=lambda ws: self.reg.mode(ws),
            now_ms=self.clock,
            dev_mode=True,
        )
        self.api = WebApi(
            WebDeps(
                keys=e.keys,
                access=e.acc,
                channels=e.svc,
                registry=self.reg,
                audit=e.audit,
                engine=self.engine,
                shares=self.shares,
                view=self.view,
                allowed_origins=frozenset({ORIGIN}),
                now_ms=self.clock,
            )
        )
        e.acc.observer = e.audit.access_change  # as serve wires it (D4)
        self.browser_tokens: set[str] = set()  # sessions made by login(): sent as the cookie, as a browser does
        self.admin = e.user("admin")
        e.acc.add_member("hub-admin", self.admin)
        self.ann = e.user("ann")
        e.acc.add_group("team")
        e.acc.add_member("team", self.ann)
        e.acc.grant(WS, "team", "edit")

    def close(self) -> None:
        self.seals.close()
        self.env.close()

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        token: str | None = None,
        cookie: bool | None = None,
        origin: str | None = ORIGIN,
        csrf: bool = True,
        ctype: str | None = "application/json",
        host: str | None = None,
        peer: str = "",
    ):
        h: dict[str, str] = {}
        if host or self.api._hosts:
            h["host"] = host or "127.0.0.1:9000"
        if cookie is None:  # a browser session goes as the cookie unless the test says bearer (cookie=False)
            cookie = token in self.browser_tokens
        if token and cookie:
            h["cookie"] = f"{self.api._cookie_name}={token}"
        elif token:
            h["authorization"] = f"Bearer {token}"
        if origin:
            h["origin"] = origin
        if csrf:
            h["x-orch-csrf"] = "1"
        raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        if raw and ctype:
            h["content-type"] = ctype
        status, hdrs, out = self.api.handle(method, "/api/v1" + path, h, raw, peer)
        return status, json.loads(out), dict(hdrs)

    def login(self, user: str, client: str = "browser") -> str:
        """A session token. The default is a browser (``full``) session, which ``call`` sends as the cookie; ``"app"``
        is the phone's PAIRING session (bearer): it can only pair and bind (protocol §6.4)."""
        s, o, h = self.call(
            "POST",
            "/session",
            {"user_id": user, "password": "correct horse battery", "totp": self.env.next_code(user), "client": client},
            origin=None if client == "app" else ORIGIN,
        )
        assert s == 200, o
        if client == "app":
            return o["token"]
        tok = h["Set-Cookie"].split(";")[0].split("=", 1)[1]
        self.browser_tokens.add(tok)
        return tok

    def creds(self, user: str) -> dict[str, str]:
        """The password + a fresh TOTP step, as the browser re-enters them to register a key (A8-3)."""
        return {"password": "correct horse battery", "totp": self.env.next_code(user)}

    def register_channel(self, token: str, user: str | None = None):
        """A browser channel registered through the HTTP API. Returns (signer, channel_id)."""
        signer = new_channel()
        s, begun, _ = self.call(
            "POST",
            "/channels/register/begin",
            {"kind": "browser", "key_pub": b64u(signer.public), **self.creds(user or self.env.svc.session_user(token))},
            token=token,
        )
        assert s == 200, begun
        s, got, _ = self.call(
            "POST",
            "/channels/register/complete",
            {"challenge_id": begun["challenge_id"], "signed": prove(signer, begun)},
            token=token,
        )
        assert s == 200, got
        return signer, got["channel_cert"]["o"]["channel_id"]

    def proof(self, signer: KS.ChannelSigner, digest_hex: str) -> str:
        return b64u(signer.proof(bytes.fromhex(digest_hex)))


__all__ = ["ORIGIN", "WS", "WS_OTHER", "WS_SEL", "H", "Web", "shared_item"]
