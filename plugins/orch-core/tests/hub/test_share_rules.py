"""V3 C6: selective share rules, their evaluation and the share_put / share_revoke sync (pure, fake link)."""

from __future__ import annotations

import dataclasses

import pytest

from orch.hubclient import shares as S
from orch.hubclient.errors import HubRefused


def T(uid, *, labels=(), type="feature", title="A title", due=None, questions=(), needs=(), key=None):
    return S.TicketFact(
        uid,
        key or "K-" + {"a": "1", "b": "2", "c": "3", "d": "4", "e": "5"}.get(uid, uid),
        type,
        title,
        "open",
        tuple(labels),
        due,
        tuple(questions),
        tuple(needs),
    )


Q_OWNER = S.QuestionFact("Q1", "which bank?", True)
Q_OTHER = S.QuestionFact("Q2", "for someone else", False)
TICKETS = [
    T("a", labels=["public"], title="Public note"),
    T("b", labels=["private"], title="Private review"),
    T("c", type="bug", title="Bug"),
    T("d", title="Asks", due="2026-11-01", questions=[Q_OWNER, Q_OTHER],
      needs=[S.NeedFact("question", "Q1", True), S.NeedFact("question", "Q2", False)]),
]  # fmt: skip


def kinds(items):
    return sorted((i.kind, i.ticket_key) for i in items)


def test_default_is_needs_you_summaries_only():
    st = S.ShareState()
    assert st.rules == [{"kind": "needs_you"}]
    items = S.select(st.rules, TICKETS)
    assert [i.kind for i in items] == ["summary"]
    (s,) = items
    assert s.payload == {"type": "question", "title": "which bank?", "deadline": "2026-11-01"}
    assert "Public note" not in repr(items)  # no ticket content leaves by default


def test_rules_select_exactly_their_tickets():
    assert kinds(S.select([{"kind": "tag", "tag": "public"}], TICKETS)) == [("ticket", "K-1")]
    assert kinds(S.select([{"kind": "type", "type": "bug"}], TICKETS)) == [("ticket", "K-3")]
    assert kinds(S.select([{"kind": "ticket", "ticket": "K-2"}], TICKETS)) == [("ticket", "K-2")]
    assert S.select([], TICKETS) == []
    qs = S.select([{"kind": "questions"}], TICKETS)
    assert [(i.kind, i.payload["text"]) for i in qs] == [("question", "which bank?")]  # only the one to the owner


def test_a_summary_never_carries_more_than_type_title_deadline():
    (s,) = S.select([{"kind": "needs_you"}], TICKETS)
    assert set(s.payload) == {"type", "title", "deadline"}


def test_titles_are_one_clean_line():
    t = T("e", title="x\n\x1b[31mred‮" + "y" * 300, labels=["l"])
    (i,) = S.select([{"kind": "tag", "tag": "l"}], [t])
    assert "\n" not in i.payload["title"] and "\x1b" not in i.payload["title"] and len(i.payload["title"]) <= 120


@pytest.mark.parametrize(
    "bad",
    [{}, {"kind": "all"}, {"kind": "tag"}, {"kind": "tag", "tag": ""}, {"kind": "tag", "tag": "a\nb"},
     {"kind": "tag", "tag": "x", "extra": 1}, {"kind": "needs_you", "tag": "x"}, {"kind": "tag", "tag": 3}, "tag"],
)  # fmt: skip
def test_malformed_rules_are_refused(bad):
    with pytest.raises(HubRefused):
        S.normalize_rule(bad)


def test_state_round_trips_and_a_damaged_file_is_refused():
    st = S.ShareState()
    st.add_rule({"kind": "tag", "tag": "public"})
    assert not st.add_rule({"kind": "tag", "tag": "public"})
    st.sent["ticket:a"] = {"item_id": "11" * 16, "digest": "d", "version": 3, "item_hash": None}
    assert S.ShareState.from_json(st.to_json()).to_json() == st.to_json()
    for bad in (
        None,
        {},
        {**st.to_json(), "rules": [{"kind": "x"}]},
        {**st.to_json(), "sent": {"a": {"item_id": "zz"}}},
    ):
        with pytest.raises(HubRefused):
            S.ShareState.from_json(bad)


class FakeLink:
    def __init__(self):
        self.calls, self.fail_on = [], None

    def call(self, op, body):
        if self.fail_on == op:
            raise HubRefused("hub_unreachable")
        self.calls.append((op, body))
        return {}


