"""Hub one-app: switch tickets (hub side), the three link ops and the web routes (TECH.md, 27 Oct)."""

from __future__ import annotations

import pytest
from webapi_helpers import ORIGIN, WS, WS_SEL, Web

from orch_hub.contract.hub import Refused
from orch_hub.link import ops
from orch_hub.switch import SwitchError, SwitchService, clean_return_to

DASH = "http://127.0.0.1:7001"


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    x.t = [1_000_000]
    x.sw = SwitchService(hub_origin=lambda: ORIGIN, hub_name="Acme", now_ms=lambda: x.t[0])
    x.api.d.switch = x.sw
    x.reg.add(WS_SEL, "selective", owner=x.ann)
    x.reg.ws[WS]["owner"] = x.ann
    yield x
    x.close()


def ctx(w, ws, op, body, user=None):
    return ops.OpContext(
        hub_id="h",
        workspace_id=ws,
        link_wsk_pub=b"",
        body=body,
        registry=w.reg,
        invites=None,
        snapshots=None,
        publish=None,
        downstream=None,
        now_ms=0,
        dev_mode=True,
        access=w.env.acc,
        switch=w.sw,
    )


def test_return_to_is_a_plain_relative_path():
    assert clean_return_to("/t/DEMO-43?x=1") == "/"  # a query is not part of a path
    assert clean_return_to("/t/DEMO-43") == "/t/DEMO-43"
    for bad in ("//evil.com", "https://evil.com", "/a\\b", "/a/../b", "x", "", None, "/" + "a" * 300):
        assert clean_return_to(bad) == "/"


def test_ticket_single_use_and_expiry(w):
    t = w.sw.issue_to_hub(w.ann, WS, "/x")
    assert w.sw.redeem(t, "to_hub")["rt"] == "/x"
    with pytest.raises(SwitchError):
        w.sw.redeem(t, "to_hub")
    t = w.sw.issue_to_hub(w.ann, WS, "/x")
    w.t[0] += 30_001
    with pytest.raises(SwitchError):
        w.sw.redeem(t, "to_hub")


def test_ticket_direction_and_workspace_are_bound(w):
    t = w.sw.issue_to_ws(w.ann, WS, "/x")
    with pytest.raises(SwitchError):
        w.sw.redeem(t, "to_ws", ws=WS_SEL)  # another workspace's link
    t = w.sw.issue_to_ws(w.ann, WS, "/x")
    with pytest.raises(SwitchError):
        w.sw.redeem(t.split(".")[1], "to_hub")  # a to_ws ticket is not a to_hub ticket
    t = w.sw.issue_to_ws(w.ann, WS, "/x")
    assert w.sw.redeem(t, "to_ws", ws=WS)["user"] == w.ann


def test_link_ops_to_hub_and_rail(w):
    r = ops.switch_ticket(ctx(w, WS, "switch_ticket", {"origin": DASH + "/", "return_to": "/t/1", "kind": "open"}))
    assert r["url"] == f"{ORIGIN}/#t={r['ticket']}"
    assert w.sw.origin_of(WS) == DASH
    rail = ops.rail_get(ctx(w, WS, "rail_get", {"origin": DASH}))
    assert rail["hub"] == {"name": "Acme", "origin": ORIGIN}
    cur = [x for x in rail["workspaces"] if x["current"]]
    assert [x["workspace_id"] for x in cur] == [WS] and cur[0]["origin"] == DASH
    with pytest.raises(Refused):
        ops.rail_get(ctx(w, WS, "rail_get", {"origin": "javascript:alert(1)"}))


def test_link_op_without_owner_alias_is_refused(w):
    w.reg.ws[WS]["owner"] = None
    with pytest.raises(Refused) as e:
        ops.switch_ticket(ctx(w, WS, "switch_ticket", {"origin": DASH, "return_to": "/", "kind": "open"}))
    assert e.value.code == "no_owner_alias"


