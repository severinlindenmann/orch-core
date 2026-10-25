"""H6a, live updates (§0.9, D-P2-12, F11): the journal, ``sub.*``, revalidation, cursors, end reasons, ``wait`` on the
bus. Each acceptance item of the H6a row in §8 has a test here that fails without the code it names."""

from __future__ import annotations

import asyncio
import os

import pytest

from orch.host.journal import peek_cursor
from tests.host.dispatch_helpers import err
from tests.host.live_helpers import SESSION, World, jpos_of, ok, principal_of, world
from tests.host.test_dispatch_matrix import as_agent_class


@pytest.fixture
def w(tmp_path):
    s = world(tmp_path)
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


def released(s: World) -> None:
    s.bus.hold = False
    s.h.loop.call_soon_threadsafe(s.bus.kick)


def held(s: World) -> None:
    s.bus.hold = True


# --- basics -----------------------------------------------------------------------------------------------------------


def test_a_subscription_delivers_ticket_events_with_ids_and_a_cursor(w):
    uid, key = w.ticket()
    c = w.conn()
    sub = c.open(["tickets"])
    assert sub["sub"].startswith("sub_") and jpos_of(sub["cursor"]) > 0
    w.write(key, "hello")
    frame = c.evt(sub["sub"])
    (item,) = frame["items"]
    assert item["type"] == "log.added" and item["uid"] == uid and item["key"] == key and item["log"] == uid
    assert item["id"] == f"{item['jpos']}.e" and item["jpos"] == jpos_of(sub["cursor"]) + 1
    assert jpos_of(frame["cursor"]) == item["jpos"]
    assert item["actor"]["kind"] == "agent" and set(item) == {
        "id", "jpos", "log", "key", "uid", "seq", "ws_seq", "type", "at", "actor"
    }  # fmt: skip


def test_sub_open_refuses_detail_and_bad_topics_and_close_ends_with_closed(w):
    c = w.conn()
    assert err(c.call("sub.open", {"topics": ["tickets"], "detail": "yes"})) == "invalid.input"  # sub_ext/1: a bool
    assert err(c.call("sub.open", {"topics": ["everything"]})) == "invalid.input"
    assert err(c.call("sub.open", {"topics": ["ticket:DEMO-9999"]})) == "not_found"
    assert err(c.call("sub.open", {"topics": ["tickets"], "after": "c1.bm9wZQ"})) == "sub.reset"
    sub = c.open(["needs"])
    rid = c.send("sub.close", {"sub": sub["sub"]})
    assert ok(c.answer(rid))["data"] == {}
    end = c.evt(sub["sub"])
    assert end["end"] == "closed" and end["items"] == []
    assert c.frames.index(end) > next(i for i, f in enumerate(c.frames) if f.get("id") == rid)  # res, then end


def test_at_most_eight_subscriptions_per_connection(w):
    c = w.conn()
    for _ in range(8):
        c.open(["needs"])
    assert err(c.call("sub.open", {"topics": ["needs"]})) == "quota.host"


# --- a write's res before its evt -------------------------------------------------------------------------------------


def test_a_writes_res_comes_before_the_evt_with_its_own_events(w):
    _, key = w.ticket()
    c = w.conn()
    sub = c.open(["tickets"])
    for n in range(3):
        rid = c.send("log", {"ref": key, "text": f"mine {n}"}, grant=w.grant, session=SESSION, idem=True)
        res = ok(c.answer(rid))
        frame = c.evt(sub["sub"])
        assert [i["seq"] for i in frame["items"]] == [res["seq"]]
        res_at = next(i for i, f in enumerate(c.frames) if f.get("id") == rid)
        assert res_at < c.frames.index(frame), "the evt with the write's own event came before its res"
        w.lab.ws.tick()


# --- revalidation: a revoked grant and a removed person stop delivery, queued items included -------------------------


def test_a_revoked_grant_ends_the_subscription_and_drops_its_queued_items(w):
    _, key = w.ticket()
    c = w.conn()
    sub = c.open(["tickets", "workspace"], grant=w.grant)  # column G: the owner's agent grant
    held(w)
    w.write(key, "queued 1")
    w.write(key, "queued 2")  # both are queued for the subscription, not delivered
    w.person_event("grant.revoked", grant=w.lab.ws.grant_id)
    released(w)
    end = c.evt(sub["sub"])
    assert end["end"] == "grant.expired" and end["items"] == []
    c.quiet(sub["sub"])


