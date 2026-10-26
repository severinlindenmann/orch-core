"""H6b, ``activity.list`` (§0.8, cap ``activity/1``): one test per acceptance item of the H6b row in §8."""

from __future__ import annotations

import json

import pytest

from orch.host import reads_feed
from tests.host.dispatch_helpers import err
from tests.host.live_helpers import SESSION, World, jpos_of, ok, world
from tests.host.test_dispatch_matrix import as_agent_class


@pytest.fixture
def w(tmp_path):
    s = world(tmp_path)
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


@pytest.fixture
def q(tmp_path):
    s = world(tmp_path, map_to="quinn")  # column P: quinn, a plain member
    try:
        yield s
    finally:
        s.close()
        s.lab.close()


GATES = {"plan": {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": False}}


def feed(c, args=None, **kw):
    return ok(c.req("activity.list", args or {}, **kw))["data"]


def pages(c, args, **kw):
    """Every page of a feed: ``[page, ...]`` until ``next_page`` is null."""
    out, args = [], dict(args)
    while True:
        page = feed(c, args, **kw)
        out.append(page)
        if page["next_page"] is None:
            return out
        args["page"] = page["next_page"]


def flat(pgs):
    return [i for p in pgs for i in p["items"]]


def populate(s: World):
    """An open ticket, a ticket restricted to quinn alone, and the workspace events of two grants and a setting."""
    ua, ka = s.ticket("open one")
    ur, kr = s.ticket("secret one")
    s.write(kr, "s1")
    s.write(kr, "s2")
    s.restrict(ur, [s.quinn.ref])
    s.write(ka, "a1")
    s.write(ka, "a2")
    s.owner_grant()
    s.grant_for(s.quinn)
    s.person_event("settings.changed", set={"claim_ttl_min": 90})
    return ua, ka, ur, kr


def uids(items):
    return {i["uid"] for i in items if i["uid"]}


def types(items):
    return [i["type"] for i in items]


# --- caps and the shape ---------------------------------------------------------------------------------------------


def test_activity_1_is_in_welcome_caps(w):
    assert "activity/1" in w.client().welcome["caps"]
    assert w.client().welcome["ops"]["activity.list"] == 1


def test_an_item_is_the_subscription_item_plus_actor_title_summary_data_and_there_is_no_total(w):
    uid, key = w.ticket("hello")
    w.write(key, "x")
    c = w.client()
    page = feed(c, {"scope": "tickets"})
    assert set(page) == {"items", "next_page", "sub_cursor", "as_of"}  # no total
    item = page["items"][0]
    assert set(item) == {
        "id", "jpos", "log", "key", "uid", "title", "seq", "ws_seq", "type", "at", "actor", "summary", "data",
    }  # fmt: skip
    assert item["type"] == "log.added" and item["title"] == "hello" and item["key"] == key and item["uid"] == uid
    assert item["summary"] == "x" and item["data"] == {"text": "x"} and item["actor"]["kind"] == "agent"
    assert item["id"] == f"{item['jpos']}.e"
    ws = feed(c, {"scope": "workspace", "limit": 1})["items"][0]
    assert ws["log"] == "workspace" and ws["key"] is None and ws["uid"] is None and ws["title"] is None
    assert ws["ws_seq"] == ws["seq"]


def test_jpos_is_the_journals_position_and_the_ids_are_the_subscription_ids(w):
    populate(w)
    c = w.client()
    sub_conn = w.conn()
    sub = sub_conn.open(["tickets", "workspace"])
    items = flat(pages(c, {"order": "asc", "limit": 4}))
    journal = w.service.journal
    assert [i["jpos"] for i in items] == sorted({i["jpos"] for i in items})
    assert len(items) < journal.head  # the restricted ticket's jpos are gaps, never holes in a page
    for i in items:
        entry = journal.entry_at(i["jpos"])
        assert (entry.log, entry.seq, entry.ws_seq, entry.type, entry.at) == (
            i["log"], i["seq"], i["ws_seq"], i["type"], i["at"]
        )  # fmt: skip
    top = feed(c, {"limit": 1})
    assert top["items"][0]["jpos"] == journal.head == jpos_of(top["sub_cursor"])
    # the id of a live item is the id of the feed item of the same event
    _, key = w.ticket("later")
    w.write(key, "live")
    got, _ = sub_conn.items_until(sub["sub"], journal.head)
    live = {i["id"]: i for i in got}
    again = {i["id"]: i for i in flat(pages(c, {"limit": 200}))}
    assert live and set(live) <= set(again)
    for id_, item in live.items():
        assert (again[id_]["jpos"], again[id_]["type"], again[id_]["uid"]) == (item["jpos"], item["type"], item["uid"])


# --- per-item visibility --------------------------------------------------------------------------------------------


def test_every_item_is_filtered_on_its_own_for_p_g_and_n(w):
    ua, ka, ur, kr = populate(w)
    c = w.client()
    owner = flat(pages(c, {"limit": 200}))
    assert ua in uids(owner) and ur not in uids(owner)  # the owner is not on the restricted list
    assert types(owner).count("grant.issued") == 3  # a person-class owner sees every grant (bootstrap's too)
    g = flat(pages(c, {"limit": 200}, grant=w.grant))
    assert ur not in uids(g)
    issued = [i for i in g if i["type"] == "grant.issued"]
    assert len(issued) == 2 and {i["data"]["person"] for i in issued} == {w.lab.owner}  # only its own person's
    assert "settings.changed" in types(g)
    as_agent_class(w)
    n = flat(pages(w.client(), {"limit": 200}))
    assert ur not in uids(n) and ua in uids(n)
    assert all(i["log"] != "workspace" for i in n)  # no-person: no workspace-log event at all


def test_a_non_owner_gets_only_its_own_grant_items(q):
    ua, ka, ur, kr = populate(q)
    items = flat(pages(q.client(), {"limit": 200}))
    assert ur in uids(items)  # quinn is on the restricted list: the whole history
    assert [i["type"] for i in items if i["log"] == ur][0] == "visibility.changed"
    issued = [i for i in items if i["type"] == "grant.issued"]
    assert [i["data"]["person"] for i in issued] == [q.quinn.ref]  # not the owner's grant
    q.person_event("grant.revoked", grant=q.lab.ws.grant_id)  # the owner's grant: not quinn's
    q.person_event("grant.revoked", who=q.quinn, grant=issued[0]["data"]["grant"])  # quinn's own
    revoked = [i for i in flat(pages(q.client(), {"limit": 200})) if i["type"] == "grant.revoked"]
    assert [i["data"]["person"] for i in revoked] == [q.quinn.ref]


def test_workspace_log_data_holds_only_the_fields_of_its_table(w):
    w.owner_grant()
    w.person_event("policy.changed", gates=GATES)
    w.person_event("settings.changed", set={"claim_ttl_min": 90, "repos": {"x": {"path": "/secret/path"}}})
    w.person_event("role.changed", person=w.quinn.ref, role="viewer")
    third = w.lab.add_member()
    w.lab.revoke_device(third)
    w.check_journals()
    items = flat(pages(w.client(), {"scope": "workspace", "limit": 200}))
    table = {
        "workspace.created": {"owner"}, "member.added": {"person", "role"}, "role.changed": {"person", "role"},
        "policy.changed": {"gates"}, "settings.changed": {"keys", "repos"}, "device.revoked": {"person", "device"},
        "grant.issued": {"grant", "person", "scope", "expires_at"},
    }  # fmt: skip
    seen = set()
    for i in items:
        assert set(i["data"]) <= table[i["type"]], i
        seen.add(i["type"])
    assert {"policy.changed", "settings.changed", "device.revoked", "grant.issued", "role.changed"} <= seen
    body = json.dumps(items)
    for word in ("secret_hash", "revocation", "label", "cert", "reason", "/secret/path", "pk_pub", "sig"):
        assert word not in body, word
    (settings,) = [i for i in items if i["type"] == "settings.changed"]
    assert settings["data"] == {"keys": ["claim_ttl_min", "repos"], "repos": {"x": "set"}}  # names only, never paths
    (revoked,) = [i for i in items if i["type"] == "device.revoked"]
    assert revoked["data"]["person"] == third.ref and revoked["data"]["device"] == third.device


def test_a_viewer_role_person_gets_the_same_items_as_a_member_without_grant_items(q):
    q.grant_for(q.quinn)  # before the role change: a viewer cannot write
    q.owner_grant()
    q.person_event("role.changed", person=q.quinn.ref, role="viewer")
    q.person_event("policy.changed", gates=GATES)
    q.person_event("settings.changed", set={"claim_ttl_min": 90})
    q.person_event("grant.revoked", grant=q.lab.ws.grant_id)
    third = q.lab.add_member()
    q.lab.revoke_device(third)
    q.check_journals()
    items = flat(pages(q.client(), {"scope": "workspace", "limit": 200}))
    got = set(types(items))
    assert {"member.added", "role.changed", "device.revoked", "policy.changed", "settings.changed"} <= got
    assert not {t for t in got if t.startswith("grant.")} - {"grant.issued"}  # only quinn's own, as for a member
    assert [i["data"]["person"] for i in items if i["type"] == "grant.issued"] == [q.quinn.ref]
    added = [i["data"] for i in items if i["type"] == "member.added"]
    assert {"person": q.quinn.ref, "role": "member"} in added and {"person": third.ref, "role": "member"} in added


def test_duplicate_of_an_invisible_ticket_is_the_key_only(w):
    ua, ka = w.ticket("survivor")
    ub, kb = w.ticket("duplicate")
    w.write(ka, "n")
    c = w.client()
    w.person_event("ticket.closed", ub, resolution="duplicate", duplicate_of=ka)
    w.restrict(ua, [w.quinn.ref])  # the survivor is not the owner's to see now
    items = flat(pages(c, {"limit": 200}))
    (closed,) = [i for i in items if i["type"] == "ticket.closed"]
    assert closed["data"] == {"resolution": "duplicate", "duplicate_of": ka}
    assert ua not in uids(items) and "survivor" not in json.dumps(items)


# --- filters --------------------------------------------------------------------------------------------------------


def test_filters_combine_with_and_and_their_values_with_or(w):
    ua, ka, ur, kr = populate(w)
    c = w.client()
    allitems = flat(pages(c, {"limit": 200}))
    assert set(types(flat(pages(c, {"scope": "workspace", "limit": 200})))) <= set(types(allitems))
    assert all(i["log"] == "workspace" for i in flat(pages(c, {"scope": "workspace", "limit": 200})))
    assert all(i["log"] != "workspace" for i in flat(pages(c, {"scope": "tickets", "limit": 200})))
    one = flat(pages(c, {"ticket": ka, "limit": 200}))
    assert one and uids(one) == {ua}
    both = flat(pages(c, {"types": ["log.added", "ticket.created"], "limit": 200}))
    assert set(types(both)) == {"log.added", "ticket.created"}
    assert {i["type"] for i in flat(pages(c, {"types": ["log.added"], "ticket": ka, "limit": 200}))} == {"log.added"}
    agents = flat(pages(c, {"actor_kind": ["agent"], "limit": 200}))
    assert agents and {i["actor"]["kind"] for i in agents} == {"agent"}
    persons = flat(pages(c, {"actor_kind": ["person", "agent"], "limit": 200}))
    assert len(persons) == len(allitems)
    mine = flat(pages(c, {"person": w.lab.owner, "limit": 200}))  # the owner and the agents acting for them
    assert mine and all(w.lab.owner in (i["actor"]["id"], i["actor"].get("for")) for i in mine)
    assert types(flat(pages(c, {"person": w.quinn.ref, "limit": 200}))) == ["grant.issued"]  # quinn's own grant
    first, last = min(i["at"] for i in allitems), max(i["at"] for i in allitems)
    assert last > first
    only_first = flat(pages(c, {"until": last, "limit": 200}))
    assert all(i["at"] < last for i in only_first) and len(only_first) < len(allitems)
    assert all(i["at"] >= last for i in flat(pages(c, {"since": last, "limit": 200})))  # since is inclusive
    asc = flat(pages(c, {"order": "asc", "limit": 200}))
    assert [i["jpos"] for i in asc] == sorted(i["jpos"] for i in allitems)
    assert [i["jpos"] for i in allitems] == sorted((i["jpos"] for i in allitems), reverse=True)


def test_bad_filters_are_invalid_input_or_not_found(w):
    ua, ka, ur, kr = populate(w)
    c = w.client()
    assert err(c.req("activity.list", {"scope": "workspace", "ticket": ka})) == "invalid.input"
    assert err(c.req("activity.list", {"since": "2026-09-21T14:13:24Z", "until": "2026-09-21T14:13:23Z"})) == (
        "invalid.input"
    )
    assert err(c.req("activity.list", {"ticket": kr})) == "not_found"  # restricted: as ticket.get
    assert err(c.req("activity.list", {"ticket": "NOPE-1"})) == "not_found"
    assert err(c.req("activity.list", {"limit": 0})) == "invalid.input"


# --- paging ---------------------------------------------------------------------------------------------------------


def test_next_page_and_page_fullness_without_holes(w):
    ua, ka, ur, kr = populate(w)
    for n in range(5):
        w.write(ka, f"more {n}")
    c = w.client()
    whole = feed(c, {"limit": 200})
    assert whole["next_page"] is None
    everything = flat(pages(c, {"limit": 200}))
    for order in ("desc", "asc"):
        want = everything if order == "desc" else everything[::-1]
        pgs = pages(c, {"order": order, "limit": 3})
        assert [i["id"] for i in flat(pgs)] == [i["id"] for i in want]  # no repeat, no gap
        assert all(len(p["items"]) == 3 for p in pgs[:-1]) and 1 <= len(pgs[-1]["items"]) <= 3
        assert len({p["sub_cursor"] for p in pgs}) == 1  # the first page's, on every page
    # exactly full: no empty trailing page
    exact = pages(c, {"limit": len(everything)})
    assert len(exact) == 1 and exact[0]["next_page"] is None
    assert len(pages(c, {"limit": len(everything) - 1})) == 2


def test_the_scan_budget_gives_short_pages_and_a_token_not_a_hole(w, monkeypatch):
    ur, kr = w.ticket("hidden")
    for n in range(6):
        w.write(kr, f"hidden {n}")
    w.restrict(ur, [w.quinn.ref])  # invisible to the owner: scanned, never returned
    ua, ka = w.ticket("shown")
    for n in range(2):
        w.write(ka, f"visible {n}")
    c = w.client()
    everything = flat(pages(c, {"limit": 200}))
    monkeypatch.setattr(reads_feed, "SCAN_BUDGET", 4)
    pgs = pages(c, {"limit": 200})
    assert len(pgs) > 1 and any(len(p["items"]) < 4 for p in pgs[:-1])  # short pages, each with a token
    assert all(p["next_page"] for p in pgs[:-1])
    assert [i["id"] for i in flat(pgs)] == [i["id"] for i in everything]
    # the budget ran out inside a hidden run: an empty page still continues
    pgs = pages(c, {"limit": 1})
    assert [i["id"] for i in flat(pgs)] == [i["id"] for i in everything]


def test_the_first_pages_sub_cursor_joined_to_sub_open_loses_and_repeats_nothing(w):
    populate(w)
    _, key = w.ticket("t")
    c, sub_conn = w.client(), w.conn()
    page = feed(c, {"limit": 2})
    cursor = page["sub_cursor"]
    assert jpos_of(cursor) == page["items"][0]["jpos"] == w.service.journal.head
    w.write(key, "after 1")
    w.write(key, "after 2")
    sub = sub_conn.open(["tickets", "workspace"], after=cursor)
    items, _ = sub_conn.items_until(sub["sub"], w.service.journal.head)
    assert [i["jpos"] for i in items] == [page["items"][0]["jpos"] + 1, page["items"][0]["jpos"] + 2]
    later = feed(c, {"limit": 200})
    assert {i["id"] for i in items} <= {i["id"] for i in later["items"]}
    assert not {i["id"] for i in items} & {i["id"] for i in flat(pages(c, {"limit": 2, "page": page["next_page"]}))}


def test_a_later_page_reads_only_up_to_the_snapshot(w):
    ua, ka, ur, kr = populate(w)
    c = w.client()
    first = feed(c, {"limit": 2})
    head = w.service.journal.head
    w.write(ka, "after the snapshot")
    rest = flat(pages(c, {"limit": 2, "page": first["next_page"]}))
    assert rest and all(i["jpos"] < first["items"][1]["jpos"] for i in rest)
    assert max(i["jpos"] for i in first["items"] + rest) <= head


# --- page tokens ----------------------------------------------------------------------------------------------------


def test_a_token_with_other_arguments_is_invalid_input(w):
    populate(w)
    c = w.client()
    first = feed(c, {"limit": 2, "types": ["log.added", "ticket.created"]})
    tok = first["next_page"]
    assert tok
    assert err(c.req("activity.list", {"limit": 2, "page": tok})) == "invalid.input"
    assert err(c.req("activity.list", {"limit": 2, "types": ["log.added"], "page": tok})) == "invalid.input"
    assert err(c.req("activity.list", {"limit": 2, "types": ["log.added", "ticket.created"], "order": "asc",
                                       "page": tok})) == "invalid.input"  # fmt: skip
    ok(c.req("activity.list", {"limit": 5, "types": ["log.added", "ticket.created"], "page": tok}))  # limit may change


def test_an_expired_token_is_page_expired(w):
    populate(w)
    c = w.client()
    tok = feed(c, {"limit": 2})["next_page"]
    w.lab.ws.clock[0] += 601
    assert err(c.req("activity.list", {"limit": 2, "page": tok})) == "page.expired"


def test_a_forged_altered_or_other_principal_token_is_page_expired(w):
    populate(w)
    c = w.client()
    args = {"limit": 2}
    tok = feed(c, args)["next_page"]
    for bad in (
        "garbage", "p1.e30.AAAA", tok[:-2] + ("AA" if not tok.endswith("AA") else "BB"),
        tok.replace("p1.", "p1.x", 1), tok + "x", "p1." + tok.split(".")[2] + "." + tok.split(".")[1],
    ):  # fmt: skip
        assert err(c.req("activity.list", {**args, "page": bad})) == "page.expired", bad
    other = c.req("activity.list", {**args, "page": tok}, grant=w.grant)  # another principal (the grant's)
    assert err(other) == "page.expired"
    # the same token still works for its own principal
    assert ok(c.req("activity.list", {**args, "page": tok}))["data"]["items"]
    # a token of another op
    other_op = c.req("ticket.query", {"limit": 1})
    if other_op["ok"] and other_op["result"]["data"].get("next_page"):  # served by another implementer's handler
        assert err(c.req("activity.list", {**args, "page": other_op["result"]["data"]["next_page"]})) == "page.expired"


def test_a_ticket_that_becomes_visible_between_pages_adds_nothing(w):
    ua, ka, ur, kr = populate(w)
    c = w.client()
    first = feed(c, {"limit": 2})
    assert ur not in uids(first["items"])
    w.restrict(ur, [w.quinn.ref, w.lab.owner])  # now visible to the owner
    w.write(ka, "post-snapshot")
    rest = flat(pages(c, {"limit": 2, "page": first["next_page"]}))
    assert ur not in uids(rest)  # not in the snapshot: adds nothing
    assert ur in uids(flat(pages(c, {"limit": 200})))  # a fresh snapshot has it, whole history


def test_a_ticket_that_becomes_invisible_between_pages_is_dropped(w):
    ua, ka, ur, kr = populate(w)
    c = w.client()
    first = feed(c, {"limit": 2})
    old = flat(pages(c, {"limit": 200}))
    assert ua in uids(old)
    w.restrict(ua, [w.quinn.ref])  # the owner loses the open ticket
    rest = flat(pages(c, {"limit": 2, "page": first["next_page"]}))
    assert ua not in uids(rest) and rest  # workspace events remain
    assert ua not in uids(flat(pages(c, {"limit": 200})))


def test_a_removed_person_on_page_two_is_page_expired(q):
    """Stricter than "filtered as the new viewer" (§0.8): a change of the viewer's person on the same principal,
    to none included, expires the token (pages.resume), so nothing of the first viewer's snapshot is served."""
    ua, ka, ur, kr = populate(q)
    c = q.client()
    first = feed(c, {"limit": 2})
    assert ur in uids(flat(pages(c, {"limit": 200})))
    q.person_event("member.removed", person=q.quinn.ref)
    res = c.req("activity.list", {"limit": 2, "page": first["next_page"]})
    assert err(res) == "page.expired"  # the same principal, now no-person


def test_a_revoked_grant_on_page_two_is_filtered_as_the_new_viewer(w):
    ua, ka, ur, kr = populate(w)
    c = w.client()
    first = feed(c, {"limit": 2}, grant=w.grant)
    w.person_event("grant.revoked", grant=w.lab.ws.grant_id)
    res = c.req("activity.list", {"limit": 2, "page": first["next_page"]}, grant=w.grant)
    assert err(res) == "page.expired"  # the grant no longer verifies: another principal, nothing carries over
    # the P token of the same connection is another principal's
    assert err(c.req("activity.list", {"limit": 2, "page": first["next_page"]})) == "page.expired"


def test_a_ticket_updated_summary_equals_ticket_gets(w):
    uid, key = w.ticket("summary")
    c = w.client()
    for text in ("first criterion", "second criterion"):
        ok(c.req("ac.add", {"ref": key, "text": text}, grant=w.grant, session=SESSION, idem=True))
    ok(c.req("log", {"ref": key, "text": "note"}, grant=w.grant, session=SESSION, idem=True))
    feed_items = {i["seq"]: i for i in flat(pages(c, {"ticket": key, "limit": 200}))}
    updated = [i for i in feed_items.values() if i["type"] == "ticket.updated"]
    assert len(updated) == 2 and updated[0]["summary"] != updated[1]["summary"]
    assert any("AC1" in i["summary"] for i in updated) and any("AC2" in i["summary"] for i in updated)
    res = c.req("ticket.get", {"ref": key})
    if not res["ok"]:  # ticket.get is another handler of the same group
        assert err(res) == "unknown_command"
        return
    for entry in res["result"]["data"]["log"]:
        assert feed_items[entry["seq"]]["summary"] == entry["summary"]
        assert feed_items[entry["seq"]]["data"] == entry["data"]


# --- scan finding: the activity base cache (stale cache / integrity) -------------------------------------------------


def _fresh(w, c, args, **kw):
    """The same feed computed with the base cache dropped first."""
    w.service.activity_base = None
    return flat(pages(c, args, **kw))


def test_scan_finding_the_cache_is_dropped_by_a_journal_rebuild_after_a_foreign_append(w):
    uid, key = w.ticket("cached")
    w.write(key, "one")
    c = w.client()
    flat(pages(c, {"limit": 200}))  # warm the cache
    generation = w.service.journal.generation
    w.append(lambda store: (store.ticket(uid), w.lab.ws.log(uid, "foreign")))  # another writer: not the host
    w.check_journals()
    assert w.service.journal.generation > generation  # rebuilt
    after = flat(pages(c, {"limit": 200}))
    assert w.service.activity_base.key[1] == w.service.journal.generation
    assert [i for i in after if i["data"].get("text") == "foreign"]
    assert after == _fresh(w, c, {"limit": 200})


def test_scan_finding_a_cache_entry_that_no_longer_matches_the_journal_is_rebuilt(w):
    uid, key = w.ticket("cached")
    w.write(key, "one")
    c = w.client()
    warm = flat(pages(c, {"limit": 200}))
    cache = w.service.activity_base
    cache.entries[-1] = reads_feed.Entry("workspace", "member.added", "1970-01-01T00:00:00Z", {}, None, {})
    assert flat(pages(c, {"limit": 200})) == warm  # the tampered position does not match the journal: rebuilt


def test_scan_finding_cached_items_never_carry_another_viewers_view(w):
    ua, ka, ur, kr = populate(w)
    c = w.client()
    owner = flat(pages(c, {"limit": 200}))  # the owner warms the cache
    g = flat(pages(c, {"limit": 200}, grant=w.grant))
    assert g == _fresh(w, c, {"limit": 200}, grant=w.grant)
    w.restrict(ua, [w.quinn.ref])  # a visibility change after the cache was warmed
    hidden = flat(pages(c, {"limit": 200}))
    assert ua in uids(owner) and ua not in uids(hidden)
    assert hidden == _fresh(w, c, {"limit": 200})
    as_agent_class(w)
    n = flat(pages(w.client(), {"limit": 200}))
    assert n == _fresh(w, w.client(), {"limit": 200}) and all(i["log"] != "workspace" for i in n)


# --- H6b fix round: compact snapshot visibility, capped base cache, no shared state ---


def test_the_snapshot_holds_the_smaller_visibility_list_and_unpacks_exactly():
    allu = [f"t{n:026d}" for n in range(200_000)]
    for seen in (allu, allu[:1000], allu[::2], []):
        packed = reads_feed.pack_vis(allu, seen)
        assert len(next(iter(packed.values()))) <= len(allu) // 2
        test = reads_feed.unpack_vis(packed)
        want = set(seen)
        assert all(test(u) == (u in want) for u in allu[:5000] + allu[-5000:])
        assert len(json.dumps(packed)) < 200_000 // 2 * 32  # worst case half the tickets: far under 8 MiB
    from orch.host import pages as pages_mod

    worst = reads_feed.pack_vis(allu, allu[::2])
    assert len(json.dumps(worst)) < pages_mod.PRINCIPAL_BYTES // 2


def test_the_activity_snapshot_stores_hidden_uids_only_not_a_vis_map(w):
    ua, ka, ur, kr = populate(w)
    c = w.client()
    page = feed(c, {"limit": 2})
    assert page["next_page"]
    snap = [s for s in w.service.pages._snaps.values() if s.op == "activity.list"][-1]  # the newest
    assert snap.data["vis"] == {"hidden": [ur]} or snap.data["vis"] == {"shown": [ua]}
    assert flat(pages(c, {"limit": 2})) == flat(pages(c, {"limit": 200}))


@pytest.fixture
def small_cache(monkeypatch):
    monkeypatch.setattr(reads_feed, "MAX_ENTRIES", 8)
    monkeypatch.setattr(reads_feed, "SCAN_BUDGET", 6)


def _cold_all(w, c, args, monkeypatch, **kw):
    """The feed computed with the real caps and an empty cache (the oracle)."""
    with monkeypatch.context() as m:
        m.setattr(reads_feed, "MAX_ENTRIES", 50_000)
        m.setattr(reads_feed, "SCAN_BUDGET", 10_000)
        w.service.activity_base = None
        return flat(pages(c, args, **kw))


@pytest.mark.parametrize("order", ["desc", "asc"])
def test_the_base_cache_never_exceeds_its_cap_and_results_equal_a_cold_computation(w, monkeypatch, order, small_cache):
    ua, ka, ur, kr = populate(w)
    for n in range(12):
        w.write(ka, f"more {n}")
    c = w.client()
    args = {"limit": 3, "order": order}
    got = flat(pages(c, args))
    cache = w.service.activity_base
    assert len(cache.entries) <= 8 and cache.first + len(cache.entries) - 1 == w.service.journal.head
    assert got == _cold_all(w, c, args, monkeypatch)
    assert len(got) > 8  # older positions than the cache held were served too (cold range reads)
    w.write(ka, "after the cache was built")  # the journal extends it, the oldest go
    cache = w.service.activity_base
    again = flat(pages(c, args))
    cache = w.service.activity_base
    assert len(cache.entries) <= 8 + reads_feed.MAX_ENTRIES // 16 and again[-1 if order == "asc" else 0]["seq"] >= 1
    assert again == _cold_all(w, c, args, monkeypatch)


def test_the_base_cache_byte_cap_holds(w, monkeypatch):
    ua, ka, ur, kr = populate(w)
    for n in range(10):
        w.write(ka, "x" * 100 + str(n))
    c = w.client()
    probe = flat(pages(c, {"limit": 200}))
    one = w.service.activity_base.entries[-1].size
    monkeypatch.setattr(reads_feed, "MAX_BYTES", one * 3)
    w.service.activity_base = None
    got = flat(pages(c, {"limit": 4}))
    assert w.service.activity_base.bytes <= one * 3 + 400 and len(w.service.activity_base.entries) < len(probe)
    assert got == probe


def test_an_idle_base_cache_is_dropped_and_rebuilt_equal(w):
    ua, ka, ur, kr = populate(w)
    c = w.client()
    warm = flat(pages(c, {"limit": 200}))
    cache = w.service.activity_base
    assert cache.entries and reads_feed.sweep(cache.used + 1) == 0  # in use
    assert reads_feed.sweep(cache.used + reads_feed.IDLE_SECONDS + 1) >= 1
    assert cache.entries == [] and cache.bytes == 0
    assert flat(pages(c, {"limit": 200})) == warm


def test_a_mutated_response_item_never_changes_a_later_response(w):
    ua, ka, ur, kr = populate(w)
    c = w.client()
    first = feed(c, {"limit": 200})
    snapshot = json.dumps(first, sort_keys=True)
    for item in first["items"]:
        item["data"]["MUTATED"] = True
        item["actor"]["id"] = "MUTATED"
        item["summary"] = "MUTATED"
        for v in item["data"].values():
            if isinstance(v, (dict, list)):
                v.clear()
    assert json.dumps(feed(c, {"limit": 200}), sort_keys=True) == snapshot


# --- Opus delta: the cold path of the capped cache ------------------------------------------------------------------


def test_five_asc_first_pages_beyond_the_cache_derive_the_logs_once(w, monkeypatch):
    monkeypatch.setattr(reads_feed, "MAX_ENTRIES", 5)
    for i in range(20):
        w.ticket(f"t{i}")
    calls = []
    real = reads_feed.derive

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(reads_feed, "derive", spy)
    c = w.client()
    first = feed(c, {"order": "asc", "limit": 5})
    calls.clear()
    pages_seen = [feed(c, {"order": "asc", "limit": 5}) for _ in range(5)]
    assert all(p["items"] == first["items"] for p in pages_seen)
    assert len(calls) == 0  # every repeat is served from the kept cold chunk
    monkeypatch.setattr(reads_feed, "MAX_ENTRIES", 50_000)
    w.service.activity_base = None
    assert feed(c, {"order": "asc", "limit": 5})["items"] == first["items"]  # cold equals warm


def test_one_cold_derivation_per_principal_at_a_time(w, monkeypatch):
    from tests.host.live_helpers import principal_of

    monkeypatch.setattr(reads_feed, "MAX_ENTRIES", 5)
    for i in range(12):
        w.ticket(f"t{i}")
    c = w.client()
    feed(c, {"limit": 5})  # warm the (tiny) cache
    w.service.activity_base.cold, w.service.activity_base.cold_bytes = [], 0  # the next asc page must derive
    principal = principal_of()  # this connection's principal (uid:<n>)
    reads_feed._COLD_BUSY.add(principal)
    try:
        res = c.req("activity.list", {"order": "asc", "limit": 5})
        assert err(res) == "quota.host"
    finally:
        reads_feed._COLD_BUSY.discard(principal)
    assert feed(c, {"order": "asc", "limit": 5})["items"]


# --- scan finding: the cold chunks never cross an epoch or a journal build ------------------------------------------


def test_scan_finding_a_cold_chunk_is_never_served_after_a_rebuild(w, monkeypatch):
    monkeypatch.setattr(reads_feed, "MAX_ENTRIES", 5)
    uid, key = w.ticket("t0")
    for i in range(10):
        w.ticket(f"t{i + 1}")
    c = w.client()
    before = feed(c, {"order": "asc", "limit": 3})  # derives and keeps a cold chunk
    cache = w.service.activity_base
    assert cache.cold
    w.append(lambda store: (store.ticket(uid), w.lab.ws.log(uid, "foreign")))  # another writer
    w.check_journals()  # rebuild: a new journal generation
    assert cache.cold_at(1, (cache.key[0], cache.key[1])) is None or cache.key[1] != w.service.journal.generation
    after = feed(c, {"order": "asc", "limit": 50})
    w.service.activity_base = None
    assert after == feed(c, {"order": "asc", "limit": 50})  # equals a cold computation of the new generation
    assert before["items"][0]["id"] == after["items"][0]["id"]


def test_scan_finding_a_derivation_interleaved_with_a_rebuild_publishes_nothing(w, monkeypatch):
    monkeypatch.setattr(reads_feed, "MAX_ENTRIES", 5)
    for i in range(10):
        w.ticket(f"t{i}")
    c = w.client()
    feed(c, {"limit": 5})
    cache = w.service.activity_base
    cache.cold, cache.cold_bytes = [], 0
    real = reads_feed.derive

    def racing(*a, **k):  # a rebuild lands while the cold chunk is being derived
        out = real(*a, **k)
        w.service.journal.generation += 1
        return out

    monkeypatch.setattr(reads_feed, "derive", racing)
    res = c.req("activity.list", {"order": "asc", "limit": 3})
    assert err(res) == "page.expired"
    assert cache.cold == []  # nothing derived under the old generation was published
    monkeypatch.setattr(reads_feed, "derive", real)
    assert feed(c, {"order": "asc", "limit": 3})["items"]


def test_human_1_is_in_welcome_caps_with_its_ops(w):
    welcome = w.client().welcome
    assert "human/1" in welcome["caps"]
    for op in ("human.prepare", "human.get", "human.submit", "human.cancel", "human.pending"):
        assert welcome["ops"][op] == 1
