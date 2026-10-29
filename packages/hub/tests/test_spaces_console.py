# ruff: noqa: F811, RUF059  (the `shub` fixture is imported then named as an argument)
"""M3-2: the shared spaces page's server side (#1443 #1444 #1447), Opus condition 10: a member's mode and the archive flag
are enforced at the hub's ``space_put``, only an admin sets them, and an edits -> reads change applies at once."""

from __future__ import annotations

import pytest
from spaces_helpers import ADMIN, ALICE, BOB, WS_C, Host, shub  # noqa: F401
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


def test_a_non_member_is_told_not_member_before_anything_about_the_archive(shub):
    from orch_hub.contract.hub import Refused

    h, a, b, sid, ha, hb = setup(shub)
    h.svc.set_archived(sid, True, ADMIN, 1_000)
    outsider = G.fake16("outsider")
    for call, body in (("put", ha.put_body(sid, IT2)), ("revoke", {"space_id": sid, "item_id": IT1})):
        with pytest.raises(Refused) as e:
            getattr(h.svc, call)(outsider, body)
        assert e.value.code == "not_member"  # not "archived": an outsider learns nothing about the space
    refused(a, "space_put", ha.put_body(sid, IT2), "archived")  # a member still hears why


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
            from orch_hub.activity.store import ActivityLog

            log = w.api.d.activity = ActivityLog(":memory:")
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
            kinds = {e["type"]: e for e in log.list(limit=20)}
            assert kinds["space.archived"]["detail"] == {"space": sid}  # the space is named, never what is in it
            assert kinds["space.mode_set"]["detail"] == {"mode": "reads", "space": sid}
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


# --- SP-2: create, members and history from the console (#1443 #1445) -------------------------------------------------


def _b64(x: bytes) -> str:
    return G.b64u(x)


class Console:
    """A web API over a real SpaceService, a signed-in hub admin, two full workspaces (hosts a, b) and one selective."""

    def __init__(self, tmp_path):
        from spaces_helpers import KEY, SHub
        from webapi_helpers import Web

        from orch_hub.activity.store import ActivityLog

        self.w = Web(tmp_path)
        self.sh = SHub()
        d = self.w.api.d
        d.spaces, d.registry, self.log = self.sh.svc, self.sh.registry, ActivityLog(":memory:")
        d.activity = self.log
        self.w.reg = self.sh.registry
        self.sh.access.add_user(self.w.admin, "webadmin")
        self.sh.access.add_member("hub-admin", self.w.admin)
        self.a, self.b = self.sh.join(G.WS_A), self.sh.join(G.WS_B)
        self.c = self.sh.join(WS_C, "selective")
        self.admin, self.ann = self.w.login(self.w.admin), self.w.login(self.w.ann)
        self.ha, self.hb, self.KEY = Host(self.a, G.WS_A), Host(self.b, G.WS_B), KEY

    def call(self, method, path, body=None, **kw):
        kw.setdefault("token", self.admin)
        s, o, _ = self.w.call(method, path, body, **kw)
        return s, o

    def space(self):
        s, o = self.call(
            "POST",
            "/admin/spaces",
            {"type": "wiki", "name": "Finance", "members": [G.WS_A, G.WS_B], "understood": True},
        )
        assert s == 200, o
        self.ha.sync(o["space_id"])
        self.hb.sync(o["space_id"])
        return o["space_id"]

    def put(self, host, sid, item_id, body, created_ms=1_000):
        """A signed, sealed put with the given body and signed time (what a real host does)."""
        from orch_hub.contract import keystore as KS
        from orch_hub.contract import spaces as SP

        sp = host.space
        o = {
            "v": 2, "suite": 2, "kind": "space.item", "hv": 0, "hub_id": G.HUB_ID, "space_id": sid, "epoch": sp["epoch"],
            "members_hash": SP.members_hash(sp["members"]), "item_id": item_id, "workspace_id": host.ws,
            "created_ms": created_ms, "body": body, "dev": True,
        }  # fmt: skip
        signed = KS.WskSigner(G.K.priv[self.KEY[host.ws]]).sign_object(o)
        sealed = SP.seal_item(host.sk[sp["epoch"]], sid, sp["epoch"], item_id, signed, salt=SP.new_salt())
        host.link.request("space_put", {"space_id": sid, "epoch": sp["epoch"], "item_id": item_id, "sealed": sealed})

    def close(self):
        self.sh.close()
        self.w.close()


