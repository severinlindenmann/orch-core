# ruff: noqa: F811  (the `w` fixture is imported, then named as a test argument)
"""M2-8b: explicit connections between workspaces (#1414-#1417), Opus conditions 11-13 and 15.

* 11 approver rules: not the proposer; the receiving owner alias or an EXPLICIT workspace admin grant; a hub-admin-group
  member alone does not count; a SELECTIVE recipient: its owner alias only; closing is immediate;
* 12 ``may_route`` = a connection AND the access rule, fail closed; the one-time backfill is audited as ``migrated``;
* 13 route stats are counts only, scoped, with a 90-day retention.
"""

from __future__ import annotations

import pytest
from router_helpers import envelope, rhub  # noqa: F401
from test_access_changes import activity, approve, owner_session, prepare, w  # noqa: F401
from webapi_helpers import WS_SEL

from orch_hub.access.store import ADMIN_GROUP
from orch_hub.connections import ConnectionAuthz, Connections
from orch_hub.contract import vectorgen as G
from orch_hub.link import LinkError
from orch_hub.router import AccessAuthz, BoundOnly

WS2, WS3 = "a1" * 16, "b2" * 16  # the test world: olive owns WS2, omar owns WS3 (both in "owners": admin on both)
DAY = 86_400_000


def propose(w, tok, a, b):
    s, o, _ = w.call("POST", "/me/connections", {"from": a, "to": b, "allow": True}, token=tok)
    assert s == 202, o
    return o["change_id"]


def allowed(w, a, b):
    return w.connections.allowed(a, b)


# --- 12: routing is a connection AND the access rule -------------------------------------------------------------------


def test_may_route_needs_both_a_connection_and_access(w):
    authz = ConnectionAuthz(w.connections, AccessAuthz(w.reg, w.env.acc))
    assert AccessAuthz(w.reg, w.env.acc).may_route(WS2, WS3) is True  # access alone: allowed ...
    assert authz.may_route(WS2, WS3) is False  # ... but no connection: refused
    w.connections.add(WS2, WS3, approved_by=w.omar, via="approved")
    assert authz.may_route(WS2, WS3) is True
    assert authz.may_route(WS3, WS2) is False  # directional
    w.env.acc.revoke(WS3, "owners")  # a connection never grants more than access allows
    assert authz.may_route(WS2, WS3) is False


def test_a_selective_recipient_is_never_routed_and_errors_fail_closed(w):
    w.connections.add(WS2, WS_SEL, approved_by=None, via="override")
    assert ConnectionAuthz(w.connections, AccessAuthz(w.reg, w.env.acc)).may_route(WS2, WS_SEL) is False

    class Boom:
        def may_route(self, a, b):
            raise RuntimeError("store down")

    w.connections.add(WS2, WS3, approved_by=None, via="override")
    assert ConnectionAuthz(w.connections, Boom()).may_route(WS2, WS3) is False
    w.connections.close()  # a closed store answers "no", never "yes"
    assert ConnectionAuthz(w.connections, BoundOnly(w.reg)).may_route(WS2, WS3) is False
    w.connections = Connections(":memory:")  # so the fixture can close


def test_backfill_runs_once_and_keeps_current_routes(w):
    store = Connections(":memory:")
    pairs = [(WS2, WS3), (WS3, WS2)]
    assert store.backfill(pairs) == 2
    assert store.backfill(pairs) is None  # exactly once
    assert {(e["from"], e["to"], e["via"]) for e in store.edges()} == {(WS2, WS3, "migrated"), (WS3, WS2, "migrated")}
    assert Connections(":memory:").backfill([]) == 0  # a fresh hub: nothing carried over, still flagged


def test_serve_wires_connection_authz_stats_and_backfill():
    import ast
    from pathlib import Path

    import orch_hub.serve as serve_module

    src = Path(serve_module.__file__).read_text()
    tree = ast.parse(src)
    routers = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "Router"]
    assert routers and any(any(k.arg == "on_routed" for k in r.keywords) for r in routers)
    assert "ConnectionAuthz(connections, access_authz)" in src and "connections.backfill(" in src
    assert "connection.migrated" in src


# --- 11: who may ask, who may approve ---------------------------------------------------------------------------------


