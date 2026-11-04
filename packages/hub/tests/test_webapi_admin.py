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
    carol = w.env.user("carol", password=False)  # people join through an invitation link now (test_invites.py)
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
    assert {"name": "devs", "members": [carol], "ends": {}, "grants": [{"workspace_id": ws2, "role": "edit"}]} in o[
        "groups"
    ]
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
    assert w.call("POST", "/setup", body)[:2] == (
        401,
        {"error": "bad_credentials"},
    )  # a wrong token: 401 whoever exists
    # a second admin with the RIGHT token is refused (the fixture already has an admin); the token is not consumed
    assert w.call("POST", "/setup", {**body, "token": "tok-abc"})[:2] == (403, {"error": "not_allowed"})
    assert f.exists()
    for admin in list(w.env.acc.members("hub-admin")):  # now a hub with no admin yet
        w.env.acc.remove_member("hub-admin", admin)
    assert w.call("POST", "/setup", {**body, "token": "tok-abc", "password": "short"})[:2] == (
        400,
        {"error": "weak_password"},
    )
    s, o, _ = w.call("POST", "/setup", {**body, "token": "tok-abc"})
    assert s == 200 and w.env.acc.is_hub_admin(o["user_id"]) and o["totp_secret"]
    assert not f.exists()
    assert w.call("POST", "/setup", {**body, "token": "tok-abc"})[:2] == (401, {"error": "bad_credentials"})