def test_a_removed_person_ends_the_subscription_and_drops_its_queued_items(tmp_path):
    s = world(tmp_path, map_to="quinn")  # column P: quinn, a member
    try:
        _, key = s.ticket()
        c = s.conn()
        sub = c.open(["tickets"])
        s.write(key, "seen")
        assert [i["type"] for i in c.evt(sub["sub"])["items"]] == ["log.added"]
        held(s)
        s.write(key, "queued")
        s.person_event("member.removed", person=s.quinn.ref)
        released(s)
        end = c.evt(sub["sub"])
        assert end["end"] == "person.removed" and end["items"] == []
    finally:
        s.close()
        s.lab.close()


# --- resume with no gap after a host restart --------------------------------------------------------------------------


def test_resume_after_a_host_restart_loses_and_repeats_nothing(w):
    _, key = w.ticket()
    c = w.conn()
    sub = c.open(["tickets"])
    first = w.write(key, "before")
    frame = c.evt(sub["sub"])
    (seen,) = frame["items"]
    assert seen["seq"] == first["seq"]
    stored = frame["cursor"]
    w.close()  # the host stops; one event lands while it is down (an older CLI), then it starts again
    w.lab.ws.tick()
    w.append(lambda store: (store.ticket(w.lab.ws.uid(key)), w.lab.ws.log(w.lab.ws.uid(key), "while down")))
    s2 = World.__new__(World)
    s2.__dict__.update(w.__dict__)
    s2.h = w.lab.host(clock=lambda: w.lab.ws.clock[0])
    s2.h.__enter__()
    s2.service, s2.bus, s2.clients = s2.h.host.services[w.wid], s2.h.host.bus, []
    try:
        for stale in ("_writer", "_maker"):  # clients of the stopped host
            s2.__dict__.pop(stale, None)
        later = s2.write(key, "after")
        c2 = s2.conn()
        resumed = c2.open(["tickets"], after=stored)
        assert resumed["cursor"] == stored
        items, _ = c2.items_until(resumed["sub"], jpos_of(stored) + 2)
        assert [i["jpos"] for i in items] == [seen["jpos"] + 1, seen["jpos"] + 2]  # no gap, nothing again
        assert [i["type"] for i in items] == ["log.added", "log.added"]
        assert items[-1]["seq"] == later["seq"]
    finally:
        s2.close()


# --- overflow, then snapshot, then cursor -----------------------------------------------------------------------------


def test_overflow_then_a_snapshot_then_its_cursor_loses_and_repeats_nothing(w):
    _, key = w.ticket()
    c = w.conn()
    sub = c.open(["tickets"])
    w.bus.max_items = 3
    held(w)  # a reader that does not keep up: its queue grows past the limit
    for n in range(4):
        w.write(key, f"backlog {n}")
    w.h.loop.call_soon_threadsafe(w.bus.kick)
    end = c.evt(sub["sub"])
    assert end["end"] == "overflow" and end["items"] == []
    released(w)
    snapshot = w.snapshot_cursor(principal_of())  # the client re-reads its state; the read gives this cursor
    after = w.write(key, "after the snapshot")
    again = c.open(["tickets"], after=snapshot)
    items, _ = c.items_until(again["sub"], jpos_of(snapshot) + 1)
    assert [(i["type"], i["seq"]) for i in items] == [("log.added", after["seq"])]  # nothing older, nothing lost


def test_a_slow_reader_that_lags_too_long_overflows(w):
    _, key = w.ticket()
    c = w.conn()
    sub = c.open(["tickets"])
    w.bus.max_lag = 0.0
    session = next(s for s in w.h.host.servers[w.wid][0].sessions if s.subscriptions)
    real = w.bus._room
    w.bus._room = lambda sess: False if sess is session else real(sess)  # its write queue stays full
    w.write(key, "stuck")
    w.h.loop.call_soon_threadsafe(w.bus.kick)
    w.bus._room = real
    assert c.evt(sub["sub"])["end"] == "overflow"


# --- the journal is rebuilt after a foreign append (F11) --------------------------------------------------------------