def fake_build(item_id, item, version):
    return f"sealed:{item_id}:{item.logical}:{version}", "sha256:" + "ab" * 32


def run(st, tickets, link, **kw):
    saved = []
    n = iter(range(1, 1000))
    got = S.sync(st, S.select(st.rules, tickets), link=link, build=fake_build, save=lambda s: saved.append(1),
                 new_id=lambda: f"{next(n):032x}", **kw)  # fmt: skip
    return got


def test_put_then_nothing_again_then_revoke_when_the_rule_is_removed():
    st, link = S.ShareState(), FakeLink()
    st.add_rule({"kind": "tag", "tag": "public"})
    assert run(st, TICKETS, link) == {"put": 2, "revoke": 0, "kept": 0}  # the summary and the public ticket
    assert sorted(b["item_id"] for o, b in link.calls if o == "share_put") == [f"{1:032x}", f"{2:032x}"]
    link.calls.clear()
    assert run(st, TICKETS, link) == {"put": 0, "revoke": 0, "kept": 2} and link.calls == []  # idempotent
    st.remove_rule({"kind": "tag", "tag": "public"})
    assert run(st, TICKETS, link)["revoke"] == 1
    ((op, body),) = link.calls
    assert op == "share_revoke" and body["item_id"] in (f"{1:032x}", f"{2:032x}")
    link.calls.clear()
    assert run(st, TICKETS, link) == {"put": 0, "revoke": 0, "kept": 1} and link.calls == []  # not revoked twice


def test_a_changed_item_is_resent_under_the_same_id():
    st, link = S.ShareState(rules=[{"kind": "tag", "tag": "public"}]), FakeLink()
    run(st, TICKETS, link)
    first = st.sent["ticket:a"]["item_id"]
    link.calls.clear()
    run(st, [T("a", labels=["public"], title="Renamed")] + TICKETS[1:], link)
    assert [(o, b["item_id"]) for o, b in link.calls] == [("share_put", first)]


def test_an_item_that_stops_matching_is_revoked():
    st, link = S.ShareState(rules=[{"kind": "tag", "tag": "public"}]), FakeLink()
    run(st, TICKETS, link)
    link.calls.clear()
    run(st, [T("a", labels=[], title="Public note")] + TICKETS[1:], link)
    assert [o for o, _ in link.calls] == ["share_revoke"]


def test_withdrawn_stays_withdrawn_while_it_matches_and_is_forgotten_after():
    st, link = S.ShareState(rules=[{"kind": "tag", "tag": "public"}]), FakeLink()
    run(st, TICKETS, link)
    st.withdrawn.append("ticket:a")
    link.calls.clear()
    assert run(st, TICKETS, link)["revoke"] == 1 and run(st, TICKETS, link) == {"put": 0, "revoke": 0, "kept": 0}
    assert st.withdrawn == ["ticket:a"]
    run(st, [T("a", labels=[]), *TICKETS[1:]], link)  # stops matching: forgotten
    assert st.withdrawn == []


def test_an_unreachable_hub_keeps_the_id_so_the_retry_does_not_duplicate():
    st, link = S.ShareState(rules=[{"kind": "tag", "tag": "public"}]), FakeLink()
    link.fail_on = "share_put"
    with pytest.raises(HubRefused):
        run(st, TICKETS, link)
    ids = {lg: v["item_id"] for lg, v in st.sent.items()}
    assert ids and all(v["digest"] is None for v in st.sent.values())
    link.fail_on = None
    run(st, TICKETS, link)
    assert {lg: v["item_id"] for lg, v in st.sent.items()} == ids


def test_too_many_items_refuse_before_anything_is_sent(monkeypatch):
    monkeypatch.setattr(S, "MAX_ITEMS", 2)
    st, link = S.ShareState(rules=[{"kind": "type", "type": "feature"}]), FakeLink()
    with pytest.raises(HubRefused) as e:
        run(st, [T(str(n)) for n in range(3)], link)
    assert e.value.code == "too_many_items" and link.calls == []


