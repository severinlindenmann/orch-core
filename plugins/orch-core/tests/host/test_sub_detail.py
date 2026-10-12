"""H6b, ``sub.open {detail:true}`` (v1.5, cap ``sub_ext/1``, §0.9, §0.8.5): the event items of a live subscription are
the ``ActivityItem`` of ``activity.list``, in bounded frames."""

from __future__ import annotations

import json

import pytest

from orch.host import bus as bus_module
from orch.host import dispatch
from tests.host.dispatch_helpers import err
from tests.host.live_helpers import SESSION, jpos_of, ok, world

GATES = {"plan": {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False}}


@pytest.fixture
def w(tmp_path):
    s = world(tmp_path)
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


def held(s):
    s.bus.hold = True


def released(s):
    s.bus.hold = False
    s.h.loop.call_soon_threadsafe(s.bus.kick)


def feed_by_id(c, **kw):
    out, args = {}, {"limit": 200}
    while True:
        page = ok(c.req("activity.list", args, **kw))["data"]
        out.update({i["id"]: i for i in page["items"]})
        if page["next_page"] is None:
            return out
        args = {**args, "page": page["next_page"]}


def canon(item):
    return json.dumps(item, sort_keys=True)


def drain(conn, sub, upto):
    items, _ = conn.items_until(sub, upto)
    return items


def several_events(w, key):
    """Several kinds of events through the host: ticket events of every flavour and workspace events."""
    c = w.client()
    ok(c.req("ac.add", {"ref": key, "text": "first criterion"}, grant=w.grant, session=SESSION, idem=True))
    ok(c.req("ac.add", {"ref": key, "text": "second criterion"}, grant=w.grant, session=SESSION, idem=True))
    ok(c.req("set", {"ref": key, "pairs": ["title=Renamed"]}, grant=w.grant, session=SESSION, idem=True))
    w.write(key, "a note " + "é" * 700)  # the summary is cut, the data holds a Snip
    w.person_event("settings.changed", set={"claim_ttl_min": 90})
    w.person_event("policy.changed", gates=GATES)
    w.owner_grant()


# --- the cap -------------------------------------------------------------------------------------------------------


def test_sub_ext_1_is_in_welcome_caps_and_detail_is_accepted(w):
    assert "sub_ext/1" in w.client().welcome["caps"]
    c = w.conn()
    sub = ok(c.call("sub.open", {"topics": ["tickets"], "detail": True}))["data"]
    assert sub["sub"].startswith("sub_")
    ok(c.call("sub.open", {"topics": ["tickets"], "detail": False}))
    assert ok(c.call("sub.open", {"topics": ["tickets"]}))["data"]["cursor"]
    assert ok(c.call("sub.close", {"sub": sub["sub"]}))["data"] == {}
    assert err(c.call("sub.open", {"topics": ["tickets"], "detail": "yes"})) == "invalid.input"
    assert err(c.call("sub.open", {"topics": ["tickets"], "detail": 1})) == "invalid.input"


def test_detail_without_sub_ext_is_invalid_input_an_unknown_key(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, "SUB_EXT", False)
    s = world(tmp_path)
    try:
        assert "sub_ext/1" not in s.client().welcome["caps"]
        c = s.conn()
        assert err(c.call("sub.open", {"topics": ["tickets"], "detail": True})) == "invalid.input"
        assert err(c.call("sub.open", {"topics": ["tickets"], "detail": False})) == "invalid.input"
        assert ok(c.call("sub.open", {"topics": ["tickets"]}))["data"]["sub"]  # v1.4 clients are unaffected
    finally:
        s.close()
        s.lab.close()


# --- the item ---------------------------------------------------------------------------------------------------------


def test_a_detail_item_equals_the_activity_list_item_of_the_same_event(w):
    uid, key = w.ticket("original title")
    c, listing = w.conn(), w.client()
    sub = c.open(["tickets", "workspace"])
    plain = w.conn()
    plain_sub = plain.open(["tickets", "workspace"])
    detail = ok(c.call("sub.open", {"topics": ["tickets", "workspace"], "detail": True}))["data"]
    held(w)
    several_events(w, key)
    head = w.service.journal.head
    released(w)
    items = drain(c, detail["sub"], head)
    events = [i for i in items if "gone" not in i and i.get("type") != "needs.changed"]
    assert len(events) >= 7
    feed = feed_by_id(listing)
    for item in events:
        assert canon(item) == canon(feed[item["id"]]), item["id"]
    assert {i["type"] for i in events} >= {"ticket.updated", "log.added", "settings.changed", "policy.changed"}
    titles = {i["title"] for i in events if i["uid"] == uid}
    assert titles == {"Renamed"}  # the title at delivery, not at the event
    summaries = [i["summary"] for i in events if i["type"] == "ticket.updated"]
    assert any("AC1" in s for s in summaries) and any("AC2" in s for s in summaries)
    # the same subscription without detail is the v1.4 item
    drain(c, sub["sub"], head)  # keep the connection tidy
    v14 = drain(plain, plain_sub["sub"], head)
    assert all(set(i) == {"id", "jpos", "log", "key", "uid", "seq", "ws_seq", "type", "at", "actor"} for i in v14)