def test_a_foreign_append_rebuilds_the_journal_and_resets_every_subscription(w, caplog):
    uid, key = w.ticket()
    c = w.conn()
    sub = c.open(["tickets"])
    before = w.snapshot_cursor(principal_of())
    w.lab.ws.tick()
    w.append(lambda store: (store.ticket(uid), w.lab.ws.log(uid, "foreign")))  # another writer: an older CLI
    with caplog.at_level("WARNING", logger="orch.host.workspace"):
        w.check_journals()  # the 60 s check
    end = c.evt(sub["sub"])
    assert end["end"] == "reset" and end["items"] == []
    assert any("foreign append" in r.message for r in caplog.records)  # one audit line
    assert w.service.state == "ok"  # degraded only while the rebuild ran
    again = c.open(["tickets"], after=before)  # the rebuilt journal holds the foreign event at the next jpos
    items, _ = c.items_until(again["sub"], jpos_of(before) + 1)
    assert [(i["type"], i["uid"], i["jpos"]) for i in items] == [("log.added", uid, jpos_of(before) + 1)]
    later = w.write(key, "host again")
    (item,) = c.evt(again["sub"])["items"]
    assert item["seq"] == later["seq"] and item["jpos"] == jpos_of(before) + 2


def test_a_foreign_append_is_found_before_the_next_append(w):
    uid, key = w.ticket()
    c = w.conn()
    sub = c.open(["tickets"])
    w.lab.ws.tick()
    w.append(lambda store: (store.ticket(uid), w.lab.ws.log(uid, "foreign")))
    w.write(key, "the host appends")  # before_append sees the foreign line first
    assert c.evt(sub["sub"])["end"] == "reset"


# --- item identity: one jpos, three items -----------------------------------------------------------------------------


def test_one_jpos_yields_up_to_three_ids_event_and_needs_or_gone_and_needs(q):
    """§0.9 (amended): one jpos gives ``.e``+``.n`` to a viewer who still sees the ticket and ``.g``+``.n`` to one who
    lost it, never ``.e`` and ``.g`` together. Two subscriptions on the same jpos, one evt each."""
    uid, key = q.ticket()
    ok(
        q.client().req(
            "ask", {"ref": key, "text": "which one?", "text_question": True}, grant=q.grant, session=SESSION, idem=True
        )
    )
    stays, loses = q.conn(), q.conn()
    a = stays.open(["tickets", "needs"])  # P: quinn, who stays on the list
    b = loses.open(["tickets", "needs"], grant=q.grant)  # G: the owner, who is taken off it
    q.restrict(uid, [q.quinn.ref])  # the ticket has an open question: its needs change who may see them
    fa, fb = stays.evt(a["sub"]), loses.evt(b["sub"])
    j = fa["items"][0]["jpos"]
    assert [i["id"] for i in fa["items"]] == [f"{j}.e", f"{j}.n"]
    assert fa["items"][0]["type"] == "visibility.changed" and fa["items"][0]["uid"] == uid
    assert fb["items"] == [
        {"id": f"{j}.g", "jpos": j, "uid": uid, "gone": True},  # only the uid it knew: not the event that hid it
        {"id": f"{j}.n", "jpos": j, "type": "needs.changed"},
    ]
    q.restrict(uid, [q.quinn.ref])  # a later event on it
    assert [i["id"] for i in stays.evt(a["sub"])["items"]] == [f"{j + 1}.e"]
    loses.quiet(b["sub"])  # every later item about it is filtered, and no second gone


def test_a_replay_never_shows_the_event_that_hid_a_ticket(w):
    uid, _ = w.ticket()
    w.restrict(uid, [w.quinn.ref])  # hidden from the owner before any subscription
    c = w.conn()
    sub = c.open(["tickets"], grant=w.grant)
    start = w.h.run(
        w.service.call(lambda: w.service.journal.cursor(principal_of(grant=w.grant), w.service.journal.floor))
    )
    again = c.open(["tickets"], after=start, grant=w.grant)
    items, _ = c.items_until(again["sub"], jpos_of(sub["cursor"]))
    assert [i for i in items if i.get("uid") == uid and "gone" not in i] == []  # at most a gone, never the event