def test_an_owner_approves_a_connection_into_their_workspace_and_it_is_audited(w):
    tok = w.login(w.admin)
    cid = propose(w, tok, WS2, WS3)
    assert not allowed(w, WS2, WS3)  # nothing is connected until an approver says yes
    otok, osig, och = owner_session(w, w.omar)
    mine = w.call("GET", "/me/changes", token=otok)[1]["pending"]
    assert next((c["id"], c["kind"], c["from_name"], c["to_name"]) for c in mine)[:2] == (cid, "connection_allow")
    s, o, _ = approve(w, otok, cid, osig, och)
    assert (s, o["state"]) == (200, "applied")
    assert allowed(w, WS2, WS3) and not allowed(w, WS3, WS2)
    assert [r["detail"].get("approved_by") for r in activity(w, "connection.allowed")] == [w.omar]


def test_the_proposer_cannot_approve_their_own_proposal(w):
    otok, _osig, _och = owner_session(w, w.olive)  # olive owns the SENDER and proposes
    cid = propose(w, otok, WS2, WS3)
    assert w.call("POST", f"/me/changes/{cid}/approve/prepare", {}, token=otok)[:2] == (
        404,
        {"error": "unknown_change"},
    )
    assert not allowed(w, WS2, WS3)
    mtok, msig, mch = owner_session(w, w.omar)  # omar (receiving owner) can
    assert approve(w, mtok, cid, msig, mch)[0] == 200 and allowed(w, WS2, WS3)


def test_hub_admin_group_membership_alone_cannot_approve(w):
    tok = w.login(w.admin)
    cid = propose(w, tok, WS2, WS3)
    admin2 = w.env.user("admin2")
    w.env.acc.add_member(ADMIN_GROUP, admin2)
    for user in (w.admin, admin2):
        t = w.login(user)
        assert w.call("GET", "/me/changes", token=t)[1]["pending"] == []
        assert w.call("POST", f"/me/changes/{cid}/approve/prepare", {}, token=t)[:2] == (
            404,
            {"error": "unknown_change"},
        )
    assert not allowed(w, WS2, WS3)


def test_an_explicit_workspace_admin_grant_may_approve(w):
    w.env.acc.add_group("wsadmins")
    w.env.acc.add_member("wsadmins", w.dana)
    w.env.acc.grant(WS3, "wsadmins", "admin")  # explicit, workspace-level
    tok = w.login(w.admin)
    cid = propose(w, tok, WS2, WS3)
    dtok, dsig, dch = owner_session(w, w.dana)
    assert approve(w, dtok, cid, dsig, dch)[0] == 200 and allowed(w, WS2, WS3)
    assert [r["detail"].get("approved_by") for r in activity(w, "connection.allowed")] == [w.dana]


def test_a_selective_recipient_only_its_owner_alias_may_approve_into_it(w):
    w.env.acc.add_group("wsadmins")
    w.env.acc.add_member("wsadmins", w.dana)
    w.env.acc.grant(WS_SEL, "wsadmins", "admin")  # hub roles grant nothing in a selective workspace
    assert w.changes.eligible_approvers(WS_SEL) == [w.olive]


def test_nobody_to_approve_means_no_approver(w):
    ws9 = "c9" * 16
    w.reg.add(ws9, "full", owner=None, name="Orphan")
    s, o, _ = w.call("POST", "/me/connections", {"from": WS2, "to": ws9, "allow": True}, token=w.login(w.admin))
    assert (s, o) == (409, {"error": "no_approver"})


def test_who_may_propose_and_malformed_requests(w):
    assert w.call("POST", "/me/connections", {"from": WS2, "to": WS3, "allow": True}, token=w.login(w.newbie))[0] == 403
    admin = w.login(w.admin)
    for body in (
        {"from": WS2, "to": WS2, "allow": True},
        {"from": "x", "to": WS3, "allow": True},
        {"from": WS2, "to": WS3},
    ):
        assert w.call("POST", "/me/connections", body, token=admin)[0] == 400
    w.connections.add(WS2, WS3, approved_by=None, via="override")
    assert w.call("POST", "/me/connections", {"from": WS2, "to": WS3, "allow": True}, token=admin)[0] == 409