def test_a_detail_item_equals_the_item_for_a_grant_viewer_too(w):
    uid, key = w.ticket("t")
    c, listing = w.conn(), w.client()
    detail = ok(c.call("sub.open", {"topics": ["tickets", "workspace"], "detail": True}, grant=w.grant))["data"]
    held(w)
    w.write(key, "one")
    w.owner_grant()  # the owner's own grant: delivered to its person
    w.person_event("settings.changed", set={"claim_ttl_min": 91})
    head = w.service.journal.head
    released(w)
    events = drain(c, detail["sub"], head)
    assert [i["type"] for i in events] == ["log.added", "grant.issued", "settings.changed"]
    feed = feed_by_id(listing, grant=w.grant)
    for item in events:
        assert canon(item) == canon(feed[item["id"]])
    assert events[1]["data"]["person"] == w.lab.owner and set(events[1]["data"]) == {
        "grant", "person", "scope", "expires_at",
    }  # fmt: skip


def test_gone_needs_and_visibility_changed_items_under_detail(w):
    uid, key = w.ticket("hide me")
    c, listing = w.conn(), w.client()
    detail = ok(c.call("sub.open", {"topics": ["tickets", "needs"], "detail": True}))["data"]
    w.restrict(uid, [w.quinn.ref])  # hidden from the owner: gone, not the event
    w.restrict(uid, [w.quinn.ref, w.lab.owner])  # visible again: the event as visibility.changed
    head = w.service.journal.head
    items = drain(c, detail["sub"], head)
    gone = [i for i in items if i.get("gone")]
    assert gone and set(gone[0]) == {"id", "jpos", "uid", "gone"}
    assert not [i for i in items if i["id"] == gone[0]["id"].replace(".g", ".e")]
    # (the item keeps its own type; a re-typing is for the v1.4 item only, see the test below)
    (back,) = [i for i in items if i.get("type") == "visibility.changed"]
    assert back["title"] == "hide me" and "data" in back and back["data"] == {"restricted": True}
    assert canon(back) == canon(feed_by_id(listing)[back["id"]])
    assert all(set(i) == {"id", "jpos", "type"} for i in items if i.get("type") == "needs.changed")


def test_a_detail_subscription_never_sees_what_a_plain_one_does_not(w):
    uid, key = w.ticket("secret")
    w.write(key, "hidden note")
    w.restrict(uid, [w.quinn.ref])
    c = w.conn()
    detail = ok(c.call("sub.open", {"topics": ["tickets", "workspace"], "detail": True}))["data"]
    w.owner_grant()
    other, other_key = w.ticket("open")
    head = w.service.journal.head
    items = drain(c, detail["sub"], head)
    assert "secret" not in json.dumps(items) and "hidden note" not in json.dumps(items)
    assert {i["uid"] for i in items if i.get("uid")} == {other}


# --- frames and bounds -----------------------------------------------------------------------------------------------


def test_frames_hold_at_most_64_items_and_one_jpos_stays_whole(w):
    uid, key = w.ticket("busy")
    c = w.conn()
    detail = ok(c.call("sub.open", {"topics": ["tickets"], "detail": True}))["data"]
    held(w)
    for n in range(70):
        w.person_event("log.added", uid, text=f"note {n}")
    head = w.service.journal.head
    released(w)
    seen, frames = [], []
    while not seen or jpos_of(frames[-1]["cursor"]) < head:
        frame = c.evt(detail["sub"])
        frames.append(frame)
        seen.extend(frame["items"])
    assert len(seen) == 70 and all(len(f["items"]) <= 64 for f in frames) and len(frames) >= 2
    assert [i["jpos"] for i in seen] == sorted(i["jpos"] for i in seen)


def test_frames_are_split_by_bytes_within_the_frame_limit(w, monkeypatch):
    uid, key = w.ticket("big")
    c = w.conn()
    detail = ok(c.call("sub.open", {"topics": ["tickets"], "detail": True}))["data"]
    budget = 6_000
    monkeypatch.setattr(bus_module, "FRAME_BUDGET", budget)
    held(w)
    for n in range(12):
        w.person_event("log.added", uid, text=f"{n} " + "x" * 3000)  # an item of ~1.4 KB: a Snip holds 1 KiB
    head = w.service.journal.head
    released(w)
    seen, frames = [], []
    while not seen or jpos_of(frames[-1]["cursor"]) < head:
        frame = c.evt(detail["sub"])
        frames.append(frame)
        seen.extend(frame["items"])
    assert len(seen) == 12 and len(frames) > 2
    for frame in frames:
        size = sum(len(json.dumps(i, separators=(",", ":"), ensure_ascii=False).encode()) + 1 for i in frame["items"])
        assert size <= budget or len(frame["items"]) == 1
        assert len(frame["items"]) <= 64
    assert max(len(json.dumps(i)) for i in seen) > 1024 and all(i["data"]["text_cut"] for i in seen)
    assert bus_module.FRAME_BUDGET < 2 * 1024 * 1024  # and the real budget is within the §0.2 frame limit