def test_a_cursor_the_host_did_not_issue_is_sub_reset(w):
    from orch.host.journal import Cursors

    c = w.conn()
    real = c.open(["tickets"])["cursor"]
    assert ok(c.call("sub.open", {"topics": ["tickets"], "after": real}))  # the host's own: accepted
    body = peek_cursor(real)  # readable, so a client could make one for itself ...
    forged = Cursors(b"k" * 32).encode(body["w"], principal_of(), body["e"], body["j"], body["r"])
    assert peek_cursor(forged) == body
    assert err(c.call("sub.open", {"topics": ["tickets"], "after": forged})) == "sub.reset"  # ... but not forge it
    head, _, mac = real.rpartition(".")
    flipped = head + "." + ("A" if mac[0] != "A" else "B") + mac[1:]
    assert err(c.call("sub.open", {"topics": ["tickets"], "after": flipped})) == "sub.reset"
    assert err(c.call("sub.open", {"topics": ["tickets"], "after": head + ".éé"})) == "sub.reset"  # not internal


def test_a_cursor_whose_entry_moved_after_a_rebuild_is_sub_reset(w):
    import dataclasses

    _, key = w.ticket()
    w.write(key, "one")
    c = w.conn()
    stored = c.open(["tickets"])["cursor"]
    j = jpos_of(stored)

    def shift() -> None:  # as if a rebuild found another event at that position (a foreign event sorting before)
        journal = w.service.journal
        i = j - journal.window[0].jpos
        journal.window[i] = dataclasses.replace(journal.window[i], ref="000000000000")

    w.h.run(w.service.call(shift))
    assert err(c.call("sub.open", {"topics": ["tickets"], "after": stored})) == "sub.reset"


# --- a fully filtered jpos still advances the cursor ------------------------------------------------------------------


def test_a_fully_filtered_jpos_still_advances_the_cursor(w):
    _, open_key = w.ticket("open")
    hidden_uid, _ = w.ticket("hidden")
    w.restrict(hidden_uid, [w.quinn.ref])
    c = w.conn()
    sub = c.open(["tickets"], grant=w.grant)  # the owner: does not see the hidden ticket
    held(w)
    shown = w.write(open_key, "visible")
    w.restrict(hidden_uid, [w.quinn.ref])  # two events on a ticket the viewer does not see
    w.restrict(hidden_uid, [w.quinn.ref])
    last = w.snapshot_cursor(principal_of(grant=w.grant))
    released(w)
    frame = c.evt(sub["sub"])
    assert [i["seq"] for i in frame["items"]] == [shown["seq"]]
    assert frame["cursor"] == last  # after the two filtered positions, not after the visible one


# --- gone only to a viewer who saw the ticket -------------------------------------------------------------------------


def test_gone_is_sent_only_to_a_viewer_who_saw_the_ticket(w):
    never_uid, _ = w.ticket("never seen")
    w.restrict(never_uid, [w.quinn.ref])
    seen_uid, _ = w.ticket("seen")
    _, open_key = w.ticket("open")
    c = w.conn()
    sub = c.open(["tickets"], grant=w.grant)  # the owner
    held(w)
    w.restrict(never_uid, [w.quinn.ref])  # invisible to the viewer before and after: no item, and no gone
    w.restrict(seen_uid, [w.quinn.ref])  # visible at the previous jpos, invisible now: gone
    w.restrict(never_uid, [w.quinn.ref, w.lab.owner])  # becomes visible: its event as visibility.changed
    w.restrict(never_uid, [w.quinn.ref])  # and is hidden again: the viewer saw it at the previous jpos, so gone
    marker = w.write(open_key, "marker")
    released(w)
    items, _ = c.items_until(sub["sub"], jpos_of(w.snapshot_cursor(principal_of(grant=w.grant))))
    got = [(i["uid"], i.get("type"), i.get("gone", False)) for i in items]
    assert got == [
        (seen_uid, None, True),  # only the gone, never the event that hid it
        # the event that showed it is dropped at delivery (the ticket is invisible now); the one that hid it gives a
        # gone, because the viewer saw the ticket at the previous jpos
        (never_uid, None, True),
        (w.lab.ws.uid(open_key), "log.added", False),
    ]
    assert items[-1]["seq"] == marker["seq"]


# --- v1.5 (blocker): the workspace topic's per-type rule, at delivery, with withheld positions advancing the cursor ---