@pytest.fixture
def con(tmp_path):
    c = Console(tmp_path)
    yield c
    c.close()


def test_create_refuses_unless_understood_is_exactly_true(con):
    good = {"type": "wiki", "name": "Finance", "members": [G.WS_A], "understood": True}
    for bad in ({**good, "understood": False}, {**good, "understood": "true"}, {**good, "understood": 1}):
        assert con.call("POST", "/admin/spaces", bad)[0] == 400
    assert con.call("POST", "/admin/spaces", {k: v for k, v in good.items() if k != "understood"})[0] == 400
    assert con.sh.svc.list_all() == []  # nothing was created
    for bad in ({**good, "type": "chat"}, {**good, "name": " "}, {**good, "members": []}, {**good, "extra": 1}):
        assert con.call("POST", "/admin/spaces", bad)[0] == 400
    assert con.call("POST", "/admin/spaces", {**good, "members": [G.fake16("nobody")]})[0] == 404
    assert con.call("POST", "/admin/spaces", good)[0] == 200


def test_create_names_the_selective_workspace_as_waiting_and_records_activity(con):
    s, o = con.call(
        "POST", "/admin/spaces",
        {"type": "files", "name": "Plans", "purpose": "Shared plans", "members": [G.WS_A, WS_C], "understood": True},
    )  # fmt: skip
    assert s == 200 and o["members"] == [G.WS_A] and [x["workspace_id"] for x in o["waiting"]] == [WS_C]
    sid = o["space_id"]
    assert con.sh.svc.list_all()[0]["members"] == [G.WS_A]  # the selective one is NOT in, until it accepts at its host
    assert con.call("GET", "/admin/spaces/about")[1] == {"about": {sid: {"type": "files", "purpose": "Shared plans"}}}
    ev = [e for e in con.log.list(limit=10) if e["type"] == "space.created"]
    assert len(ev) == 1 and ev[0]["detail"] == {"space": sid}
    only_selective = {"type": "wiki", "name": "X", "members": [WS_C], "understood": True}
    assert con.call("POST", "/admin/spaces", only_selective)[0] == 409  # nobody can be added now


def test_only_a_hub_admin_creates_and_a_cross_origin_post_is_refused(con):
    body = {"type": "wiki", "name": "Finance", "members": [G.WS_A], "understood": True}
    assert con.call("POST", "/admin/spaces", body, token=con.ann)[0] == 403
    assert con.call("POST", "/admin/spaces", body, token=None)[0] == 401
    assert con.call("POST", "/admin/spaces", body, origin="https://evil.example")[0] == 403
    assert con.call("POST", "/admin/spaces", body, origin=None)[0] == 403  # a cookie without a browser origin
    assert con.call("POST", "/admin/spaces", body, csrf=False)[0] == 403
    assert con.call("POST", "/admin/spaces", body, ctype="text/plain")[0] == 403
    assert con.sh.svc.list_all() == []
    sid = con.space()
    for path, b in (
        (f"/admin/spaces/{sid}/members", {"workspace_id": WS_C}),
        (f"/admin/spaces/{sid}/members/remove", {"workspace_id": G.WS_B}),
    ):
        assert con.call("POST", path, b, origin="https://evil.example")[0] == 403
        assert con.call("POST", path, b, token=con.ann)[0] == 403
    assert con.call("GET", f"/admin/spaces/{sid}/history", token=con.ann)[0] == 403
    assert con.sh.svc.list_all()[0]["members"] == sorted([G.WS_A, G.WS_B])


