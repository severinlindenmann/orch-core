"""Publish add-on, e2e (#296 exit criterion): each workspace publishes in its own namespace, and one cannot touch the
other's shares. Two real workspaces (own host, own keys, own genesis, own state dir) against ONE real local
``orch-publish dev`` server on loopback.

Opt-in like ``test_publishing_real.py`` (not part of the main CI shards): ``e2e/run.sh publish-isolation`` runs it, and
the ``publish-e2e`` CI job runs that. Set ``ORCH_PUBLISH_SRC`` to an orch-publish ``develop`` checkout with its own
``.venv`` to run it by hand. The server is a child process, stopped by the pid in its pid file.

Signing is the terminal path the other tests use (``Human.run``: ``human.prepare``, a signature by the person's device
key over exactly what the review shows, ``human.submit``). Nothing here uses one-click, the keychain or real data."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest

from orch import crypto
from orch.host import publishing
from orch.publishclient import PublishClient, PublishError, flow
from orch.publishclient.client import Transport
from orch.wsproto.pubsign import PublishContext, PublishSigner, publish_signed_bytes
from tests.host.dispatch_helpers import Served, err, ok
from tests.host.human_helpers import Human
from tests.host.lifecycle_helpers import Lab
from tests.host.test_publishing import REPORT, World
from tests.host.test_publishing_real import SRC, Server

pytestmark = [
    pytest.mark.skipif(not SRC, reason="set ORCH_PUBLISH_SRC to an orch-publish checkout with a .venv"),
    pytest.mark.skipif(
        bool(SRC) and "delegation_json" not in (Path(SRC).expanduser() / "pubsrv" / "registry.py").read_text(),
        reason="needs an orch-publish with the card-free register (design 2.6b)",
    ),
]

WS_A = "a1" * 16
WS_B = "b2" * 16
TOKEN = re.compile(r"/s/([0-9a-f]{32})#t=([A-Za-z0-9_-]{43})")


class Tap(Transport):
    """The real network, with every request the hosts send written down (to replay one later)."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def __call__(self, scheme, netloc, method, target, headers, body, timeout):
        self.sent.append({"scheme": scheme, "netloc": netloc, "method": method, "target": target,
                          "headers": dict(headers), "body": body})  # fmt: skip
        return super().__call__(scheme, netloc, method, target, headers, body, timeout)


def raw(tap_or_none, api: int, method: str, target: str, headers: dict[str, str], body: bytes = b"") -> tuple[int, str]:
    """One request exactly as given (no client in between): status and the server's error word."""
    status, data = Transport()("http", f"127.0.0.1:{api}", method, target, headers, body, 10.0)
    try:
        code = json.loads(data).get("error", "")
    except (ValueError, AttributeError):
        code = ""
    return status, code


def forge(world_lab: Lab, api: int, header_ws: str, method: str, target: str, body: bytes = b"", *, sign_ws=None,
          ts=None, nonce=None, host=None) -> tuple[dict[str, str], bytes]:  # fmt: skip
    """Headers for a request signed with the WSK of ``world_lab``'s workspace, for any workspace id in the header or
    path: what a hostile workspace owner (who has the real key of their own workspace) can produce."""
    ts = int(time.time() * 1000) if ts is None else ts
    nonce = os.urandom(16) if nonce is None else nonce
    signed = publish_signed_bytes(
        bytes.fromhex(sign_ws or header_ws), ts, nonce, method, host or f"127.0.0.1:{api}", target, body
    )
    sig = world_lab.ws.signer.sign(signed)
    return {"Orch-Workspace": header_ws, "Orch-Ts": str(ts), "Orch-Nonce": nonce.hex(),
            "Orch-Signature": crypto.b64u(sig)}, body  # fmt: skip