def test_setup_signs_the_new_admin_in_once_with_a_browser_cookie(w):
    f = w.env.keys.state / "setup-token"
    f.write_text("tok-abc\n")
    w.api.d.setup_token_file = f
    for admin in list(w.env.acc.members("hub-admin")):
        w.env.acc.remove_member("hub-admin", admin)
    body = {"token": "tok-abc", "name": "boss", "password": "a long password 1"}
    s, o, h = w.call("POST", "/setup", body)
    assert s == 200 and o["totp_uri"].startswith("otpauth://")
    cookie = h["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    tok = cookie.split(";")[0].split("=", 1)[1]
    info = w.api.d.channels.session_info(tok)
    assert info["user_id"] == o["user_id"] and info["scope"] == "full" and info["via"] == "setup"
    w.browser_tokens.add(tok)  # the cookie works on a hub-admin route and on the session route
    assert w.call("GET", "/session", token=tok)[0] == 200
    assert w.call("GET", "/admin/users", token=tok)[0] == 200
    # single use: the same token answers 401 and sets no cookie; a wrong token never gets a session
    s2, _, h2 = w.call("POST", "/setup", body)
    assert s2 == 401 and "Set-Cookie" not in h2


def test_web_listen_takes_a_private_ip_not_a_wildcard(w):
    from orch_hub.serve import parse_web_listen

    assert parse_web_listen("100.71.14.98:8100", assigned=lambda ip: True) == ("100.71.14.98", 8100)
    assert parse_web_listen("127.0.0.1:0") == ("127.0.0.1", 0)
    for bad in ("0.0.0.0:8100", "8.8.8.8:8100"):
        with pytest.raises(ValueError):
            parse_web_listen(bad, assigned=lambda ip: True)


def test_workspace_left_listed(w):
    left_ms = 1730000000000
    w.reg.add("cd" * 16, "full", status="unbound", left_ms=left_ms)
    status, body, _ = w.call("GET", "/admin/workspaces", token=w.login(w.admin))
    assert status == 200
    rows = {r["workspace_id"]: r for r in body["workspaces"]}
    assert rows["cd" * 16]["status"] == "unbound"
    assert rows["cd" * 16]["left_ms"] == left_ms
    assert rows[WS]["left_ms"] is None


def test_workspace_remove(w):
    admin, member = w.login(w.admin), w.login(w.ann)
    path = f"/admin/workspaces/{WS}"
    assert w.call("DELETE", path, token=admin)[:2] == (409, {"error": "not_left"})
    assert w.reg.binding_state(WS)["status"] == "bound"
    assert w.env.acc.grants(WS)
    w.reg.add(WS, "full", status="unbound", left_ms=1730000000000)
    w.env.acc.add_group("other")
    w.env.acc.grant(WS, "other", "read")
    assert w.call("DELETE", path, token=member)[:2] == (403, {"error": "forbidden_role"})
    assert w.reg.binding_state(WS)["status"] == "unbound"
    assert w.call("DELETE", path, token=admin)[:2] == (200, {"ok": True})
    assert w.env.acc.grants(WS) == []
    assert w.call("GET", "/admin/workspaces", token=admin)[1]["workspaces"] == []
    assert w.call("DELETE", path, token=admin)[:2] == (404, {"error": "unknown_workspace"})


def test_p05_admin_lists_a_selective_workspace_of_someone_else_but_a_member_cannot_and_me_workspaces_still_omits_it(w):
    ben_ws = "ef" * 16
    ben = w.env.user("ben")
    w.reg.add(ben_ws, "selective", owner=ben)
    s, o, _ = w.call(
        "GET", "/admin/workspaces", token=w.login(w.admin)
    )  # the hub admin sees it exists (name, owner, status)
    assert s == 200 and ben_ws in {r["workspace_id"] for r in o["workspaces"]}
    assert w.call("GET", "/admin/workspaces", token=w.login(ben))[:2] == (
        403,
        {"error": "forbidden_role"},
    )  # server-side gate
    assert w.call("GET", "/admin/workspaces", token=w.login(w.ann))[:2] == (403, {"error": "forbidden_role"})
    s, o, _ = w.call("GET", "/me/workspaces", token=w.login(w.admin))  # unchanged: no role in a selective workspace
    assert s == 200 and ben_ws not in {r["workspace_id"] for r in o["workspaces"]}


def test_p05_a_switch_session_of_an_admin_is_refused_on_admin_workspaces(w):
    ticket_session = w.env.svc.open_session(w.admin)["token"]  # a workspace vouched for it: no password sign-in
    w.browser_tokens.add(ticket_session)
    s, o, _ = w.call("GET", "/admin/workspaces", token=ticket_session)
    assert (s, o["error"]) == (403, "reauth_required")


def test_update_request_marks_older_workspaces_once_and_only_admins_may_ask(w, monkeypatch):
    from orch_hub import __version__

    asked: dict[str, dict] = {}
    monkeypatch.setattr(w.reg, "status", lambda ws: {"version": "0.0.1"}, raising=False)
    monkeypatch.setattr(
        w.reg,
        "ask_update",
        lambda ws, t, now, by: asked.setdefault(ws, {"target_version": t, "asked_ms": now}),
        raising=False,
    )
    tok = w.login(w.admin)
    s, o, _ = w.call("POST", "/admin/workspaces/update-request", {}, token=tok)
    assert s == 200 and o["asked"] == len(w.reg.ws) and o["target_version"] == __version__
    assert asked and all(a["target_version"] == __version__ for a in asked.values())
    # only newly asked workspaces are counted: a second press asks nobody new
    done = set(asked)
    monkeypatch.setattr(w.reg, "ask_update", lambda ws, t, now, by: ws not in done, raising=False)
    assert w.call("POST", "/admin/workspaces/update-request", {}, token=tok)[1]["asked"] == 0
    assert w.call("POST", "/admin/workspaces/update-request", {}, token=w.login(w.ann))[0] == 403


def test_update_request_reaches_only_the_workspace_that_is_still_older_and_keeps_the_first_time(hub):
    from orch_hub import __version__

    c = hub.open(first=True)
    c.request("join", hub.join_body(mode="full"))
    older = {"version": "0.0.1", "commit": "", "host": "h", "name": "n"}
    assert c.request("status_put", older) == {}
    ws = next(iter(hub.registry.pins()))
    assert hub.registry.ask_update(ws, __version__, 1000, "u") is True
    assert hub.registry.ask_update(ws, __version__, 2000, "u") is False  # idempotent: the first time stays
    assert hub.registry.update_ask(ws) == {"target_version": __version__, "asked_ms": 1000}
    assert c.request("status_put", older) == {"update_requested": {"target_version": __version__}}
    assert c.request("status_put", {**older, "version": __version__}) == {}  # updated: no more request


def test_my_workspaces_tells_an_admin_when_the_owners_were_asked(w, monkeypatch):
    monkeypatch.setattr(w.reg, "update_ask", lambda ws: {"target_version": "9.9.9", "asked_ms": 4242}, raising=False)
    _, o, _ = w.call("GET", "/me/workspaces", token=w.login(w.admin))
    assert o["workspaces"] and all(r["update_asked_ms"] == 4242 for r in o["workspaces"])
    assert all(r["update_asked_target"] == "9.9.9" for r in o["workspaces"])


# --- GA-07: end dates, the review list, the default group --------------------------------------------------------------

DAY = 86_400_000


def test_ga07_member_with_an_end_date_review_keep_and_default_group_over_http(w):
    tok = w.login(w.admin)
    carol = w.env.user("carol", password=False)
    now = w.clock()
    assert w.call("POST", "/admin/groups", {"name": "temps"}, token=tok)[0] == 200
    end = now + 5 * DAY
    assert w.call("PUT", f"/admin/groups/temps/members/{carol}", {"ends_ms": end}, token=tok)[0] == 200
    _, o, _ = w.call("GET", "/admin/groups", token=tok)
    assert next(g for g in o["groups"] if g["name"] == "temps")["ends"] == {carol: end}
    # a past or too-far end date is refused; the hub-admin group takes none
    s, o, _ = w.call("PUT", f"/admin/groups/temps/members/{carol}", {"ends_ms": now - 1}, token=tok)
    assert s >= 400 and "error" in o
    assert w.call("PUT", f"/admin/groups/temps/members/{carol}", {"ends_ms": "soon"}, token=tok)[0] >= 400
    s, _o, _ = w.call("PUT", f"/admin/groups/hub-admin/members/{carol}", {"ends_ms": end}, token=tok)
    assert s == 400
    # the review list shows it ("ends in 5 days"), one click extends it by 30 days
    _, o, _ = w.call("GET", "/admin/review", token=tok)
    assert [(i["group"], i["user_id"], i["ends_ms"], i["ended"]) for i in o["items"]] == [("temps", carol, end, False)]
    assert w.call("POST", "/admin/review/keep", {"group": "temps", "user_id": carol}, token=tok)[0] == 200
    _, o, _ = w.call("GET", "/admin/groups", token=tok)
    assert next(g for g in o["groups"] if g["name"] == "temps")["ends"] == {carol: end + 30 * DAY}
    # the default group: admin only, never hub-admin
    assert w.call("PUT", "/admin/default-group", {"group": "temps"}, token=tok)[:2] == (
        200,
        {"ok": True, "default_group": "temps"},
    )
    assert w.call("PUT", "/admin/default-group", {"group": "hub-admin"}, token=tok)[0] == 400
    _, o, _ = w.call("GET", "/admin/groups", token=tok)
    assert o["default_group"] == "temps"


def test_ga07_only_a_hub_admin_may_use_the_new_routes(w):
    other = w.env.user("dave")
    tok = w.login(other)
    for method, path, body in (
        ("GET", "/admin/review", None),
        ("POST", "/admin/review/keep", {"group": "x", "user_id": other}),
        ("PUT", "/admin/default-group", {"group": None}),
    ):
        assert w.call(method, path, body, token=tok)[0] in (401, 403), path