def test_one_refused_item_is_skipped_and_reported_and_the_rest_goes_on():
    st, link = S.ShareState(), FakeLink()
    st.add_rule({"kind": "tag", "tag": "public"})
    tickets = [T("a", labels=["public"]), T("b", labels=["public"]), T("c", labels=["public"])]
    n = iter(range(1, 1000))

    def build(item_id, item, version):
        if item.ticket_key == "K-2":
            raise HubRefused("malformed")
        return fake_build(item_id, item, version)

    got = S.sync(st, S.select(st.rules, tickets), link=link, build=build, save=lambda s: None,
                 new_id=lambda: f"{next(n):032x}")  # fmt: skip
    assert got["refused"] == {"ticket:b": "malformed"} and (got["put"], got["revoke"], got["kept"]) == (2, 0, 0)
    assert got["refused_items"] == [{"item": "ticket K-2: A title", "code": "malformed", "stale": False}]
    assert [b["item_id"] for o, b in link.calls if o == "share_put"] == [f"{1:032x}", f"{3:032x}"]
    assert "ticket:b" not in st.sent  # nothing was recorded for the refused item


def test_a_put_the_hub_refuses_for_the_item_is_skipped_but_a_link_failure_still_stops():
    class Link(FakeLink):
        def call(self, op, body):
            if op == "share_put" and body["item_id"] == f"{1:032x}":
                raise HubRefused("too_large")
            return super().call(op, body)

    st = S.ShareState()
    st.add_rule({"kind": "tag", "tag": "public"})
    tickets = [T("a", labels=["public"]), T("b", labels=["public"])]
    assert run(st, tickets, Link())["refused"] == {"ticket:a": "too_large"}
    link = FakeLink()
    link.fail_on = "share_put"
    with pytest.raises(HubRefused):  # hub_unreachable is not about the item: stop
        run(S.ShareState(rules=[{"kind": "tag", "tag": "public"}]), tickets, link)


def test_c6r3_2_an_item_carries_the_chain_of_its_own_snapshot_not_a_later_read():
    seen = []

    def later(uid):  # a later read would see a newer head: it must not be used when the snapshot has one
        seen.append(uid)
        return (9, "sha256:" + "ee" * 32)

    t = S.TicketFact("a", "K-1", "feature", "T", "open", ("public",), None, chain=(4, "sha256:" + "cd" * 32))
    (item,) = S.select([{"kind": "tag", "tag": "public"}], [t])
    assert item.chain == (4, "sha256:" + "cd" * 32)
    other = dataclasses.replace(t, chain=(5, "x"))
    assert item.digest == S.select([{"kind": "tag", "tag": "public"}], [other])[0].digest  # the chain is not content
    assert seen == []


def test_c6r3_1_a_refused_update_of_a_shared_item_is_stale_and_stays_visible_in_the_state():
    st, link = S.ShareState(), FakeLink()
    st.add_rule({"kind": "tag", "tag": "public"})
    assert run(st, [T("a", labels=["public"], title="Old")], link)["put"] == 1
    hub_has = st.sent["ticket:a"]["digest"]
    refuse = {"on": True}

    def build(item_id, item, version):
        if refuse["on"]:
            raise HubRefused("too_large")
        return fake_build(item_id, item, version)

    new = [T("a", labels=["public"], title="New")]
    got = S.sync(st, S.select(st.rules, new), link=link, build=build, save=lambda s: None)
    assert got["refused_items"] == [{"item": "ticket K-1: New", "code": "too_large", "stale": True}]
    assert st.sent["ticket:a"]["digest"] == hub_has  # the hub still has the old version
    again = S.ShareState.from_json(st.to_json())  # persisted: `share rule list` shows it later
    assert again.refused_items() == got["refused_items"]
    refuse["on"] = False  # fixed: the next sync sends it and the refusal is gone
    assert S.sync(st, S.select(st.rules, new), link=link, build=build, save=lambda s: None)["put"] == 1
    assert st.refused == {} and "refused_items" not in S.sync(
        st, S.select(st.rules, new), link=link, build=build, save=lambda s: None
    )


def test_c6r3_1_a_refusal_ends_when_the_item_stops_matching_and_a_damaged_entry_is_refused():
    st, link = S.ShareState(), FakeLink()
    st.add_rule({"kind": "tag", "tag": "public"})

    def build(item_id, item, version):
        raise HubRefused("malformed")

    S.sync(st, S.select(st.rules, [T("a", labels=["public"])]), link=link, build=build, save=lambda s: None)
    assert list(st.refused) == ["ticket:a"]
    S.sync(st, S.select(st.rules, [T("a", labels=[])]), link=link, build=build, save=lambda s: None)
    assert st.refused == {}
    bad = st.to_json() | {"refused": {"ticket:a": {"code": "x"}}}
    with pytest.raises(HubRefused):
        S.ShareState.from_json(bad)
    assert S.ShareState.from_json({k: v for k, v in st.to_json().items() if k != "refused"}).refused == {}  # legacy