def test_add_and_remove_a_member_record_activity_and_a_selective_one_waits(con):
    sid = con.space()
    s, o = con.call("POST", f"/admin/spaces/{sid}/members", {"workspace_id": WS_C})
    assert s == 200 and o["added"] is False and o["waiting"][0]["workspace_id"] == WS_C
    assert con.call("POST", f"/admin/spaces/{sid}/members", {"workspace_id": G.WS_A})[0] == 409  # already in
    assert con.call("POST", f"/admin/spaces/{sid}/members", {"workspace_id": G.fake16("x")})[0] == 404
    assert con.call("POST", f"/admin/spaces/{sid}/members", {"workspace_id": G.WS_A, "x": 1})[0] == 400
    s, o = con.call("POST", f"/admin/spaces/{sid}/members/remove", {"workspace_id": G.WS_B})
    assert (s, o) == (200, {"ended": False, "members": [G.WS_A]})
    assert con.call("POST", f"/admin/spaces/{sid}/members/remove", {"workspace_id": G.WS_B})[0] == 404  # no longer in
    types = [e["type"] for e in con.log.list(limit=20)]
    assert "space.member_removed" in types and "space.member_added" not in types  # the waiting add changed nothing
    # a full workspace added later: a new epoch and an activity record
    sh = con.sh
    d = sh.svc
    assert d.list_all()[0]["members"] == [G.WS_A]
    s, o = con.call("POST", f"/admin/spaces/{sid}/members", {"workspace_id": G.WS_B})
    assert s == 200 and o["added"] is True and o["members"] == sorted([G.WS_A, G.WS_B])
    assert [e["detail"] for e in con.log.list(limit=20) if e["type"] == "space.member_added"] == [{"space": sid}]


def test_history_lists_versions_newest_first_with_a_fixed_by_table(con):
    sid = con.space()

    def t(s):
        return {"name": "plan.md", "data": _b64(s.encode()), "size": len(s)}

    con.put(con.ha, sid, G.fake16("v1"), {**t("one"), "by": "person:u1"}, 1_000)
    con.put(con.hb, sid, G.fake16("v2"), {**t("two"), "by": "agent:s_9"}, 3_000)
    con.put(con.ha, sid, G.fake16("v3"), {**t("three"), "by": "root:admin", "restored_from": G.fake16("v1")}, 2_000)
    con.put(con.ha, sid, G.fake16("v4"), t("no by"), 4_000)  # a body without `by` (older host)
    con.put(con.ha, sid, G.fake16("v5"), {**t("hi"), "name": "other.md", "by": ["agent:x"]}, 500)
    s, o = con.call("GET", f"/admin/spaces/{sid}/history")
    assert s == 200
    names = {n["name"]: n["versions"] for n in o["names"]}
    plan = names["plan.md"]
    assert [v["content"]["text"] for v in plan] == ["no by", "two", "three", "one"]  # newest first by the SIGNED time
    assert [v["by"] for v in plan] == [
        "unknown",
        "agent",
        "unknown",
        "person",
    ]  # a fixed table; "root:admin" is unknown
    assert [v["restored"] for v in plan] == [False, False, True, False]
    assert all(set(v) == {"item_id", "created_ms", "author", "by", "restored", "revoked", "content"} for v in plan)
    assert names["other.md"][0]["by"] == "unknown"  # a list is not an actor
    assert "u1" not in str(o) and "s_9" not in str(o)  # the actor id is never passed on
    one = con.call("GET", f"/admin/spaces/{sid}/history?name=other.md")[1]
    assert [n["name"] for n in one["names"]] == ["other.md"]
    assert con.call("GET", f"/admin/spaces/{G.fake16('zz')}/history")[0] == 404


