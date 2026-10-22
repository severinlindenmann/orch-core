"""V3 C6: selective share rules, their evaluation and the share_put / share_revoke sync (pure, fake link)."""

from __future__ import annotations

import pytest

from orch.hubclient import shares as S
from orch.hubclient.errors import HubRefused


def T(uid, *, labels=(), type="feature", title="A title", due=None, questions=(), needs=(), key=None):
    return S.TicketFact(uid, key or f"K-{uid}", type, title, "open", tuple(labels), due, tuple(questions), tuple(needs))


Q_OWNER = S.QuestionFact("q1", "which bank?", True)
Q_OTHER = S.QuestionFact("q2", "for someone else", False)
TICKETS = [
    T("a", labels=["public"], title="Public note"),
    T("b", labels=["private"], title="Private review"),
    T("c", type="bug", title="Bug"),
    T("d", title="Asks", due="2026-11-01", questions=[Q_OWNER, Q_OTHER],
      needs=[S.NeedFact("question", "q1", True), S.NeedFact("question", "q2", False)]),
]  # fmt: skip


def kinds(items):
    return sorted((i.kind, i.ticket_key) for i in items)


def test_default_is_needs_you_summaries_only():
    st = S.ShareState()
    assert st.rules == [{"kind": "needs_you"}]
    items = S.select(st.rules, TICKETS)
    assert [i.kind for i in items] == ["summary"]
    (s,) = items
    assert s.payload == {"kind": "summary", "type": "question", "title": "which bank?", "deadline": "2026-11-01"}
    assert "Public note" not in repr(items)  # no ticket content leaves by default


def test_rules_select_exactly_their_tickets():
    assert kinds(S.select([{"kind": "tag", "tag": "public"}], TICKETS)) == [("ticket", "K-a")]
    assert kinds(S.select([{"kind": "type", "type": "bug"}], TICKETS)) == [("ticket", "K-c")]
    assert kinds(S.select([{"kind": "ticket", "ticket": "K-b"}], TICKETS)) == [("ticket", "K-b")]
    assert S.select([], TICKETS) == []
    qs = S.select([{"kind": "questions"}], TICKETS)
    assert [(i.kind, i.payload["text"]) for i in qs] == [("question", "which bank?")]  # only the one to the owner


def test_a_summary_never_carries_more_than_type_title_deadline():
    (s,) = S.select([{"kind": "needs_you"}], TICKETS)
    assert set(s.payload) == {"kind", "type", "title", "deadline"}


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
    st.sent["ticket:a"] = {"item_id": "11" * 16, "digest": "d"}
    assert S.ShareState.from_json(st.to_json()).to_json() == st.to_json()
    for bad in (None, {}, {**st.to_json(), "rules": [{"kind": "x"}]}, {**st.to_json(), "sent": {"a": {"item_id": "zz"}}}):
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


def fake_build(item_id, item):
    return f"sealed:{item_id}:{item.logical}"


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


def test_the_sealed_format_gap_refuses_and_sends_nothing():
    st, link = S.ShareState(rules=[{"kind": "tag", "tag": "public"}]), FakeLink()
    with pytest.raises(HubRefused) as e:
        S.sync(st, S.select(st.rules, TICKETS), link=link, save=lambda s: None)
    assert e.value.code == "sealed_format_pending" and link.calls == [] and st.sent == {}


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
