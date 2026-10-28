# ruff: noqa: F811, RUF059  (the `shub` fixture is imported then named as an argument)
"""M3-2: the shared spaces page's server side (#1443 #1444 #1447), Opus condition 10: a member's mode and the archive flag
are enforced at the hub's ``space_put``, only an admin sets them, and an edits -> reads change applies at once."""

from __future__ import annotations

import pytest
from spaces_helpers import ADMIN, ALICE, BOB, Host, shub  # noqa: F401
from test_spaces_service import IT1, IT2, refused, two

from orch_hub.contract import vectorgen as G
from orch_hub.spaces import SpaceError


def setup(shub):
    h, a, b = two(shub)
    sid = h.svc.create("finance", [G.WS_A, G.WS_B], ADMIN)["space_id"]
    ha, hb = Host(a, G.WS_A), Host(b, G.WS_B)
    ha.sync(sid)
    hb.sync(sid)
    return h, a, b, sid, ha, hb


def test_a_reads_member_cannot_put_or_revoke_and_an_edits_change_applies_at_once(shub):
    h, a, b, sid, ha, hb = setup(shub)
    assert a.request("space_put", ha.put_body(sid, IT1)) == {"seq": 1}
    h.svc.set_member_mode(sid, G.WS_A, "reads", ADMIN)  # takes effect on the very next op: no epoch, no key change
    refused(a, "space_put", ha.put_body(sid, IT2), "read_only")
    refused(a, "space_revoke", {"space_id": sid, "item_id": IT1}, "read_only")
    assert (
        hb.sync(sid)["entries"] and a.request("space_get", {"space_id": sid, "since": 0})["entries"]
    )  # reading goes on
    assert b.request("space_put", hb.put_body(sid, IT2)) == {"seq": 2}  # the other member still edits
    h.svc.set_member_mode(sid, G.WS_A, "edits", ADMIN)
    assert a.request("space_put", ha.put_body(sid, G.fake16("it4"))) == {"seq": 3}
    assert h.store.modes(sid) == {}  # the default is stored as nothing


def test_only_an_admin_sets_a_mode_and_only_for_a_member(shub):
    h, a, b, sid, ha, hb = setup(shub)
    with pytest.raises(SpaceError) as e:
        h.svc.set_member_mode(sid, G.WS_A, "reads", BOB)  # no role at all
    assert e.value.code == "forbidden_role"
    with pytest.raises(SpaceError) as e:
        h.svc.set_member_mode(sid, G.fake16("stranger"), "reads", ADMIN)
    assert e.value.code == "not_member"
    for bad in ("read", "", "admin", None):
        with pytest.raises(SpaceError):
            h.svc.set_member_mode(sid, G.WS_A, bad, ADMIN)
    assert h.store.mode(sid, G.WS_A) == "edits"
    _ = ALICE


def test_an_archived_space_refuses_new_puts_and_revokes_but_reads_go_on(shub):
    h, a, b, sid, ha, hb = setup(shub)
    a.request("space_put", ha.put_body(sid, IT1))
    h.svc.set_archived(sid, True, ADMIN, 1_000)
    refused(a, "space_put", ha.put_body(sid, IT2), "archived")
    refused(b, "space_put", hb.put_body(sid, IT2), "archived")
    refused(a, "space_revoke", {"space_id": sid, "item_id": IT1}, "archived")
    assert len(hb.sync(sid)["entries"]) == 1
    assert h.svc.list_all()[0]["archived_ms"] == 1_000
    h.svc.set_archived(sid, False, ADMIN, 2_000)
    assert a.request("space_put", ha.put_body(sid, IT2)) == {"seq": 2}
    with pytest.raises(SpaceError):
        h.svc.set_archived(sid, True, BOB, 3_000)  # only an admin


def test_a_removed_member_loses_its_mode_and_a_deleted_space_loses_the_flags(shub):
    h, a, b, sid, ha, hb = setup(shub)
    h.svc.set_member_mode(sid, G.WS_B, "reads", ADMIN)
    h.svc.set_archived(sid, True, ADMIN, 1)
    assert h.svc.list_all()[0]["modes"] == {G.WS_A: "edits", G.WS_B: "reads"}
    h.svc.remove(sid, G.WS_B, ADMIN)
    assert h.store.mode(sid, G.WS_B) == "edits"  # a return starts from the default
    h.svc.remove(sid, G.WS_A, ADMIN)  # the last member leaves: the space ends
    assert h.store.archived_ms(sid) is None and h.store.modes(sid) == {}


def test_the_mode_is_not_part_of_any_signed_record(shub):
    h, a, b, sid, ha, hb = setup(shub)
    before = a.request("space_list", {})["spaces"]
    h.svc.set_member_mode(sid, G.WS_A, "reads", ADMIN)
    assert (
        a.request("space_list", {})["spaces"] == before
    )  # same record, same epoch: nothing for the hosts to re-verify


def test_the_admin_routes_list_set_the_mode_archive_and_are_admin_only(tmp_path):
    from webapi_helpers import Web

    w = Web(tmp_path)
    try:
        from spaces_helpers import SHub

        sh = SHub()
        try:
            sid = None
            w.api.d.spaces = sh.svc
            w.reg = sh.registry
            w.api.d.registry = sh.registry
            sh.join(G.WS_A)
            sh.join(G.WS_B)
            sid = sh.svc.create("finance", [G.WS_A, G.WS_B], ADMIN)["space_id"]
            admin = w.login(w.admin)
            w.env.acc.add_member("hub-admin", w.admin)
            s, o, _ = w.call("GET", "/admin/spaces", token=admin)
            assert (
                s == 200
                and o["spaces"][0]["space_id"] == sid
                and {m["mode"] for m in o["spaces"][0]["members"]} == {"edits"}
            )
            actor_ok = (
                sh.access
            )  # the SpaceService authorises against ITS access model: make the web admin one there too
            actor_ok.add_user(w.admin, "webadmin")
            actor_ok.add_member("hub-admin", w.admin)
            assert w.call("PUT", f"/admin/spaces/{sid}/members/{G.WS_A}/mode", {"mode": "reads"}, token=admin)[:2] == (
                200,
                {"mode": "reads"},
            )
            assert w.call("PUT", f"/admin/spaces/{sid}/members/{G.WS_A}/mode", {"mode": "nope"}, token=admin)[0] == 400
            assert (
                w.call("PUT", f"/admin/spaces/{sid}/members/{G.fake16('x')}/mode", {"mode": "reads"}, token=admin)[0]
                == 404
            )
            assert w.call("POST", f"/admin/spaces/{sid}/archive", {"archived": True}, token=admin)[:2] == (
                200,
                {"archived": True},
            )
            got = w.call("GET", "/admin/spaces", token=admin)[1]["spaces"][0]
            assert (
                got["archived_ms"] is not None
                and {m["workspace_id"]: m["mode"] for m in got["members"]}[G.WS_A] == "reads"
            )
            ann = w.login(w.ann)
            for method, path, body in (
                ("GET", "/admin/spaces", None),
                ("PUT", f"/admin/spaces/{sid}/members/{G.WS_A}/mode", {"mode": "reads"}),
                ("POST", f"/admin/spaces/{sid}/archive", {"archived": False}),
            ):
                assert w.call(method, path, body, token=ann)[0] == 403 and w.call(method, path, body)[0] == 401
        finally:
            sh.close()
    finally:
        w.close()