def test_no_person_gets_no_workspace_log_item_and_the_withheld_positions_advance_the_cursor(tmp_path):
    s = world(tmp_path)
    try:
        _, key = s.ticket()
        as_agent_class(s)  # an agent-class UID without a grant: no-person (column N)
        c = s.conn()
        assert ok(c.call("whoami"))["data"]["person"] is None
        sub = c.open(["workspace", "tickets"])
        held(s)
        s.owner_grant()  # grant.issued
        shown = s.write(key, "a workspace ticket: visible to no-person")
        s.person_event("role.changed", person=s.quinn.ref, role="viewer")  # a member/role event
        last = s.snapshot_cursor(principal_of())
        released(s)
        frame = c.evt(sub["sub"])
        assert [(i["log"], i["seq"]) for i in frame["items"]] == [(s.lab.ws.uid(key), shown["seq"])]
        assert frame["cursor"] == last  # the withheld workspace positions still advance it
    finally:
        s.close()
        s.lab.close()


@pytest.fixture
def q(tmp_path):
    """The UID mapped to quinn, a member (column P, not an owner)."""
    s = world(tmp_path, map_to="quinn")
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


def kinds(items):
    return [i["type"] for i in items]


def test_a_non_owner_gets_no_grant_item_of_another_person_only_its_own(q):
    c = q.conn()
    sub = c.open(["workspace"])
    held(q)
    q.owner_grant()  # the owner's grant: not quinn's to see
    q.grant_for(q.quinn)  # quinn's own grant: delivered
    q.person_event("grant.revoked", grant=q.lab.ws.grant_id)  # the owner's grant again
    q.person_event("settings.changed", set={"claim_ttl_min": 90})  # a type every person gets
    last = q.snapshot_cursor(principal_of())
    released(q)
    items, cursor = c.items_until(sub["sub"], jpos_of(last))
    assert kinds(items) == ["grant.issued", "settings.changed"]
    assert cursor == last


def test_the_grant_rule_is_rechecked_at_delivery_after_a_role_change(q):
    c = q.conn()
    q.person_event("role.changed", person=q.quinn.ref, role="owner")  # quinn is a person-class owner now
    sub = c.open(["workspace"])
    q.owner_grant()
    items, _ = c.items_until(sub["sub"], jpos_of(q.snapshot_cursor(principal_of())))
    assert kinds(items) == ["grant.issued"]  # an owner sees every grant
    held(q)
    q.owner_grant()  # queued while quinn is an owner ...
    q.person_event("role.changed", person=q.quinn.ref, role="member")  # ... and judged at delivery, as a member
    last = q.snapshot_cursor(principal_of())
    released(q)
    items, cursor = c.items_until(sub["sub"], jpos_of(last))
    assert kinds(items) == ["role.changed"] and cursor == last  # withheld, and its position still passed
    held(q)
    q.owner_grant()  # queued while quinn is a member ...
    q.person_event("role.changed", person=q.quinn.ref, role="owner")  # ... delivered to the owner quinn is now
    last = q.snapshot_cursor(principal_of())
    released(q)
    items, _ = c.items_until(sub["sub"], jpos_of(last))
    assert kinds(items) == ["grant.issued", "role.changed"]


def test_an_owner_acting_through_a_grant_is_not_a_person_class_owner(w):
    """The grant rule's owner is a person-class owner (column P); G for the owner sees only the owner's grants."""
    c = w.conn()
    sub = c.open(["workspace"], grant=w.grant)
    w.grant_for(w.quinn)  # quinn's grant: not the owner's own
    w.owner_grant()
    items, _ = c.items_until(sub["sub"], jpos_of(w.snapshot_cursor(principal_of(grant=w.grant))))
    assert kinds(items) == ["grant.issued"] and items[0]["actor"]["id"] == w.lab.owner
    p = w.conn()  # the same owner, person class: every grant
    sub = p.open(["workspace"], after=w.snapshot_cursor(principal_of()))
    w.grant_for(w.quinn)
    items, _ = p.items_until(sub["sub"], jpos_of(w.snapshot_cursor(principal_of())))
    assert kinds(items) == ["grant.issued"] and items[0]["actor"]["id"] == w.quinn.ref


# --- wait on the bus --------------------------------------------------------------------------------------------------


def answer(s, uid):
    def make(ws):
        q = ws.store.ticket(uid).questions[0]
        return ws.person_event(ws.owner, uid, "question.answered", question=q.id, hash=q.hash, text="this one"), uid

    return s.host_append(make)