def test_history_caps_text_marks_binary_and_shows_removed_and_hostile_text_as_plain_text(con):
    sid = con.space()
    big = ("é" * 3000).encode()  # 6000 bytes of UTF-8
    con.put(con.ha, sid, G.fake16("t1"), {"name": "big.txt", "data": _b64(big), "size": len(big)}, 1)
    con.put(con.ha, sid, G.fake16("t2"), {"name": "bin.dat", "data": _b64(b"\xff\xfe\x00\x01" * 100), "size": 400}, 2)
    hostile = "<img src=x onerror=alert(1)>\u202e http://evil.example"
    con.put(con.ha, sid, G.fake16("t3"), {"name": "h.txt", "data": _b64(hostile.encode()), "size": 1}, 3)
    con.put(con.ha, sid, G.fake16("t6"), {"name": "esc.txt", "data": _b64(b"\x1b[31mhi"), "size": 7}, 6)
    con.put(con.ha, sid, G.fake16("t4"), {"name": "bad.txt", "data": "***not base64***", "size": 1}, 4)
    con.put(con.ha, sid, G.fake16("t5"), {"name": "gone.txt", "data": _b64(b"x"), "size": 1}, 5)
    con.a.request("space_revoke", {"space_id": sid, "item_id": G.fake16("t5")})
    o = con.call("GET", f"/admin/spaces/{sid}/history")[1]
    by = {n["name"]: n["versions"][0] for n in o["names"]}
    assert by["big.txt"]["content"]["cut"] is True and len(by["big.txt"]["content"]["text"].encode()) <= 4096
    assert by["bin.dat"]["content"] == {"file": 400}
    assert by["bad.txt"]["content"] == {"file": 0}
    txt = by["h.txt"]["content"]["text"]
    assert "<img" in txt and "\u202e" not in txt and "<U+202E>" in txt  # markup stays text; a bidi override is escaped
    assert by["esc.txt"]["content"] == {"file": 7}  # terminal escapes: not text at all
    assert "gone.txt" not in str(o["names"]) and len(o["removed"]) == 1 and o["removed"][0]["revoked"] is True
    assert "x" not in str(o["removed"][0].get("content", ""))  # a removed item has no content


def test_history_shows_nothing_for_an_item_that_does_not_verify(con):
    sid = con.space()
    con.put(con.ha, sid, G.fake16("ok"), {"name": "a.txt", "data": _b64(b"fine"), "size": 4}, 1)
    row = con.sh.store.items(sid)[G.fake16("ok")]
    import sqlite3  # noqa: F401

    con.sh.store._db.execute("UPDATE space_item SET sealed=? WHERE item_id=?", ("AAAA", G.fake16("ok")))
    assert con.call("GET", f"/admin/spaces/{sid}/history")[1] == {"names": [], "removed": []}
    _ = row


def test_the_console_has_no_route_that_signs_puts_restores_or_revokes(con):
    from orch_hub.webapi.api import ROUTES

    space = [r for r in ROUTES if "/spaces" in r.path]
    assert sorted((r.method, r.path.removeprefix("/api/v1")) for r in space) == sorted([
        ("GET", "/admin/spaces"), ("PUT", "/admin/spaces/{space_id}/members/{workspace_id}/mode"),
        ("POST", "/admin/spaces/{space_id}/archive"), ("POST", "/admin/spaces"), ("GET", "/admin/spaces/about"),
        ("POST", "/admin/spaces/{space_id}/members"), ("POST", "/admin/spaces/{space_id}/members/remove"),
        ("GET", "/admin/spaces/{space_id}/history"),
    ])  # fmt: skip
    assert all(r.auth == "admin" for r in space)
    for r in space:
        assert not any(w in (r.path + r.handler).lower() for w in ("put", "restore", "revoke", "sign", "item"))
    import inspect

    from orch_hub.webapi import spaces_api

    src = inspect.getsource(spaces_api)
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith(("#", '"""')))
    for call in (".put(", ".revoke(", "restore(", "sign_object", "Signer"):
        assert call not in code.replace('"""', "").split("def space_routes")[0].replace("Never a route", "")