def test_closing_is_immediate_for_either_side_and_withdraws_pending_asks(w):
    tok = w.login(w.admin)
    cid = propose(w, tok, WS3, WS2)
    w.connections.add(WS2, WS3, approved_by=None, via="override")
    assert w.call("POST", "/me/connections", {"from": WS2, "to": WS3, "allow": False}, token=w.login(w.olive))[1][
        "removed"
    ]
    assert not allowed(w, WS2, WS3)  # the sender's owner closed it
    w.connections.add(WS2, WS3, approved_by=None, via="override")
    assert w.call("POST", "/me/connections", {"from": WS2, "to": WS3, "allow": False}, token=w.login(w.omar))[0] == 200
    assert not allowed(w, WS2, WS3)  # the receiving side closed it
    w.connections.add(WS3, WS2, approved_by=None, via="override")
    assert (
        w.call("POST", "/me/connections", {"from": WS3, "to": WS2, "allow": False}, token=w.login(w.newbie))[0] == 403
    )
    assert w.call("POST", "/me/connections", {"from": WS3, "to": WS2, "allow": False}, token=tok)[0] == 200
    assert w.changes._db.change(cid)["state"] == "cancelled"  # the ask for the same pair is withdrawn
    assert len(activity(w, "connection.denied")) == 3


def test_an_override_is_audited_and_the_receiving_owner_is_told(w):
    tok = w.login(w.admin)
    cid = propose(w, tok, WS2, WS3)
    assert w.call("POST", f"/admin/changes/{cid}/override", {"reason": "needed tonight"}, token=tok)[0] == 200
    assert allowed(w, WS2, WS3) and w.connections.edges()[0]["via"] == "override"
    assert [r["workspace_id"] for r in activity(w, "change.overridden")] == [WS3]
    notices = w.call("GET", "/me/changes", token=w.login(w.omar))[1]["notices"]
    assert [n["id"] for n in notices] == [cid] and notices[0]["reason"] == "needed tonight"


def test_the_ask_goes_stale_when_an_end_leaves_the_hub(w):
    tok = w.login(w.admin)
    cid = propose(w, tok, WS2, WS3)
    w.reg.ws[WS3]["status"] = "unbound"  # the receiving workspace left
    otok, _osig, _och = owner_session(w, w.omar)
    s, o, _ = w.call("POST", f"/me/changes/{cid}/approve/prepare", {}, token=otok)
    assert (s, o) == (409, {"error": "stale_change"}) and not allowed(w, WS2, WS3)


# --- 13: counts only, scoped, retained 90 days -------------------------------------------------------------------------


def test_route_stats_are_counts_only_with_90_day_retention():
    clock = {"t": 100 * DAY}
    store = Connections(":memory:", now_ms=lambda: clock["t"])
    store.bump("a", "b")
    store.bump("a", "b")
    clock["t"] += 40 * DAY
    store.bump("a", "b")
    assert store.traffic() == [{"from": "a", "to": "b", "n": 1}]  # the first two are older than 30 days
    columns = {r[1] for r in store._db.execute("PRAGMA table_info(route_stats)")}
    assert columns == {"day", "from_ws", "to_ws", "n"}  # no content, no ticket, no person
    clock["t"] += 60 * DAY
    store.bump("a", "b")  # prunes anything older than 90 days
    assert store._db.execute("SELECT COUNT(*) FROM route_stats").fetchone()[0] == 2


def test_traffic_is_scoped_to_the_pairs_a_person_is_responsible_for(w):
    ws9 = "d9" * 16
    w.reg.add(ws9, "full", owner=w.dana, name="Third")
    w.connections.bump(WS2, WS3)
    w.connections.bump(WS3, ws9)
    w.connections.bump(WS3, ws9)
    got = lambda user: w.call("GET", "/me/connections/traffic", token=w.login(user))[1]["traffic"]
    assert {(t["from"], t["to"], t["n"]) for t in got(w.admin)} == {(WS2, WS3, 1), (WS3, ws9, 2)}  # a hub admin: all
    assert {(t["from"], t["to"]) for t in got(w.dana)} == {(WS3, ws9)}  # the owner of ws9: pairs including it
    # olive owns WS2 and holds an explicit admin grant on WS3 (via "owners"): both pairs involve a workspace she answers for
    assert {(t["from"], t["to"]) for t in got(w.olive)} == {(WS2, WS3), (WS3, ws9)}
    assert got(w.newbie) == []


def test_the_router_counts_accepted_hand_offs_and_refuses_unconnected_ones(rhub):
    store = Connections(":memory:")
    h = rhub(authz=ConnectionAuthz(store, BoundOnly(None) if False else _AlwaysAccess()), on_routed=store.bump)
    a, b = h.join(G.WS_A), h.join(G.WS_B)
    with pytest.raises(LinkError) as e:
        a.route_out(envelope())  # no connection yet
    assert e.value.code == "not_allowed" and store.traffic() == []
    store.add(G.WS_A, G.WS_B, approved_by=None, via="override")
    assert a.route_out(envelope()) == "forwarded"
    assert store.traffic() == [{"from": G.WS_A, "to": G.WS_B, "n": 1}]
    b.next_route_in()