def wait_looks(s, monkeypatch):
    """Count the worker jobs (looks) of ``wait`` requests and the git observes they run."""
    backend = s.h.host.servers[s.wid][0].handler.backend
    git = s.h.run(s.service.call(s.service.capabilities)).git
    looks, observes = [], []
    serve, observe = backend.serve, type(git).observe

    def counting_serve(call, step=None):
        if call.frame["op"] == "wait":
            looks.append(step)
        return serve(call, step)

    def counting_observe(self, *a, **k):
        observes.append(a)
        return observe(self, *a, **k)

    backend.serve = counting_serve
    monkeypatch.setattr(type(git), "observe", counting_observe)
    return looks, observes


def test_wait_parks_on_the_bus_without_holding_the_worker_and_wakes_on_the_decision(w, monkeypatch):
    uid, key = w.ticket()
    ok(
        w.client().req(
            "ask", {"ref": key, "text": "which?", "text_question": True}, grant=w.grant, session=SESSION, idem=True
        )
    )
    looks, observes = wait_looks(w, monkeypatch)
    c = w.conn()
    rid = c.send("wait", {"ref": key, "timeout": 60}, grant=w.grant, session=SESSION)
    other = w.conn()
    for _ in range(3):  # the workspace worker is free while the wait is parked
        assert ok(other.call("list"))["exit"] == 0
    for n in range(3):
        w.write(key, f"not a decision {n}")  # entries that cannot answer a wait: no new look
    other_uid, _ = w.ticket("another ticket")  # nor entries of another ticket
    assert ok(other.call("status"))["exit"] == 0
    assert len(looks) == 1
    answer(w, uid)
    res = ok(c.answer(rid))
    assert res["data"]["kind"] == "answered" and res["key"] == key
    assert len(looks) == 2 and len(observes) == 1  # one look per answerable entry; git observed on the first only


def test_a_parked_wait_wakes_on_a_workspace_revoke(q, monkeypatch):
    """quinn waits through quinn's own grant; revoking it wakes the wait at once, and the next look re-authorizes it
    (here as no-person, which may still wait on a workspace ticket)."""
    _, key = q.ticket()
    grant = q.grant_for(q.quinn)
    looks, _ = wait_looks(q, monkeypatch)
    c = q.conn()
    c.send("wait", {"ref": key, "timeout": 60}, grant=grant, session=SESSION)
    c.quiet(wait=0.3)
    assert len(looks) == 1
    q.person_event("settings.changed", set={"claim_ttl_min": 90})  # a workspace entry that cannot refuse it
    c.quiet(wait=0.3)
    assert len(looks) == 1
    q.person_event("grant.revoked", who=q.quinn, grant=grant.partition(".")[0])
    c.quiet(wait=0.5)
    assert len(looks) == 2


def test_wait_times_out_by_its_own_timeout(w):
    _, key = w.ticket()
    c = w.conn()
    res = ok(c.call("wait", {"ref": key, "timeout": 1}, grant=w.grant, session=SESSION))
    assert res["data"]["kind"] == "timeout"
    assert err(c.call("wait", {"ref": key, "timeout": 1, "strict_timeout": True}, grant=w.grant)) == "wait.timeout"


def test_a_drain_answers_a_parked_wait_and_ends_subscriptions_with_host_restarting(w):
    _, key = w.ticket()
    c = w.conn()
    sub = c.open(["tickets"])
    rid = c.send("wait", {"ref": key, "timeout": 60}, grant=w.grant, session=SESSION)
    c.quiet(wait=0.3)
    w.h.loop.call_soon_threadsafe(w.h.host.begin_drain)
    assert err(c.answer(rid)) == "host.restarting"
    assert c.evt(sub["sub"])["end"] == "host.restarting"


def test_a_closed_connection_frees_its_subscriptions(w):
    import asyncio

    c = w.conn()
    c.open(["tickets"])
    assert len(w.bus.subs) == 1
    c.client.close()  # the session's close hook ends them
    w.h.run(asyncio.wait_for(w.bus.emptied(), 5))
    assert not w.bus.subs


# --- review round: bounded work on the loop and the worker ------------------------------------------------------------


