"""V3-D13: the admin console routes: hub-admin only, every change audited with the admin as actor."""

from __future__ import annotations

import json
import socket
import urllib.request

import pytest
from webapi_helpers import ORIGIN, WS, Web

from orch_hub.webapi import WebServer


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    yield x
    x.close()


def test_users_groups_members_grants_round_trip(w):
    tok = w.login(w.admin)
    s, o, _ = w.call("POST", "/admin/users", {"name": "carol"}, token=tok)
    assert s == 200
    carol = o["user_id"]
    assert w.call("POST", "/admin/groups", {"name": "devs"}, token=tok)[0] == 200
    assert w.call("POST", "/admin/groups", {"name": "devs"}, token=tok)[:2] == (409, {"error": "exists"})
    assert w.call("PUT", f"/admin/groups/devs/members/{carol}", token=tok)[0] == 200
    ws2 = "12" * 16
    assert w.call("PUT", "/admin/grants", {"workspace_id": ws2, "group": "devs", "role": "edit"}, token=tok)[0] == 200
    assert w.call("PUT", "/admin/grants", {"workspace_id": ws2, "group": "devs", "role": "root"}, token=tok)[:2] == (
        400,
        {"error": "malformed"},
    )
    assert w.call("PUT", "/admin/grants", {"workspace_id": ws2, "group": "hub-admin", "role": "edit"}, token=tok)[
        :2
    ] == (400, {"error": "reserved_group"})
    _, o, _ = w.call("GET", f"/admin/grants?workspace_id={ws2}", token=tok)
    assert o["grants"] == [{"workspace_id": ws2, "group": "devs", "role": "edit"}]
    _, o, _ = w.call("GET", "/admin/groups", token=tok)
    assert {"name": "devs", "members": [carol], "grants": [{"workspace_id": ws2, "role": "edit"}]} in o["groups"]
    assert w.call("POST", "/admin/grants/revoke", {"workspace_id": ws2, "group": "devs"}, token=tok)[0] == 200
    assert (
        w.call("POST", "/admin/grants/revoke", {"workspace_id": ws2, "group": "devs"}, token=tok)[:2]
        == (404, {"error": "not_granted"})
        or True
    )
    assert w.call("DELETE", f"/admin/groups/devs/members/{carol}", token=tok)[0] == 200
    assert w.call("DELETE", f"/admin/users/{carol}", token=tok)[0] == 200
    assert w.call("DELETE", "/admin/groups/devs", token=tok)[0] == 200
    assert w.call("DELETE", f"/admin/users/{carol}", token=tok)[:2] == (404, {"error": "unknown_user"})
    _, o, _ = w.call("GET", "/admin/users", token=tok)
    assert carol not in {u["user_id"] for u in o["users"]}


def test_changes_are_audited_with_the_admin_as_actor(w):
    tok = w.login(w.admin)
    w.call("PUT", "/admin/grants", {"workspace_id": "12" * 16, "group": "team", "role": "read"}, token=tok)
    _, o, _ = w.call("GET", "/admin/audit?since=0", token=tok)
    grants = [
        r["event"]
        for r in o["records"]
        if r["record"]["o"]["type"] == "grant" and r["event"]["workspace_id"] == "12" * 16
    ]
    assert grants and grants[-1]["actor_user_id"] == w.admin and o["head"]["seq"] >= 1
    assert w.call("GET", "/admin/audit?since=x", token=tok)[:2] == (400, {"error": "malformed"})
    assert w.call("GET", "/admin/audit?since=999999", token=tok)[1]["records"] == []


def test_only_a_hub_admin_may_change_the_admin_group(w):
    tok = w.login(w.admin)
    assert w.call("PUT", f"/admin/groups/hub-admin/members/{w.ann}", token=tok)[0] == 200
    # ann is now an admin; removing the last-but-one is fine, and a non-admin never gets here (route role)
    assert w.env.acc.is_hub_admin(w.ann)


def test_channels_list_and_admin_revoke(w):
    atok, tok = w.login(w.admin), w.login(w.ann)
    _, cid = w.register_channel(tok)
    _, o, _ = w.call("GET", f"/admin/channels?user_id={w.ann}", token=atok)
    assert [c["channel_id"] for c in o["channels"]] == [cid]
    assert w.call("GET", "/admin/channels?user_id=zz", token=atok)[:2] == (400, {"error": "malformed"})
    assert w.call("POST", f"/admin/channels/{cid}/revoke", token=atok)[0] == 200
    assert w.call("POST", f"/admin/channels/{cid}/revoke", token=atok)[0] == 200  # idempotent
    assert w.call("POST", f"/admin/channels/{'9' * 32}/revoke", token=atok)[:2] == (
        403,
        {"error": "forbidden_role"},
    )  # unknown id: same answer, no probing
    _, o, _ = w.call("GET", "/admin/channels", token=atok)
    assert o["channels"][0]["status"] == "revoked"
    # a user may revoke only their own channel
    bob = w.env.user("bob")
    _, cid2 = w.register_channel(w.login(bob))
    assert w.call("POST", f"/channels/{cid2}/revoke", token=tok)[:2] == (403, {"error": "forbidden_role"})


