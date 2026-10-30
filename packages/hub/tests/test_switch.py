"""Hub one-app: switch tickets (hub side), the three link ops and the web routes (TECH.md, 27 Oct)."""

from __future__ import annotations

import json

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
    assert rail["hub"] == {"name": "Acme", "origin": ORIGIN, "network_origin": None}
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


def test_switch_redeems_only_for_the_owner_alias_and_a_refusal_does_not_use_the_ticket(w):
    """Contract (E4): `hub.switch` redeem answers only a ticket made for the workspace's OWNER alias. The dashboard's
    identity rule relies on it (the ticket's user can only be the owner). Anyone else's ticket is refused, unused."""
    assert w.reg.owner_alias(WS) == w.ann
    for other in (w.admin, w.env.user("bob")):
        t = w.sw.issue_to_ws(other, WS, "/x")
        for _ in range(2):  # refused every time: the refusal itself used nothing
            with pytest.raises(Refused) as e:
                ops.switch_redeem(ctx(w, WS, "switch_redeem", {"ticket": t}))
            assert e.value.code == "bad_ticket"
        assert w.sw.peek(t, "to_ws")["user"] == other  # still live, still bound to the same person and workspace
    w.reg.ws[WS]["owner"] = None  # no owner alias at all: nobody's ticket redeems
    t = w.sw.issue_to_ws(w.ann, WS, "/x")
    with pytest.raises(Refused):
        ops.switch_redeem(ctx(w, WS, "switch_redeem", {"ticket": t}))
    assert w.sw.peek(t, "to_ws")["user"] == w.ann
    w.reg.ws[WS]["owner"] = w.ann
    assert ops.switch_redeem(ctx(w, WS, "switch_redeem", {"ticket": t}))["user_id"] == w.ann  # the owner's: once
    with pytest.raises(Refused):
        ops.switch_redeem(ctx(w, WS, "switch_redeem", {"ticket": t}))


def test_a_ticket_presented_at_another_workspace_is_still_spent(w):
    t = w.sw.issue_to_ws(w.admin, WS, "/x")  # not the owner of WS_SEL either
    with pytest.raises(Refused):
        ops.switch_redeem(ctx(w, WS_SEL, "switch_redeem", {"ticket": t}))
    with pytest.raises(SwitchError):
        w.sw.peek(t, "to_ws")  # the wrong place spends it, whoever it was for


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


def _mint(w, token, method="POST", **headers):
    h = {"host": "127.0.0.1:9000", "cookie": f"{w.api._cookie_name}={token}", "x-orch-csrf": "1", **headers}
    body = json.dumps({"workspace_id": WS, "return_to": "/", "kind": "open"}).encode()
    h.setdefault("content-type", "application/json")
    status, _, out = w.api.handle(method, "/api/v1/switch/ticket", {k: v for k, v in h.items() if v}, body)
    return status, json.loads(out)


def test_a_ticket_is_minted_only_on_a_same_origin_authenticated_post(w):
    """E4: a cross-site page cannot make the hub mint a switch ticket (and so cannot land the person silently)."""
    w.sw.set_origin(WS, DASH)
    token = w.login(w.ann)
    s, o = _mint(w, token, origin=ORIGIN, **{"sec-fetch-site": "same-origin"})
    assert s == 200 and o["url"].startswith(DASH + "/#t=")
    s, o = _mint(w, token, origin=ORIGIN)  # an older browser without Fetch Metadata, same origin
    assert s == 200
    for refused in (
        {"origin": "https://evil.test", "sec-fetch-site": "cross-site"},  # a cross-site page
        {"origin": ORIGIN, "sec-fetch-site": "cross-site"},  # a forged Origin cannot hide the browser's own label
        {"origin": ORIGIN, "sec-fetch-site": "same-site"},  # a sibling subdomain is not the console
        {"origin": ORIGIN, "sec-fetch-site": "none"},  # typed or bookmarked: no page asked for it
        {"origin": None},  # no Origin at all with the ambient cookie
        {"origin": ORIGIN, "x-orch-csrf": None},  # a simple (form) POST
    ):
        s, o = _mint(w, token, **refused)
        assert (s, o["error"]) == (403, "not_allowed"), refused
    # the same session as a bearer token (a script, not the console page), even with every browser label right
    for site in ("same-origin", "cross-site", None):
        s, o = _mint(w, "", origin=ORIGIN, cookie=None, authorization=f"Bearer {token}", **{"sec-fetch-site": site})
        assert (s, o["error"]) == (403, "not_allowed"), site
    s, o, _ = w.call(
        "POST", "/switch/ticket", {"workspace_id": WS, "return_to": "/", "kind": "open"}, token=token, cookie=False
    )
    assert (s, o["error"]) == (403, "not_allowed")
    app = w.login(w.ann, client="app")  # the app's own bearer (pairing): refused by scope, as on every full-only route
    s, o, _ = w.call("POST", "/switch/ticket", {"workspace_id": WS, "return_to": "/", "kind": "open"}, token=app)
    assert (s, o["error"]) == (403, "forbidden_role")
    assert len(w.sw._tickets) == 2  # only the two allowed mints above made a ticket
    s, o = _mint(w, token, method="GET", origin=ORIGIN, **{"sec-fetch-site": "same-origin"})
    assert s == 404 and o["error"] == "unknown_op"  # there is no GET mint
    s, o = _mint(w, "", origin=ORIGIN, **{"sec-fetch-site": "same-origin"})
    assert s == 401  # unauthenticated


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