def test_the_real_frame_budget_is_within_the_frame_limit():
    from orch.host import frames

    assert 0 < bus_module.FRAME_BUDGET < frames.MAX_FRAME and bus_module.EVT_ITEMS == 64


def test_a_detail_subscription_overflows_at_1_mib_and_a_plain_one_at_256_kib(w):
    assert bus_module.MAX_DETAIL_BYTES == 1024 * 1024 and bus_module.MAX_BYTES == 256 * 1024
    assert w.bus.max_detail_bytes == 1024 * 1024 and w.bus.max_bytes == 256 * 1024
    uid, key = w.ticket("queue")
    w.bus.max_bytes, w.bus.max_detail_bytes = 4_000, 12_000  # scaled down: the same rule, faster
    c = w.conn()
    plain = c.open(["tickets"])
    detail = ok(c.call("sub.open", {"topics": ["tickets"], "detail": True}))["data"]
    held(w)
    for n in range(4):
        w.person_event("log.added", uid, text=f"{n} " + "y" * 900)  # ~1.4 KB per detail item, ~250 B per plain one
    ends = {}
    for _ in range(40):
        c._pump(0.05)
        for f in list(c.evts):
            if "end" in f:
                ends[f["sub"]] = f["end"]
                c.evts.remove(f)
    assert plain["sub"] not in ends and detail["sub"] not in ends  # 4 items fit both limits (1.4 KB x 4 < 12 000)
    for n in range(6):
        w.person_event("log.added", uid, text=f"{n} " + "y" * 900)
    for _ in range(60):
        c._pump(0.05)
        for f in list(c.evts):
            if "end" in f:
                ends[f["sub"]] = f["end"]
                c.evts.remove(f)
        if detail["sub"] in ends:
            break
    assert ends.get(detail["sub"]) == "overflow"  # ~14 KB of detail items > 12 000
    assert plain["sub"] not in ends  # ~2.5 KB of plain items < 4 000


# --- H6b fix round: one derivation, the item of a ticket that just became visible ---


def test_the_journal_detail_equals_the_activity_item_for_every_event_of_a_mixed_log(w):
    """``Entry.detail`` (journal ``_step``) and ``reads_feed.derive`` (cold) are the same item, event by event."""
    from types import SimpleNamespace

    from orch.host import reads_feed

    uid, key = w.ticket("mixed")
    several_events(w, key)
    other, other_key = w.ticket("closing")
    w.write(other_key, "last words")
    w.person_event("ticket.closed", other, resolution="other")
    w.person_event("grant.revoked", grant=w.lab.ws.grant_id)
    w.person_event("ticket.reopened", other)
    w.restrict(uid, [w.quinn.ref, w.lab.owner])
    w.grant_for(w.quinn)

    def derive_all():
        store = w.service.store
        store.load_all()
        return reads_feed.derive(SimpleNamespace(workspace=store.state.workspace, store=store), 0)

    total, cold = w.h.run(w.service.call(derive_all))
    window = list(w.service.journal.window)
    assert total == len(cold) == len(window) > 15
    types_seen = {e.type for e in window}
    assert {"grant.issued", "grant.revoked", "ticket.updated", "ticket.closed"} <= types_seen
    for entry, derived in zip(window, cold, strict=True):
        assert reads_feed._from_journal(entry) == derived, (entry.jpos, entry.type)
    assert any(e.grant_person for e in window if e.type == "grant.revoked")


def test_a_detail_item_of_a_ticket_that_became_visible_is_the_activity_item_of_the_same_id(w):
    """Review finding 6: the retype to ``visibility.changed`` is for the v1.4 item only; the detail item keeps its own
    type and data, exactly the ``activity.list`` item."""
    from types import SimpleNamespace

    from orch.host.bus import Bus, Feed
    from orch.host.journal import Entry

    detail = {
        "id": "7.e", "jpos": 7, "log": "t1", "key": "WS-1", "uid": "t1", "title": None, "seq": 3, "ws_seq": 7,
        "type": "people.changed", "at": "2026-01-01T00:00:00Z", "actor": {"kind": "human", "id": "p"},
        "summary": "owner added", "data": {"role": "owner", "add": ["p"], "remove": []},
    }  # fmt: skip
    entry = Entry(
        7, "t1", "WS-1", 3, 7, "people.changed", "2026-01-01T00:00:00Z", "human", "p",
        before=frozenset({"someone"}), after="workspace", detail=detail,
    )  # fmt: skip
    feed = Feed("ws")
    feed.vis, feed.titles = {"t1": "workspace"}, {"t1": "now"}
    bus = Bus.__new__(Bus)
    for is_detail in (True, False):
        sub = SimpleNamespace(detail=is_detail, person="p", topics=frozenset({"tickets"}), tickets=frozenset())
        (item,) = bus.items(sub, feed, entry, False)
        if is_detail:
            assert item["type"] == "people.changed" and item["data"] == detail["data"] and item["title"] == "now"
            assert item["data"] is not detail["data"] and item["actor"] is not detail["actor"]  # copies
        else:
            assert item["type"] == "visibility.changed" and set(item) == set(entry.item())  # v1.4 keeps H6a's retyping