def test_hub_to_workspace_redeem_over_the_link(w):
    w.sw.set_origin(WS, DASH)
    token = w.login(w.ann)
    s, o, _ = w.call("POST", "/switch/ticket", {"workspace_id": WS, "return_to": "/t/9", "kind": "open"}, token=token)
    assert s == 200 and o["url"].startswith(DASH + "/#t=" + WS + ".")
    t = o["url"].split("#t=")[1]
    # another workspace's link cannot redeem it, and that spends it
    with pytest.raises(Refused):
        ops.switch_redeem(ctx(w, WS_SEL, "switch_redeem", {"ticket": t}))
    with pytest.raises(Refused):
        ops.switch_redeem(ctx(w, WS, "switch_redeem", {"ticket": t}))
    s, o, _ = w.call("POST", "/switch/ticket", {"workspace_id": WS, "return_to": "/t/9", "kind": "open"}, token=token)
    t = o["url"].split("#t=")[1]
    r = ops.switch_redeem(ctx(w, WS, "switch_redeem", {"ticket": t}))
    assert (r["user_id"], r["return_to"], r["kind"]) == (w.ann, "/t/9", "open")
    with pytest.raises(Refused):
        ops.switch_redeem(ctx(w, WS, "switch_redeem", {"ticket": t}))


def test_redeem_needs_the_owner_alias(w):
    w.sw.set_origin(WS, DASH)
    t = w.sw.issue_to_ws(w.admin, WS, "/")  # a hub user who is not the workspace's owner alias
    with pytest.raises(Refused):
        ops.switch_redeem(ctx(w, WS, "switch_redeem", {"ticket": t}))


def test_web_ticket_refusals(w):
    token = w.login(w.ann)
    body = {"workspace_id": WS, "return_to": "/", "kind": "open"}
    s, o, _ = w.call("POST", "/switch/ticket", body, token=token)
    assert (s, o["error"]) == (503, "unavailable")  # no dashboard origin known yet
    w.sw.set_origin(WS, DASH)
    other = w.login(w.env.user("bob"))  # no group, no grant: no part in WS
    s, o, _ = w.call("POST", "/switch/ticket", body, token=other)
    assert (s, o["error"]) == (403, "forbidden_role")
    s, o, _ = w.call("POST", "/switch/ticket", body)
    assert s == 401