def test_run15_j02_a_selective_workspace_is_not_switchable(w):
    """D93: the host refuses a hub switch into a workspace joined for chosen items only, so the rail says so and the
    console shows no Open button for it (Ben's 403 on /auth/switch)."""
    token = w.login(w.ann)
    s, o, _ = w.call("GET", "/rail", token=token)
    rows = {x["workspace_id"]: x for x in o["workspaces"]}
    assert s == 200 and rows[WS]["switchable"] is True
    assert rows[WS_SEL]["switchable"] is False


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


from route_paths import concrete_path


def test_a_switch_session_reaches_only_the_named_routes_everything_else_is_closed(w):
    from orch_hub.webapi.api import ROUTES, SWITCH_HANDLERS, SWITCH_REGISTER_HANDLERS, route_scopes

    def fresh():  # one session per route: sign-out ends its own
        _, _, h = w.call("POST", "/switch/redeem", {"t": w.sw.issue_to_hub(w.admin, WS, "/")})
        tok = _tok(h)
        w.browser_tokens.add(tok)
        return tok

    ws = "a" * 32
    seen = 0
    for rt in ROUTES:
        if rt.auth == "none":
            continue
        path = concrete_path(rt, {"workspace_id": ws}).removeprefix("/api/v1")
        s, o, _ = w.call(rt.method, path, {}, token=fresh())
        assert o.get("error") != "unknown_op", (rt.method, rt.path)  # the sweep must really route
        seen += 1
        reauth = s == 403 and o.get("error") == "reauth_required"
        if rt.handler in SWITCH_HANDLERS | SWITCH_REGISTER_HANDLERS:
            assert not reauth, (rt.method, rt.path)
        else:  # closed: asked to sign in for real, or not even reachable with a browser cookie
            pairing_only = "full" not in route_scopes(
                rt
            )  # a pairing-scope route: the cookie session never gets that far
            assert reauth or (pairing_only and o.get("error") in ("no_session", "forbidden_role")), (
                rt.method,
                rt.path,
                s,
                o,
            )
    assert seen > 20


def test_a_channel_session_is_not_a_switch_session(w):
    """B3: the marker is explicit; a password sign-in (and a channel session born of one) is never restricted."""
    tok = w.login(w.ann)
    assert w.api.d.channels.session_info(tok)["via"] is None
    _, _, h = w.call("POST", "/switch/redeem", {"t": w.sw.issue_to_hub(w.ann, WS, "/")})
    sw_tok = _tok(h)
    assert w.api.d.channels.session_info(sw_tok)["via"] == "switch"
    s, o, _ = w.call("GET", "/me/channels", token=tok)
    assert s == 200  # allowed for the password session
    w.browser_tokens.add(sw_tok)
    s, o, _ = w.call("GET", "/me/channels", token=sw_tok)
    assert (s, o["error"]) == (403, "reauth_required")


def test_the_switch_ops_need_a_bound_workspace(w):
    """B5: a workspace whose binding never passed the join checks (a dev host against a real hub is refused there:
    tests/test_link_negative.py dev_refused) gets no ticket and no rail."""
    w.reg.add(WS_SEL, "selective", owner=w.ann, status="unbound")
    for op, body in (
        ("switch_ticket", {"origin": DASH, "return_to": "/", "kind": "open"}),
        ("switch_redeem", {"ticket": "x" * 40}),
        ("rail_get", {"origin": DASH}),
    ):
        with pytest.raises(Refused) as e:
            getattr(ops, op)(ctx(w, WS_SEL, op, body))
        assert e.value.code == "not_bound"


def test_peek_is_rate_limited_and_a_workspace_cannot_flood_the_store(w):
    for _ in range(30):
        assert w.call("POST", "/switch/peek", {"t": "a" * 32})[0] in (403, 200)
    assert w.call("POST", "/switch/peek", {"t": "a" * 32})[0] == 429
    first = w.sw.issue_to_hub(w.ann, WS, "/")
    for _ in range(25):
        w.sw.issue_to_hub(w.ann, WS, "/")
    with pytest.raises(SwitchError):
        w.sw.redeem(first, "to_hub")  # its own oldest went, not other workspaces' tickets