class Side:
    """One workspace: its lab, its host, its person, its World, and a client signing with its real key."""

    def __init__(self, tmp: Path, ws_id: str, srv: Server, tap: Tap) -> None:
        d = tmp / ws_id[:2]
        d.mkdir()
        self.lab = Lab(d, ws_id=ws_id)
        self.served = Served(self.lab)
        assert self.served.wid == ws_id
        self.human = Human(self.served)
        self.world = World(self.served, self.human)
        publishing.TRANSPORT = tap
        self.world.addon()
        self.world.configure(f"http://127.0.0.1:{srv.api}", f"http://127.0.0.1:{srv.share}")
        self.id = ws_id
        self.client = PublishClient(
            PublishSigner(self.lab.ws.signer, PublishContext(ws_id, f"http://127.0.0.1:{srv.api}")), transport=tap
        )

    def close(self) -> None:
        self.served.close()
        self.lab.close()

    def info(self) -> dict[str, Any]:
        return ok(self.world.req("publish.list", {}))["data"]

    def share(self, *names: str, **args: Any) -> tuple[str, str]:
        """Prepare, sign as the person, send. Returns (share id, token): the link is shown once."""
        uid, key = self.world.ticket_with(**{n: REPORT for n in names})
        staged = self.world.prepare(key, *names, **args)["staged_id"]
        self.world.approve(staged)
        link = ok(self.world.send(staged))["data"]["link"]
        m = TOKEN.search(link)
        assert m, link
        return m.group(1), m.group(2)

    def live(self) -> list[dict[str, Any]]:
        return ok(self.world.req("publish.list", {"live": True}))["data"]["shares"]


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setattr(publishing, "_now_ms", lambda service: int(time.time() * 1000))
    srv = Server(tmp_path)
    tap = Tap()
    sides: list[Side] = []
    try:
        a, b = Side(tmp_path, WS_A, srv, tap), Side(tmp_path, WS_B, srv, tap)
        sides += [a, b]
        # the admin pre-binds both workspaces to their owners (the server's admission gate); the pid file is in tmp
        srv.start(
            "--owner", a.info()["owner"], "--owner", b.info()["owner"],
            "--workspace", f"{WS_A}={a.info()['owner']}", "--workspace", f"{WS_B}={b.info()['owner']}",
        )  # fmt: skip
        yield srv, tap, a, b
    finally:
        for s in sides:
            s.close()
        srv.stop()
        publishing.TRANSPORT = None


def test_two_workspaces_each_in_their_own_namespace(arena):
    srv, tap, a, b = arena
    assert a.info()["workspace"] == WS_A and b.info()["workspace"] == WS_B
    assert a.info()["owner"] != b.info()["owner"]

    # 1. both register (the genesis delegation, no card) and each shares a file of its own
    for s in (a, b):
        assert ok(s.world.req("publish.register", {}))["data"]["registered"] is True
    a_id, a_token = a.share("a-report.md", days=3)
    b_id, b_token = b.share("b-report.md", days=3)
    assert a_id != b_id
    assert [r["id"] for r in a.live()] == [a_id] and [r["id"] for r in b.live()] == [b_id]  # own namespace only

    # 2. B reads A's list: its own list holds only B's; asking for A's namespace is refused
    assert [r["id"] for r in flow.list_shares(b.client, WS_B)] == [b_id]
    h, _ = forge(b.lab, srv.api, WS_B, "GET", f"/v1/ws/{WS_A}/shares", sign_ws=WS_B)
    assert raw(None, srv.api, "GET", f"/v1/ws/{WS_A}/shares", h) == (403, "wrong_workspace")
    h, _ = forge(b.lab, srv.api, WS_A, "GET", f"/v1/ws/{WS_A}/shares")  # header says A, signed by B's key
    assert raw(None, srv.api, "GET", f"/v1/ws/{WS_A}/shares", h) == (401, "bad_signature")

    # 3. B revokes / modifies A's share by id: 404, the same answer as for an id that does not exist
    with pytest.raises(PublishError) as e:
        flow.revoke(b.client, WS_B, a_id)
    assert (e.value.status, e.value.code) == (404, "not_found")
    with pytest.raises(PublishError) as e:
        flow.revoke(b.client, WS_B, "0" * 32)
    assert (e.value.status, e.value.code) == (404, "not_found")  # indistinguishable from "no such share"
    with pytest.raises(PublishError) as e:
        flow.put_file(b.client, WS_B, a_id, "a-report.md", b"B was here")
    assert e.value.status == 404
    with pytest.raises(PublishError) as e:
        flow.commit(b.client, WS_B, a_id)
    assert e.value.status == 404
    h, _ = forge(b.lab, srv.api, WS_A, "DELETE", f"/v1/ws/{WS_A}/shares/{a_id}")
    assert raw(None, srv.api, "DELETE", f"/v1/ws/{WS_A}/shares/{a_id}", h) == (401, "bad_signature")

    # 4. B re-creates A's share id in its own namespace: 409, and nothing about A is said
    meta = flow.meta_bytes(ticket="DEMO-1", kind="secret", expires_ms=int(time.time() * 1000) + 3_600_000,
                           view_limit=0, token_hash_hex=flow.token_hash("x" * 43),
                           files=[{"name": "x.md", "size": 1, "sha256": "0" * 64}])  # fmt: skip
    with pytest.raises(PublishError) as e:
        flow.put_meta(b.client, WS_B, a_id, meta)
    assert e.value.status == 409 and a_id not in str(e.value) and WS_A not in str(e.value)

    # 5. B registers with A's workspace id: its own delegation names B, so the path/header A is refused;
    #    A's delegation is already bound and B cannot sign for A
    reg_b = next(t for t in tap.sent if t["target"].endswith("/register") and t["headers"]["Orch-Workspace"] == WS_B)
    reg_a = next(t for t in tap.sent if t["target"].endswith("/register") and t["headers"]["Orch-Workspace"] == WS_A)
    target = f"/v1/ws/{WS_A}/register"
    h, body = forge(b.lab, srv.api, WS_A, "POST", target, reg_b["body"])  # B's body at A's id, signed by B
    assert raw(None, srv.api, "POST", target, h, body) == (401, "bad_signature")
    h, body = forge(b.lab, srv.api, WS_A, "POST", target, reg_a["body"])  # A's own (public) body, signed by B
    assert raw(None, srv.api, "POST", target, h, body) == (401, "bad_signature")
    h, body = forge(b.lab, srv.api, WS_B, "POST", target, reg_b["body"], sign_ws=WS_B)  # B's id in the header only
    assert raw(None, srv.api, "POST", target, h, body) == (403, "wrong_workspace")

    # 6. B replays a request A really sent: refused (the nonce was spent), as is any change of its parts
    a_req = next(t for t in reversed(tap.sent) if t["headers"]["Orch-Workspace"] == WS_A and t["method"] == "GET")
    assert raw(None, srv.api, a_req["method"], a_req["target"], a_req["headers"], a_req["body"]) == (401, "replay")
    swapped = dict(a_req["headers"], **{"Orch-Workspace": WS_B})
    assert raw(None, srv.api, "GET", a_req["target"], swapped) == (401, "bad_signature")
    # A's own replay protection also holds for a re-sent PUT the server already took (idempotent, but a new nonce)
    assert [r["id"] for r in a.live()] == [a_id]

    # 7. nothing above hurt A: its link opens, B's attempts changed nothing
    assert flow.list_shares(a.client, WS_A)[0]["state"] == "live"
    assert srv.open_link(a_id, a_token)[0] == 200
    assert srv.open_link(a_id, "A" * 43)[0] == 404
    assert srv.open_link(a_id, b_token)[0] == 404  # B's token opens nothing of A's


