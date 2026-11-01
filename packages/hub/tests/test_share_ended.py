# ruff: noqa: F811
"""B08: durable, private withdrawal notices contain only key and title."""

import sqlite3

import pytest
from conftest import NOW
from shares_helpers import IT1, OWNER, audit_records, shub  # noqa: F401
from test_shares_audience import BEN, CARL, access, ticket_body
from webapi_helpers import WS_SEL, Web

from orch_hub.activity import ActivityLog
from orch_hub.backup import guard
from orch_hub.contract import vectorgen as G
from orch_hub.contract import vectorgen_p4 as V4
from orch_hub.contract.phase4 import render_text
from orch_hub.shares import ShareStore

AUD = {"users": sorted([BEN, OWNER]), "groups": ["team-a"]}


def test_revoke_persists_only_metadata_for_users_and_current_team_minus_owner(shub, tmp_path):
    acc = access()
    path = str(tmp_path / "shares.db")
    h = shub(access=acc, store_path=path)
    c = h.join()
    content = {**V4.TICKET_CONTENT, "description": "PRIVATE DESCRIPTION"}
    c.request("share_put", ticket_body(content=content, audience=AUD))
    before = len(audit_records(h))
    c.request("share_revoke", {"item_id": IT1})
    assert len(audit_records(h)) == before + 1  # existing revoke only, no notice event
    assert h.share_store.ended_for(OWNER) == []
    for reader in (BEN, CARL):
        assert h.share_store.ended_for(reader) == [
            {
                "workspace_id": G.WS_A,
                "item_id": IT1,
                "key": content["key"],
                "title": content["title"],
                "by": "Anna",
                "at_ms": NOW,
            }
        ]
    db = sqlite3.connect(path)
    assert [r[1] for r in db.execute("PRAGMA table_info(share_ended)")] == [
        "reader",
        "ws",
        "item_id",
        "key",
        "title",
        "by_user",
        "at_ms",
    ]
    assert "PRIVATE DESCRIPTION" not in repr(db.execute("SELECT * FROM share_ended").fetchall())
    db.close()
    reopened = ShareStore(path)
    assert reopened.ended_for(BEN) == h.share_store.ended_for(BEN)
    reopened.dismiss(BEN, G.WS_A, IT1)
    assert not reopened.ended_for(BEN) and reopened.ended_for(CARL)
    reopened.close()
    acc.close()


def test_replace_reshare_and_timer_retention(shub):
    acc = access()
    h = shub(access=acc)
    c = h.join()
    c.request("share_put", ticket_body(audience=AUD))
    c.request("share_put", ticket_body(version=2, audience={"users": [], "groups": ["team-a"]}))
    assert h.share_store.ended_for(BEN) and not h.share_store.ended_for(CARL)
    c.request("share_put", ticket_body(version=3, audience=AUD))
    assert not h.share_store.ended_for(BEN)
    c.request("share_revoke", {"item_id": IT1})
    h.svc._now = lambda: NOW + 30 * 86400000
    h.svc.retry_pending()
    assert h.share_store.ended_for(BEN)
    h.svc._now = lambda: NOW + 30 * 86400000 + 1
    h.svc.retry_pending()
    assert not h.share_store.ended_for(BEN)
    acc.close()


def test_unbind_notices(shub):
    acc = access()
    h = shub(access=acc)
    c = h.join()
    c.request("share_put", ticket_body(audience=AUD))
    c.request("binding_put", {"object": G.unbound(seq=1)})
    assert h.share_store.ended_for(BEN)[0]["by"] == "Anna"
    acc.close()


def test_notice_failure_rolls_back_item_change():
    store = ShareStore()
    store.put("w", "i", b"sealed", 1, "hash", 0)
    store._db.execute("CREATE TRIGGER fail_notice BEFORE INSERT ON share_ended BEGIN SELECT RAISE(ABORT, 'fail'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.revoke("w", "i", 1, ended=[("b", "w", "i", "key", "title", "Ann", 1)])
    assert store.get("w", "i")[0] == b"sealed"
    assert not store.ended_for("b")
    store.close()


def test_api_is_private_dismiss_is_scoped_and_csrf_protected(tmp_path):
    w = Web(tmp_path)
    try:
        activity = w.api.d.activity = ActivityLog(":memory:")
        ben = w.env.user("ben")
        store = w.shares.store
        with store._db:
            store._write_ended([(u, WS_SEL, IT1, "PW-1", "<b>Invoice</b>", "ann", NOW) for u in (ben, w.ann)])
        bt, at = w.login(ben), w.login(w.ann)
        route = "/me/shared/ended/dismiss"
        payload = {"workspace_id": WS_SEL, "item_id": IT1}
        assert w.call("POST", route, payload)[0] == 401
        assert w.call("POST", route, payload, token=bt, csrf=False)[0] == 403
        admin_token = w.login(w.admin)
        before = [w.call("GET", r, token=admin_token)[1] for r in ("/admin/activity", "/admin/audit")]
        assert w.call("POST", route, payload, token=at)[0] == 200
        assert w.call("GET", "/me/shared", token=at)[1]["ended"] == []
        row = w.call("GET", "/me/shared", token=bt)[1]["ended"][0]
        assert row["item_id"] == IT1 and row["title"] == render_text("<b>Invoice</b>")
        assert w.call("POST", route, payload, token=bt)[0] == 200
        assert w.call("GET", "/me/shared", token=bt)[1]["ended"] == []
        after = [w.call("GET", r, token=admin_token)[1] for r in ("/admin/activity", "/admin/audit")]
        assert after == before
        activity.close()
    finally:
        w.close()


def test_api_newest_first_max_twenty(tmp_path):
    w = Web(tmp_path)
    try:
        with w.shares.store._db:
            w.shares.store._write_ended([(w.ann, WS_SEL, f"{i:032x}", "PW-1", "Invoice", "ben", i) for i in range(25)])
        ended = w.call("GET", "/me/shared", token=w.login(w.ann))[1]["ended"]
        assert [r["at_ms"] for r in ended] == list(range(24, 4, -1))
    finally:
        w.close()


def test_restore_notices_take_live_only_and_fresh_restore_clears_backup(tmp_path):
    new, live = tmp_path / "new", tmp_path / "live"
    for directory, reader in ((new, "old"), (live, "live")):
        directory.mkdir()
        store = ShareStore(str(directory / "shares.db"))
        with store._db:
            store._write_ended([(reader, "ws", "it", "key", "title", "Ann", 1)])
        store.close()
    guard.share_ended(new, live)
    store = ShareStore(str(new / "shares.db"))
    assert not store.ended_for("old") and store.ended_for("live")
    store.close()
    guard.share_ended(new, None)
    store = ShareStore(str(new / "shares.db"))
    assert not store.ended_for("old") and not store.ended_for("live")
    store.close()