def test_a_switch_session_counts_as_no_sign_in_and_mints_no_child_session(w):
    """N1: `login_ms` is 0 for a switch row (fail closed for every recency check); the allowed handlers make no
    pairing or channel session (a child row would carry via NULL)."""
    _, _, h = w.call("POST", "/switch/redeem", {"t": w.sw.issue_to_hub(w.ann, WS, "/")})
    tok = _tok(h)
    w.browser_tokens.add(tok)
    assert w.api.d.channels.session_info(tok)["login_ms"] == 0
    before = w.api.d.channels.db.q("SELECT COUNT(*) FROM sessions")[0][0]
    w.sw.set_origin(WS, DASH)
    for method, path, body in (
        ("GET", "/session", None),
        ("GET", "/me/workspaces", None),
        ("GET", "/me/needs-you", None),
        ("GET", "/rail", None),
        ("POST", "/switch/ticket", {"workspace_id": WS, "return_to": "/", "kind": "open"}),
    ):
        w.call(method, path, body, token=tok)
    assert w.api.d.channels.db.q("SELECT COUNT(*) FROM sessions")[0][0] == before


def test_owner_get_names_the_owner_alias_and_nothing_else(w):
    """FB15: the host asks over its own link who the hub names as owner; no argument, no stored origin."""
    r = ops.owner_get(ctx(w, WS, "owner_get", {}))
    assert r == {"user_id": w.ann}


def test_a_loopback_browser_gets_the_loopback_origin_only_when_the_hub_says_it_listens_there(w):
    """FB10: the hub's own knowledge, never a guess. Nothing declared: the hub's own origin, whatever the browser says;
    a foreign listener on that port is therefore never handed a ticket."""
    body = {"origin": DASH, "return_to": "/", "kind": "open"}
    r = ops.switch_ticket(ctx(w, WS, "switch_ticket", body))
    assert r["url"].startswith(ORIGIN + "/#t=")
    w.sw.set_loopback_origin(lambda: "http://127.0.0.1:9001")
    # a link whose peer is not on this computer may claim a loopback browser: it still gets the hub's own origin
    r = ops.switch_ticket(ctx(w, WS, "switch_ticket", body))
    assert r["url"].startswith(ORIGIN + "/#t=")
    assert ops.rail_get(ctx(w, WS, "rail_get", {"origin": DASH}))["hub"]["origin"] == ORIGIN
    same = ctx(w, WS, "switch_ticket", body)
    same.same_computer = True
    r = ops.switch_ticket(same)
    assert r["url"].startswith("http://127.0.0.1:9001/#t=")
    far = ops.switch_ticket(ctx(w, WS, "switch_ticket", {**body, "origin": "http://100.64.0.2:7001"}))
    assert far["url"].startswith(ORIGIN + "/#t=")  # a browser that is not on loopback keeps the hub's own origin
    same_rail = ctx(w, WS, "rail_get", {"origin": DASH})
    same_rail.same_computer = True
    assert ops.rail_get(same_rail)["hub"]["origin"] == "http://127.0.0.1:9001"
    assert ops.rail_get(same_rail)["hub"]["network_origin"] == ORIGIN  # the address for other devices, named beside it
    assert ops.rail_get(ctx(w, WS, "rail_get", {"origin": DASH}))["hub"]["network_origin"] is None


def test_same_computer_comes_from_the_socket_not_the_peer():
    from orch_hub.link.tls import TlsConn

    class S:
        def __init__(self, peer, mine):
            self.p, self.m = peer, mine

        def getpeername(self):
            return (self.p, 5)

        def getsockname(self):
            return (self.m, 6)

    def same(peer, mine):
        return TlsConn(None, S(peer, mine)).same_computer()

    assert same("127.0.0.1", "127.0.0.1") and same("::1", "::1")
    assert same("::ffff:127.0.0.1", "::ffff:127.0.0.1")  # a dual-stack socket reports mapped addresses
    assert same("::ffff:100.64.0.7", "100.64.0.7")
    assert same("100.64.0.7", "100.64.0.7")  # this computer dialling its own private address
    assert not same("100.64.0.9", "100.64.0.7")  # another computer