def test_a_blocked_subscription_renders_each_pending_entry_once(w, monkeypatch):
    """The overflow check counts the backlog incrementally: flushes while blocked do not re-render it (DoS bound)."""
    _, key = w.ticket()
    c = w.conn()
    sub = c.open(["tickets"])
    rendered = []
    items = w.bus.items
    monkeypatch.setattr(w.bus, "items", lambda s, f, e, o: rendered.append(e.jpos) or items(s, f, e, o))
    held(w)
    for n in range(3):
        w.write(key, f"queued {n}")
    for _ in range(5):  # many flushes while blocked
        w.h.run(asyncio.sleep(0))
        w.h.loop.call_soon_threadsafe(w.bus.flush)
    w.h.run(asyncio.sleep(0.05))
    assert len(rendered) == len(set(rendered)) == 3
    released(w)
    got, _ = c.items_until(sub["sub"], max(rendered))
    assert len(got) == 3


def test_bytes_beyond_what_the_store_wrote_make_the_journal_stale(w, monkeypatch):
    """An append landing between the store's write and its hook is not counted as the host's (F11)."""
    _, key = w.ticket()
    c = w.conn()
    sub = c.open(["tickets"])
    store = w.service.store
    real = store.log_size
    monkeypatch.setattr(store, "log_size", lambda log: (real(log) or 0) - 1)  # the file holds one byte more
    w.write(key, "raced")
    monkeypatch.setattr(store, "log_size", real)
    assert c.evt(sub["sub"])["end"] == "reset"  # rebuilt from the logs after that op


def test_a_failed_rebuild_resets_subscriptions_and_keeps_the_workspace_degraded(w, monkeypatch, caplog):
    from orch.host.journal import Journal

    uid, key = w.ticket()
    c = w.conn()
    sub = c.open(["tickets"])
    builds = []
    real = Journal.build

    def broken(self, store):
        builds.append(1)
        raise TypeError("a model bug")

    monkeypatch.setattr(Journal, "build", broken)
    w.lab.ws.tick()
    w.append(lambda store: (store.ticket(uid), w.lab.ws.log(uid, "foreign")))
    w.check_journals()
    assert c.evt(sub["sub"])["end"] == "reset"
    assert w.service.state == "degraded" and w.service.journal is None
    assert err(c.call("sub.open", {"topics": ["tickets"]})) == "workspace.unavailable"
    assert err(c.call("log", {"ref": key, "text": "x"}, grant=w.grant, session=SESSION, idem=True)) == (
        "workspace.degraded"
    )
    ok(c.call("list"))
    ok(c.call("status"))
    assert len(builds) == 1  # no retry on every op
    monkeypatch.setattr(Journal, "build", real)
    w.check_journals()  # the 60 s check tries again
    assert w.service.state == "ok" and w.service.journal is not None
    assert ok(c.call("sub.open", {"topics": ["tickets"]}))


def test_no_refresh_spin_when_the_snapshot_cannot_be_trusted(w):
    _, key = w.ticket()
    c = w.conn()
    sub = c.open(["tickets"])
    held(w)
    w.write(key, "pending")
    w.h.run(w.service.call(lambda: setattr(w.service, "auth", None)))
    jobs = []
    submit = w.service.submit
    w.service.submit = lambda fn, *a: jobs.append(fn) or submit(fn, *a)
    released(w)
    assert c.evt(sub["sub"])["end"] == "reset"
    w.h.run(asyncio.sleep(0.3))
    assert jobs == []  # ended, not refreshed over and over


def test_a_ticket_a_foreign_writer_created_is_in_the_rebuilt_journal(w):
    w.ticket()
    c = w.conn()
    sub = c.open(["tickets"])
    before = w.service.journal.head
    w.lab.ws.tick()
    w.append(lambda store: w.lab.ws.new_ticket("foreign one"))  # a new tickets/<uid>/ the host never saw
    uid = next(u for u in os.listdir(w.lab.ws.store_dir / "tickets") if u not in w.service.journal.sigs)
    w.check_journals()
    assert c.evt(sub["sub"])["end"] == "reset"
    journal = w.service.journal
    assert journal.head == before + 1 and journal.window[-1].log == uid and journal.window[-1].type == "ticket.created"
    again = c.open(["tickets"], after=sub["cursor"])
    items, _ = c.items_until(again["sub"], before + 1)
    assert [(i["uid"], i["type"]) for i in items] == [(uid, "ticket.created")]