def test_auto_wanted_is_the_confirmed_selection_only_and_shrinks_with_it():
    st, link = S.ShareState(), FakeLink()
    st.add_rule({"kind": "tag", "tag": "public"})
    first = S.select(st.rules, [T("a", labels=["public"])])
    st.confirm(first, S.plan_of(first))  # what the host does at the owner's confirm
    S.sync(st, first, link=link, build=fake_build, save=lambda s: None)
    assert st.confirmed == ["ticket:a"]
    both = [T("a", labels=["public"], title="Edited"), T("b", labels=["public"])]
    wanted = st.auto_wanted(S.select(st.rules, both))
    assert [i.logical for i in wanted] == ["ticket:a"]  # "b" starts matching later: it waits for a confirm
    assert S.sync(st, wanted, link=link, build=fake_build, save=lambda s: None)["put"] == 1
    assert [i.logical for i in st.auto_wanted(S.select(st.rules, [T("b", labels=["public"])]))] == []
    assert st.confirmed == []  # "a" left; it needs a new confirm to come back
    shown = S.select(st.rules, both)
    with pytest.raises(HubRefused):  # a selection the owner was not shown is not confirmed
        st.confirm(shown, S.plan_of(first))
    assert st.confirmed == []
    st.confirm(shown, S.plan_of(shown))
    assert st.confirmed == ["ticket:a", "ticket:b"]
    assert S.ShareState.from_json(st.to_json()).confirmed == st.confirmed


def test_a_state_from_before_the_confirmed_field_confirms_what_it_already_sent():
    st, link = S.ShareState(), FakeLink()
    st.add_rule({"kind": "tag", "tag": "public"})
    run(st, [T("a", labels=["public"])], link)
    old = {k: v for k, v in st.to_json().items() if k != "confirmed"}
    legacy = S.ShareState.from_json(old)
    assert legacy.confirmed is None
    got = legacy.auto_wanted(S.select(legacy.rules, [T("a", labels=["public"]), T("b", labels=["public"])]))
    assert [i.logical for i in got] == ["ticket:a"]
    with pytest.raises(HubRefused):
        S.ShareState.from_json(old | {"confirmed": [1]})


def test_sync_keeps_what_the_person_was_shown_for_the_admission_of_a_selective_event():
    """Ticket-format 15.7: a ``sent`` entry carries the chain claim and the f1 values the item was signed with, and the
    item hashes it replaced; an item without f1 grants no decision. Round-trips through the state file."""
    chain = (3, "sha256:" + "c1" * 32)
    f1 = {"roster_v": 2, "gates": {}, "questions": {"Q1": {"hash": "sha256:" + "d1" * 32}}}
    item = S.ShareItem("question:t:Q1", "question", "t", "K-1", {"x": 1}, "dig1", f1, "", (), chain)
    st, link = S.ShareState(), FakeLink()
    saved = []

    def build(version_hash):
        return lambda item_id, it, version: (f"sealed:{version}", version_hash)

    S.sync(st, [item], link=link, build=build("sha256:" + "01" * 32), save=lambda s: saved.append(1))
    (entry,) = st.sent.values()
    assert entry["chain"] == {"log": "t", "seq": 3, "head": chain[1]} and entry["f1"] == f1
    assert "superseded" not in entry
    changed = S.ShareItem(
        "question:t:Q1", "question", "t", "K-1", {"x": 1}, "dig2", {**f1, "roster_v": 3}, "", (), chain
    )
    S.sync(st, [changed], link=link, build=build("sha256:" + "02" * 32), save=lambda s: saved.append(1))
    (entry,) = st.sent.values()
    assert entry["f1"]["roster_v"] == 3 and entry["superseded"] == ["sha256:" + "01" * 32]
    assert S.ShareState.from_json(st.to_json()).sent == st.sent
    bare = S.ShareItem("question:t:Q1", "question", "t", "K-1", {"x": 1}, "dig3", None, "", (), chain)
    S.sync(st, [bare], link=link, build=build("sha256:" + "03" * 32), save=lambda s: saved.append(1))
    (entry,) = st.sent.values()
    assert "chain" not in entry and "f1" not in entry  # no f1: the hub cannot build on it, the host grants nothing
    bad = st.to_json()
    bad["sent"][next(iter(bad["sent"]))]["chain"] = {"log": "t"}
    with pytest.raises(HubRefused):
        S.ShareState.from_json(bad)