def test_signing_in_after_a_switch_gives_a_normal_session(w):
    """Opus review of #1481: the "Sign in to answer" step is a plain password sign-in; its session is not a switch session."""
    _, _, h = w.call("POST", "/switch/redeem", {"t": w.sw.issue_to_hub(w.admin, WS, "/")})
    sw_tok = _tok(h)
    w.browser_tokens.add(sw_tok)
    assert w.api.d.channels.session_info(sw_tok)["via"] == "switch"
    real = w.login(w.admin)
    assert w.api.d.channels.session_info(real)["via"] is None
    s, me, _ = w.call("GET", "/session", token=real)
    assert s == 200 and me["needs_sign_in"] is False


def test_workspace_rows_fall_back_to_the_owner_never_an_id_and_count_several(w):
    from orch_hub.switch import workspace_rows

    w.reg.ws[WS]["name"] = None
    w.reg.ws[WS_SEL]["name"] = None
    rows = {r["workspace_id"]: r for r in workspace_rows(w.reg, w.env.acc, w.ann)}
    names = sorted(r["name"] for r in rows.values())
    assert names == ["ann's workspace", "ann's workspace 2"] and all(r["owner_name"] == "ann" for r in rows.values())
    assert all(WS[:8] not in r["name"] for r in rows.values())


def test_a_connect_link_for_ben_issues_to_ben_only_and_the_owner_must_be_a_real_active_person(w):
    admin = w.login(w.admin)
    ben = w.env.user("ben")
    ws = "e" * 32
    w.reg.add(ws, "full", owner=ben)
    r = ops.switch_ticket(ctx(w, ws, "switch_ticket", {"origin": DASH, "return_to": "/", "kind": "open"}))
    assert w.sw.peek(r["ticket"], "to_hub")["user"] == ben  # the ticket is for the workspace's owner alias, nobody else
    w.api.d.invites = type(
        "I", (), {"mint": lambda self, now, owner_user=None: ("code" + str(owner_user)[:4], now + 1)}
    )()
    w.api.d.keys = type("K", (), {"hub_id": "a" * 32})()
    w.api.d.api_origin = ORIGIN
    s, o, _ = w.call("POST", "/admin/connect", {"owner_user": ben}, token=admin)
    assert s == 200 and o["owner_user"] == ben and o["link"].endswith("~" + ben)
    for bad in ("f" * 32, 5, "", None):
        assert w.call("POST", "/admin/connect", {"owner_user": bad}, token=admin)[:2] == (400, {"error": "malformed"})
    w.env.acc.set_disabled(ben, True, actor=w.admin)
    assert w.call("POST", "/admin/connect", {"owner_user": ben}, token=admin)[1] == {"error": "malformed"}


def test_run15_p03_a_switch_session_registers_a_browser_key_only_with_password_and_code(w):
    """P03: reg_begin/reg_complete are open to a workspace-vouched session, but reg_begin re-proves password + a fresh code
    and refuses every kind but browser; the rest of the closed list stays closed."""
    from channels_helpers import new_channel, prove
    from orch_hub.contract.canon import b64u
    from orch_hub.webapi.api import SWITCH_HANDLERS, SWITCH_REGISTER_HANDLERS

    assert SWITCH_REGISTER_HANDLERS == {"reg_begin", "reg_complete"} and not SWITCH_REGISTER_HANDLERS & SWITCH_HANDLERS
    _, _, h = w.call("POST", "/switch/redeem", {"t": w.sw.issue_to_hub(w.ann, WS, "/")})
    tok = _tok(h)
    w.browser_tokens.add(tok)
    k = new_channel()
    pub = b64u(k.public)
    s, o, _ = w.call("POST", "/channels/register/begin", {"kind": "phone", "key_pub": pub, "pairing_code": "x"}, token=tok)
    assert (s, o["error"]) == (403, "reauth_required")
    s, o, _ = w.call("POST", "/channels/register/begin", {"kind": "browser", "key_pub": pub}, token=tok)
    assert (s, o["error"]) == (400, "malformed")  # no password and code: nothing is registered
    bad = {"kind": "browser", "key_pub": pub, "password": "wrong password", "totp": w.env.next_code(w.ann)}
    s, o, _ = w.call("POST", "/channels/register/begin", bad, token=tok)
    assert s != 200 and w.env.svc.db.q("SELECT COUNT(*) FROM channels WHERE user_id=?", (w.ann,))[0][0] == 0
    s, begun, _ = w.call("POST", "/channels/register/begin", {"kind": "browser", "key_pub": pub, "label": "B", **w.creds(w.ann)}, token=tok)
    assert s == 200, begun
    s, got, _ = w.call("POST", "/channels/register/complete", {"challenge_id": begun["challenge_id"], "signed": prove(k, begun)}, token=tok)
    assert s == 200 and got["channel_cert"]["o"]["factor"] == "password+totp"
    # still a switch session: the closed list stays closed
    s, o, _ = w.call("GET", "/admin/users", token=tok)
    assert (s, o["error"]) == (403, "reauth_required")
