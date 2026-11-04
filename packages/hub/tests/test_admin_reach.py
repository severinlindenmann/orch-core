"""A live explanation includes every pin, but personal access is owner-only."""

import pytest
from webapi_helpers import WS, Web


@pytest.fixture
def w(tmp_path):
    web = Web(tmp_path)
    yield web
    web.close()


def reach(w, person, token=None):
    return w.call("GET", f"/admin/people/{person}/reach", token=token or w.login(w.admin))


def test_hub_admin_sees_all_pins(w):
    other = "12" * 16
    w.reg.add(other, "full", status="unbound", name="sales")
    status, body, _ = reach(w, w.admin)
    assert status == 200
    assert body["hub_admin"] is True
    assert body["user"]["user_id"] == w.admin
    rows = {r["workspace_id"]: r for r in body["workspaces"]}
    assert set(rows) == {WS, other}
    assert rows[other]["name"] == "sales"
    assert all(r["role"] == "admin" and {"kind": "hub-admin"} in r["via"] for r in rows.values())


def test_all_group_reasons_and_live_changes(w):
    acc = w.env.acc
    acc.add_group("sales")
    acc.add_member("sales", w.ann)
    acc.grant(WS, "sales", "read")
    other = "12" * 16
    w.reg.add(other, "full")
    token = w.login(w.admin)
    status, body, _ = reach(w, w.ann, token)
    assert status == 200 and body["hub_admin"] is False
    rows = {r["workspace_id"]: r for r in body["workspaces"]}
    assert rows[WS]["role"] == "edit"
    assert rows[WS]["via"] == [
        {"kind": "group", "group": "sales", "role": "read"},
        {"kind": "group", "group": "team", "role": "edit"},
    ]
    assert rows[other]["role"] is None and rows[other]["via"] == []
    acc.remove_member("team", w.ann)
    rows = {r["workspace_id"]: r for r in reach(w, w.ann, token)[1]["workspaces"]}
    assert rows[WS]["role"] == "read"
    assert rows[WS]["via"] == [{"kind": "group", "group": "sales", "role": "read"}]


def test_selective_only_owner_even_with_grants_or_hub_admin(w):
    w.reg.add(WS, "selective", owner=w.ann, name="Ann’s work")
    for person, role, via in [(w.ann, "owner", [{"kind": "owner"}]), (w.admin, None, [])]:
        status, body, _ = reach(w, person)
        assert status == 200
        row = body["workspaces"][0]
        assert row["role"] == role and row["via"] == via
        assert row["owner"]["user_id"] == w.ann
        assert row["note"] == "personal workspace: only its owner"
    w.reg.add(WS, "selective")
    assert reach(w, w.admin)[1]["workspaces"][0]["role"] is None


def test_unknown_user(w):
    assert reach(w, "ff" * 16)[:2] == (404, {"error": "unknown_user"})


def test_non_admin_and_anonymous_refused(w):
    assert reach(w, w.ann, w.login(w.ann))[:2] == (403, {"error": "forbidden_role"})
    assert w.call("GET", f"/admin/people/{w.ann}/reach")[:2] == (401, {"error": "no_session"})