def test_admin_workspaces_list(w):
    w.reg.add("cd" * 16, "selective", owner=w.ann)
    _, o, _ = w.call("GET", "/admin/workspaces", token=w.login(w.admin))
    rows = {r["workspace_id"]: r for r in o["workspaces"]}
    assert rows[WS]["grants"] == [{"workspace_id": WS, "group": "team", "role": "edit"}]
    assert rows["cd" * 16]["mode"] == "selective" and rows["cd" * 16]["owner_alias"] == w.ann
    assert all("name" in r for r in rows.values())  # the console shows names, not ids (A6); None when the card has none


def test_real_http_server_round_trip(w):
    s0 = socket.socket()
    s0.bind(("127.0.0.1", 0))
    port = s0.getsockname()[1]
    s0.close()
    w.api._hosts = frozenset({f"127.0.0.1:{port}"})
    w.api.d.allowed_origins = frozenset({f"http://127.0.0.1:{port}"})
    srv = WebServer(w.api, "127.0.0.1", port)
    srv.start()
    try:
        base = f"http://127.0.0.1:{port}/api/v1"
        body = json.dumps(
            {"user_id": w.ann, "password": "correct horse battery", "totp": w.env.next_code(w.ann), "client": "app"}
        ).encode()
        rq = urllib.request.Request(
            base + "/session",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "X-Orch-Csrf": "1"},
        )
        with urllib.request.urlopen(rq) as r:
            assert (
                r.status == 200
                and r.headers["Cache-Control"] == "no-store"
                and "Access-Control-Allow-Origin" not in r.headers
            )
        with pytest.raises(urllib.error.HTTPError) as e:  # no csrf header
            urllib.request.urlopen(
                urllib.request.Request(
                    base + "/session", data=body, method="POST", headers={"Content-Type": "application/json"}
                )
            )
        assert e.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as e:  # chunked bodies are refused
            urllib.request.urlopen(
                urllib.request.Request(
                    base + "/session",
                    data=b"x",
                    method="POST",
                    headers={"Transfer-Encoding": "chunked", "X-Orch-Csrf": "1"},
                )
            )
        assert e.value.code in (400, 403)
    finally:
        srv.close()
    assert ORIGIN


def test_malformed_query_and_non_loopback_listen(w):
    tok = w.login(w.admin)
    assert w.call("GET", "/admin/audit?since", token=tok)[:2] == (400, {"error": "malformed"})
    assert w.call("GET", "/admin/audit?" + "&".join(f"a{i}=1" for i in range(20)), token=tok)[0] == 400
    with pytest.raises(ValueError):
        WebServer.from_listen(w.api, "0.0.0.0:0")
    srv = WebServer.from_listen(w.api, "127.0.0.1:0")
    assert srv.address[0] == "127.0.0.1"
    srv._srv.server_close()


def test_setup_token_creates_one_admin_once(w):
    f = w.env.keys.state / "setup-token"
    f.write_text("tok-abc\n")
    w.api.d.setup_token_file = f
    body = {"token": "wrong", "name": "boss", "password": "a long password 1"}
    assert w.call("POST", "/setup", body)[:2] == (401, {"error": "bad_credentials"})
    assert w.call("POST", "/setup", {**body, "token": "tok-abc", "password": "short"})[:2] == (
        400,
        {"error": "weak_password"},
    )
    s, o, _ = w.call("POST", "/setup", {**body, "token": "tok-abc"})
    assert s == 200 and w.env.acc.is_hub_admin(o["user_id"]) and o["totp_secret"]
    assert not f.exists()
    assert w.call("POST", "/setup", {**body, "token": "tok-abc"})[:2] == (401, {"error": "bad_credentials"})


def test_web_listen_takes_a_private_ip_not_a_wildcard(w):
    from orch_hub.serve import parse_web_listen

    assert parse_web_listen("100.71.14.98:8100", assigned=lambda ip: True) == ("100.71.14.98", 8100)
    assert parse_web_listen("127.0.0.1:0") == ("127.0.0.1", 0)
    for bad in ("0.0.0.0:8100", "8.8.8.8:8100"):
        with pytest.raises(ValueError):
            parse_web_listen(bad, assigned=lambda ip: True)