class _AlwaysAccess:
    def may_route(self, a, b):
        return True


# --- the map endpoint ---------------------------------------------------------------------------------------------------


def test_the_admin_map_shows_effective_edges_stored_connections_pending_and_counts(w):
    w.api.d.authz = ConnectionAuthz(w.connections, AccessAuthz(w.reg, w.env.acc))  # as serve() wires it
    tok = w.login(w.admin)
    cid = propose(w, tok, WS3, WS2)
    w.connections.add(WS2, WS3, approved_by=None, via="migrated")
    w.connections.bump(WS2, WS3)
    s, o, _ = w.call("GET", "/admin/connections", token=tok)
    assert s == 200
    assert {"from": WS2, "to": WS3} in o["edges"] and {"from": WS3, "to": WS2} not in o["edges"]
    assert [(c["from"], c["to"], c["via"]) for c in o["connections"]] == [(WS2, WS3, "migrated")]
    assert [p["id"] for p in o["pending"]] == [cid] and o["traffic"] == [{"from": WS2, "to": WS3, "n": 1}]
    assert w.call("GET", "/admin/connections", token=w.login(w.ann))[0] == 403


def test_removing_a_workspace_removes_its_connections_and_counts(w):
    from webapi_helpers import WS

    w.connections.add(WS, WS3, approved_by=None, via="override")
    w.connections.add(WS3, WS, approved_by=None, via="override")
    w.connections.add(WS2, WS3, approved_by=None, via="override")
    w.connections.bump(WS, WS3)
    w.reg.add(WS, "full", status="unbound", left_ms=1730000000000)
    assert w.call("DELETE", f"/admin/workspaces/{WS}", token=w.login(w.admin))[0] == 200
    assert [(e["from"], e["to"]) for e in w.connections.edges()] == [(WS2, WS3)]
    assert w.connections.traffic() == []


# --- the owner of both ends: no dead end ---------------------------------------------------------------------------------


def test_the_owner_of_both_ends_connects_directly_and_it_is_audited(w):
    w.reg.ws[WS3]["owner"] = w.olive  # olive now owns both WS2 and WS3
    s, o, _ = w.call("POST", "/me/connections", {"from": WS2, "to": WS3, "allow": True}, token=w.login(w.olive))
    assert (s, o) == (200, {"ok": True, "direct": True})
    assert allowed(w, WS2, WS3) and w.changes._db.changes() == []  # no pending change
    rec = activity(w, "connection.allowed")
    assert [(r["actor_id"], r["detail"]) for r in rec] == [(w.olive, {"direct": True})]
    assert not allowed(w, WS3, WS2)  # still directional


def test_the_only_eligible_approver_being_the_proposer_who_owns_the_receiver_connects_directly(w):
    wsa = "a9" * 16
    w.reg.add(
        wsa, "full", owner=w.admin, name="Mine"
    )  # the hub admin owns the receiver; no admin grant on it for anyone else
    s, o, _ = w.call("POST", "/me/connections", {"from": WS2, "to": wsa, "allow": True}, token=w.login(w.admin))
    assert (s, o) == (200, {"ok": True, "direct": True}) and allowed(w, WS2, wsa)


def test_direct_needs_ownership_of_the_receiver_and_nobody_else_to_ask(w):
    # a hub admin who owns neither end still waits for the receiving owner (the normal case)
    assert w.call("POST", "/me/connections", {"from": WS2, "to": WS3, "allow": True}, token=w.login(w.admin))[0] == 202
    # an owner of the SENDER only (olive -> WS3 owned by omar) waits too
    assert w.call("POST", "/me/connections", {"from": WS3, "to": WS2, "allow": True}, token=w.login(w.omar))[0] == 202
    # owning the receiver but with another eligible approver (a workspace-admin group) is not direct either
    w.env.acc.add_group("wsadmins")
    w.env.acc.add_member("wsadmins", w.dana)
    w.env.acc.grant(WS3, "wsadmins", "admin")
    assert allowed(w, WS2, WS3) is False and allowed(w, WS3, WS2) is False