def test_workspace_to_hub_redeem_sets_cookie_once(w):
    r = ops.switch_ticket(ctx(w, WS, "switch_ticket", {"origin": DASH, "return_to": "/home", "kind": "open"}))
    t = r["ticket"]
    s, o, h = w.call("POST", "/switch/redeem", {"t": t})
    assert s == 200 and o == {"user_id": w.ann, "return_to": "/home", "kind": "open"}
    cookie = h["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    tok = cookie.split(";")[0].split("=", 1)[1]
    w.browser_tokens.add(tok)
    s, me, _ = w.call("GET", "/session", token=tok)
    assert s == 200 and me["user_id"] == w.ann
    s, o, _ = w.call("POST", "/switch/redeem", {"t": t})
    assert (s, o["error"]) == (403, "bad_ticket")  # one use
    s, o, _ = w.call("POST", "/switch/redeem", {"t": t}, origin=None)
    assert s == 403  # a browser label is required


def test_redeem_expired_ticket_is_refused(w):
    t = ops.switch_ticket(ctx(w, WS, "switch_ticket", {"origin": DASH, "return_to": "/", "kind": "open"}))["ticket"]
    w.t[0] += 31_000
    s, o, _ = w.call("POST", "/switch/redeem", {"t": t})
    assert (s, o["error"]) == (403, "bad_ticket")


def test_signout_kind_ends_the_presented_session(w):
    tok = w.login(w.ann)
    t = ops.switch_ticket(ctx(w, WS, "switch_ticket", {"origin": DASH, "return_to": "/", "kind": "signout"}))["ticket"]
    s, o, h = w.call("POST", "/switch/redeem", {"t": t}, token=tok)
    assert s == 200 and o["kind"] == "signout" and "Max-Age=0" in h["Set-Cookie"]
    s, _, _ = w.call("GET", "/session", token=tok)
    assert s == 401


def test_rail_lists_my_workspaces_with_origins_and_badges(w):
    w.sw.set_origin(WS, DASH)
    w.sw.badge = lambda ws, user: 3
    token = w.login(w.ann)
    s, o, _ = w.call("GET", "/rail", token=token)
    assert s == 200 and o["hub"] == {"name": "Acme", "origin": ORIGIN}
    row = next(x for x in o["workspaces"] if x["workspace_id"] == WS)
    assert (row["origin"], row["badge"], row["online"]) == (DASH, 3, True)
    assert all(x["origin"] is None for x in o["workspaces"] if x["workspace_id"] != WS)


def test_no_switch_service_means_unavailable(tmp_path):
    x = Web(tmp_path)
    try:
        s, o, _ = x.call("GET", "/rail", token=x.login(x.ann))
        assert (s, o["error"]) == (503, "unavailable")
    finally:
        x.close()


def _tok(h):
    tok = h["Set-Cookie"].split(";")[0].split("=", 1)[1]
    return tok


def test_peek_does_not_use_the_ticket_and_names_both_people(w):
    t = w.sw.issue_to_hub(w.ann, WS, "/x")
    mine = w.login(w.admin)
    s, o, _ = w.call("POST", "/switch/peek", {"t": t}, token=mine)
    assert s == 200 and o["user_id"] == w.ann and o["kind"] == "open"
    assert o["signed_in_as"]["user_id"] == w.admin
    s, o, _ = w.call("POST", "/switch/peek", {"t": t})
    assert s == 200 and o["signed_in_as"] is None
    s, o, _ = w.call("POST", "/switch/redeem", {"t": t})
    assert s == 200  # still unused after two peeks


def test_a_ticket_never_replaces_another_persons_session_silently(w):
    mine = w.login(w.admin)
    t = w.sw.issue_to_hub(w.ann, WS, "/x")
    s, o, _ = w.call("POST", "/switch/redeem", {"t": t}, token=mine)
    assert (s, o["error"]) == (403, "other_user")
    assert w.call("GET", "/session", token=mine)[0] == 200  # still signed in as the admin; the ticket is not spent
    s, o, h = w.call("POST", "/switch/redeem", {"t": t, "replace": True}, token=mine)
    assert s == 200 and o["user_id"] == w.ann
    assert w.call("GET", "/session", token=mine)[0] == 401  # the old session ended
    w.browser_tokens.add(_tok(h))


def test_the_same_person_may_pass_through(w):
    mine = w.login(w.ann)
    t = w.sw.issue_to_hub(w.ann, WS, "/x")
    assert w.call("POST", "/switch/redeem", {"t": t}, token=mine)[0] == 200


def test_a_switch_session_cannot_do_admin_until_a_real_sign_in(w):
    t = w.sw.issue_to_hub(w.admin, WS, "/")
    s, o, h = w.call("POST", "/switch/redeem", {"t": t})
    assert s == 200
    tok = _tok(h)
    w.browser_tokens.add(tok)
    s, me, _ = w.call("GET", "/session", token=tok)
    assert s == 200 and me["needs_sign_in"] is True and me["is_admin"] is True
    s, o, _ = w.call("GET", "/admin/users", token=tok)
    assert (s, o["error"]) == (403, "reauth_required")
    real = w.login(w.admin)  # a normal sign-in is unaffected
    assert w.call("GET", "/admin/users", token=real)[0] == 200
    assert w.call("GET", "/session", token=real)[2] is not None


def test_the_rail_lists_only_my_workspaces(w):
    w.sw.set_origin(WS, DASH)
    w.sw.set_origin(WS_SEL, "http://127.0.0.1:7002")
    bob = w.login(w.env.user("bob"))  # in no group, owns nothing
    s, o, _ = w.call("GET", "/rail", token=bob)
    assert s == 200 and o["workspaces"] == []
    r = ops.rail_get(ctx(w, WS, "rail_get", {"origin": DASH}))  # the link's user is the owner alias of WS (ann)
    assert {x["workspace_id"] for x in r["workspaces"]} <= {WS, WS_SEL}
    assert all(x["workspace_id"] != WS_SEL or w.reg.owner_alias(WS_SEL) == w.ann for x in r["workspaces"])


def test_a_switch_session_reaches_only_the_named_routes_everything_else_is_closed(w):
    from orch_hub.webapi.api import ROUTES, SWITCH_HANDLERS

    t = w.sw.issue_to_hub(w.admin, WS, "/")
    s, o, h = w.call("POST", "/switch/redeem", {"t": t})
    tok = _tok(h)
    w.browser_tokens.add(tok)
    ws = "a" * 32
    seen = 0
    for rt in ROUTES:
        if rt.auth == "none":
            continue
        path = rt.path[len("/api/v1") :]
        for name in ("workspace_id", "item_id", "user_id", "group", "channel_id", "prepared_id"):
            path = path.replace("{" + name + "}", ws)
        s, o, _ = w.call(rt.method, path, {}, token=tok)
        seen += 1
        reauth = s == 403 and o.get("error") == "reauth_required"
        if rt.handler in SWITCH_HANDLERS:
            assert not reauth, (rt.method, rt.path)
        else:  # closed: asked to sign in for real, or not even reachable with a browser cookie
            assert reauth or (s in (401, 403) and o.get("error") in ("no_session", "forbidden_role")), (rt.path, s, o)
    assert seen > 20