def test_show_once_revoke_and_admin_unbind(arena):
    srv, tap, a, b = arena
    for s in (a, b):
        ok(s.world.req("publish.register", {}))
    # a show-once link opens once, then 410
    once_id, once_token = a.share("one.md", kind="once", views=1)
    status, _, body = srv.open_link(once_id, once_token)
    assert status == 200 and b"one.md" in body
    assert srv.open_link(once_id, once_token)[0] == 410
    # a secret link: revoke by the person, then it is gone
    id1, tok1 = a.share("two.md", days=3)
    assert srv.open_link(id1, tok1)[0] == 200
    ok(a.world.req("publish.revoke", {"share_id": id1}))
    assert srv.open_link(id1, tok1)[0] == 404
    assert srv.http("GET", f"/s/{id1}")[0] == 410
    # a live one for the unbind
    id2, tok2 = a.share("three.md", days=3)
    assert srv.open_link(id2, tok2)[0] == 200
    # the admin unbinds A: A is locked out, the link stops, and A's ids stay reserved
    srv.admin("ws", "unbind", WS_A)
    assert ok(a.world.req("publish.list", {"live": True}))["data"]["live"] is False  # the server no longer answers A
    with pytest.raises(PublishError) as e:
        flow.list_shares(a.client, WS_A)
    assert (e.value.status, e.value.code) == (401, "unknown_workspace")
    assert srv.open_link(id2, tok2)[0] in (404, 410)
    for taken in (once_id, id1, id2):  # every id A ever used, also the revoked one, is not B's to take
        meta = flow.meta_bytes(ticket="DEMO-1", kind="secret", expires_ms=int(time.time() * 1000) + 3_600_000,
                               view_limit=0, token_hash_hex=flow.token_hash("y" * 43),
                               files=[{"name": "x.md", "size": 1, "sha256": "0" * 64}])  # fmt: skip
        with pytest.raises(PublishError) as e:
            flow.put_meta(b.client, WS_B, taken, meta)
        assert e.value.status == 409
    # A cannot bind itself again (the pre-binding went with the unbind) ...
    res = a.world.req("publish.register", {})
    assert err(res) == "publish.server_refused"
    # ... and B is unaffected
    b_id, b_tok = b.share("b.md", days=3)
    assert srv.open_link(b_id, b_tok)[0] == 200
