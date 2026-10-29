"""Sharing can be taken away from the dashboard with one click: ``share.revoke`` and ``share.rule_remove`` are unsigned
owner actions like ``workspace.cosmetic.set``. The dashboard's workspace link is a column-P peer whose person is the
session's person; the host checks the owner role, refuses agents and other members, rate-limits the two ops and writes
a security-log line naming the person. Nothing else about the share ops changes (the CLI keeps its own confirmation:
tests/cli/test_hub_cli.py)."""

from __future__ import annotations

import pytest

from orch.host import securitylog, shareops
from tests.host import test_share_ops as S
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import err
from tests.host.test_share_ops import agent, labelled, req, rule_op, settle, sync_shown

lab, served, human, selective, quick_auto = S.lab, S.served, S.human, S.selective, S.quick_auto
dev_workspace, keystore = S.dev_workspace, S.keystore


@pytest.fixture
def log(monkeypatch):
    lines: list[tuple[str, dict]] = []
    real = securitylog.record

    def record(state_dir, host_uid, at, event, **fields):
        lines.append((event, fields))
        return real(state_dir, host_uid, at, event, **fields)

    monkeypatch.setattr(securitylog, "record", record)
    return lines


def test_the_reducing_ops_are_exactly_revoke_and_rule_remove():
    assert shareops.REDUCE_OPS == {"share.revoke", "share.rule_remove"}
    assert shareops.REDUCE_OPS < set(shareops.SHARE_OPS) and not shareops.REDUCE_OPS & shareops.SHARE_READS


def test_the_owner_revokes_and_removes_a_rule_as_their_person_with_a_log_line(served, selective, log):
    _, key = labelled(served, "Public note", "public")
    rule = {"kind": "tag", "tag": "public"}
    ok(rule_op(served, "share.rule_add", rule))
    got = ok(req(served, "share.revoke", {"ref": key}))["data"]
    assert got["revoke"] == 1 and selective.count("share_revoke") == 1
    again = ok(req(served, "share.revoke", {"ref": key}))["data"]  # idempotent: the same answer, nothing new sent
    assert again["revoke"] == 0 and selective.count("share_revoke") == 1
    ok(rule_op(served, "share.rule_remove", rule))
    members = ok(served.client().req("member.list"))["data"]["members"]
    (person,) = [m["person"] for m in members if m["role"] == "owner"]
    reduce_lines = [(e, f) for e, f in log if e in shareops.REDUCE_OPS]
    assert [e for e, _ in reduce_lines] == ["share.revoke", "share.revoke", "share.rule_remove"]
    assert all(f["person"] == person for _, f in reduce_lines)
    add = [f for e, f in log if e == "share.rule_add"]
    assert add and add[0]["person"] == person and add[0]["count"] == 1 and len(add[0]["plan"]) == 16


def test_a_rule_removal_that_would_match_more_shares_nothing_new(served, selective):
    """Review finding: removing a rule recomputes the selection; it must never confirm or send an item the owner has
    not confirmed (here a public ticket labelled after the last confirm), only take away what the rule shared."""
    labelled(served, "Public note", "public")
    labelled(served, "Misc note", "misc")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "misc"}))
    st = shareops._load(served.service)
    confirmed, sent = set(st.confirmed), set(st.sent)
    labelled(served, "Later public", "public")  # matches a rule, never confirmed
    prev = ok(req(served, "share.preview", {"remove": {"kind": "tag", "tag": "misc"}}))["data"]
    assert prev["new"] == 1 and "Later public" in str(prev["items"])  # the after-plan holds it (the dashboard shows it)
    before = list(selective.ops)
    got = ok(rule_op(served, "share.rule_remove", {"kind": "tag", "tag": "misc"}))["data"]
    assert got["put"] == 0 and got["revoke"] == 1
    assert [o for o, _ in selective.ops[len(before) :]] == ["share_revoke"]  # the hub receives only the revoke
    st = shareops._load(served.service)
    (gone,) = sent - set(st.sent)
    assert set(st.confirmed) == confirmed - {gone} and set(st.sent) <= sent
    assert "Later public" not in str([i["content"] for i in selective.opened])
    assert ok(req(served, "share.sync"))["data"]["put"] == 0  # nor does a later unconfirmed sync


def test_after_a_removal_auto_sync_never_sends_what_it_newly_matches_only_a_confirmed_sync_does(
    served, selective, quick_auto
):
    """B3 reduce-only: an item the remaining rules match but the owner never confirmed is not sent by the removal, nor
    by any later automatic pass (a ticket change triggers one) or plain sync; only an explicit confirmed sync does."""
    _, shared = labelled(served, "Public note", "public")
    labelled(served, "Misc note", "misc")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "misc"}))
    labelled(served, "Later public", "public")  # matches the remaining rule, never confirmed
    settle(served, quick_auto)
    ok(rule_op(served, "share.rule_remove", {"kind": "tag", "tag": "misc"}))
    ok(agent(served, "set", {"ref": shared, "pairs": ["title=Public, edited"]}))  # an automatic pass runs
    settle(served, quick_auto)
    assert ok(req(served, "share.sync"))["data"]["put"] == 0
    titles = [i["content"].get("title") for i in selective.opened]
    assert "Public, edited" in titles and "Later public" not in titles, titles
    assert sync_shown(served)["put"] == 1  # the owner's explicit, confirmed sync
    assert selective.opened[-1]["content"]["title"] == "Later public"


def test_no_rule_kind_excludes_so_removing_any_rule_never_selects_more():
    from orch.hubclient import shares as S
    from orch.hubclient.shares import NeedFact, QuestionFact, TicketFact

    assert set(S._RULE_KEYS) == shareops.SELECTING_KINDS  # a new kind must be reviewed as selecting
    assert set(shareops._RULE["properties"]["kind"]["enum"]) == shareops.SELECTING_KINDS
    facts = [
        TicketFact(f"01J9ZK000000000000000000T{n}", f"K-{n}", ty, "t", "open", labels, None,
                   (QuestionFact("Q1", "why?", True, "sha256:" + "a" * 64),), (NeedFact("question", "Q1", True),),
                   (), None, None)
        for n, (ty, labels) in enumerate([("bug", ("public",)), ("feature", ("misc",)), ("bug", ())])
    ]  # fmt: skip
    rules = [{"kind": "needs_you"}, {"kind": "questions"}, {"kind": "type", "type": "bug"},
             {"kind": "ticket", "ticket": "K-1"}, {"kind": "tag", "tag": "public"}]  # fmt: skip
    every = {i.logical for i in S.select(rules, facts)}
    assert every
    for r in rules:
        rest = [x for x in rules if x != r]
        assert {i.logical for i in S.select(rest, facts)} <= every, r
        assert not shareops._widens(rules, rest, facts)


def test_a_removal_that_would_select_more_is_refused_and_sends_nothing(served, selective, monkeypatch):
    """Defence in depth for (c): were a rule kind ever to exclude, removing it would widen; the reduce path refuses."""
    from orch.hubclient import shares as S

    labelled(served, "Public note", "public")
    labelled(served, "Hidden public", "secret")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "veil"}))
    real = S.select

    def select(rules, tickets):  # a pretend exclusion: `secret` tickets are selected unless a `veil` rule hides them
        rules, tickets = [dict(r) for r in rules], list(tickets)
        items = real([*rules, {"kind": "tag", "tag": "secret"}], tickets)
        if {"kind": "tag", "tag": "veil"} not in rules:
            return items
        hidden = {f"ticket:{t.uid}" for t in tickets if "secret" in t.labels}
        return [i for i in items if i.logical not in hidden]

    monkeypatch.setattr(S, "select", select)
    before, st = list(selective.ops), shareops._load(served.service)
    res = rule_op(served, "share.rule_remove", {"kind": "tag", "tag": "veil"})
    assert err(res) == "invalid.input" and "would share more" in res["error"]["message"]
    assert selective.ops == before and shareops._load(served.service).rules == st.rules


def test_the_owner_check_runs_again_at_the_write(served, selective, monkeypatch):
    """A role change between the worker's check and the write (off the worker) refuses the write."""
    _, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    real, before = shareops._run, list(selective.ops)

    def demoted(call, plan):
        def no(_call):
            raise shareops.HostError("role.denied", "share rules are for workspace owners")

        monkeypatch.setattr(shareops, "_owner", no)
        return real(call, plan)

    monkeypatch.setattr(shareops, "_run", demoted)
    assert err(req(served, "share.revoke", {"ref": key})) == "role.denied"
    assert selective.ops == before and not shareops._load(served.service).withdrawn


def test_the_owner_check_needs_column_p_a_direct_link_and_the_owner_role():
    from types import SimpleNamespace as NS

    def call(column="P", viewer="p_a", relay=None, role="owner"):
        members = {} if role is None else {"p_a": NS(role=role)}
        ws = NS(members=members)
        service = NS(store=NS(state=NS(workspace=ws)))
        return NS(column=NS(name=column), viewer=viewer, relay=relay, service=service)

    shareops._owner(call())
    for c, code in ((call(column="G"), "op.not_allowed"), (call(viewer=None), "op.not_allowed"),
                    (call(relay="phone"), "op.not_allowed"), (call(role="maintainer"), "role.denied"),
                    (call(role="viewer"), "role.denied"), (call(role=None), "role.denied")):  # fmt: skip
        with pytest.raises(shareops.HostError) as exc:
            shareops._owner(c)
        assert exc.value.code == code


def test_removing_a_rule_that_is_not_in_the_list_is_refused(served, selective):
    labelled(served, "Public note", "public")
    before = list(selective.ops)
    res = req(served, "share.rule_remove", {"rule": {"kind": "tag", "tag": "absent"}, "expect": "p" * 43})
    assert err(res) == "not_found" and "not in the list" in res["error"]["message"]
    assert selective.ops == before
    assert [r["kind"] for r in ok(req(served, "share.rules"))["data"]["rules"]] == ["needs_you"]


def test_revoke_still_withdraws_exactly_one_item(served, selective):
    _, key = labelled(served, "Public note", "public")
    labelled(served, "Other public", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    st = shareops._load(served.service)
    confirmed = set(st.confirmed)
    got = ok(req(served, "share.revoke", {"ref": key}))["data"]
    assert (got["put"], got["revoke"]) == (0, 1)
    st = shareops._load(served.service)
    assert len(st.sent) == 1 and set(st.confirmed) <= confirmed


def test_ten_reducing_changes_a_minute(served, selective):
    _, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    for _ in range(shareops.REDUCE_RATE):
        ok(req(served, "share.revoke", {"ref": key}))
    res = req(served, "share.revoke", {"ref": key})
    assert err(res) == "host.busy"
    assert ok(req(served, "share.rules"))["data"]["rules"]  # reads are not limited


def test_an_agent_cannot_take_sharing_away_either(served, selective):
    _, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    before = list(selective.ops)
    remove = {"rule": {"kind": "tag", "tag": "public"}, "expect": "p" * 43}
    for op, args in (("share.revoke", {"ref": key}), ("share.rule_remove", remove)):
        assert err(agent(served, op, args)) in ("op.not_allowed", "human_only"), op
    assert selective.ops == before


def test_a_member_who_is_not_the_owner_is_refused(tmp_path):
    from tests.host.live_helpers import world

    wd = world(tmp_path, map_to="quinn")  # the socket's person is a plain member
    try:
        for op, args in (("share.revoke", {"ref": "K-1"}),
                         ("share.rule_remove", {"rule": {"kind": "needs_you"}, "expect": "p" * 43})):  # fmt: skip
            assert err(wd.client().req(op, args, idem=True)) == "role.denied", op
    finally:
        wd.close()
        wd.lab.close()


def test_the_review_of_an_adding_change_comes_from_the_host_and_lists_only_new_items(served, selective):
    labelled(served, "Public note", "public")
    first = ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))  # confirmed and sent
    assert first
    labelled(served, "Second public", "public")
    got = ok(req(served, "share.preview", {"add": {"kind": "tag", "tag": "public"}}))["data"]
    review = got["review"]
    assert review["new"]["count"] == 1 and review["new"]["items"][0]["line"].endswith(": Second public")
    assert review["leaves"] == [{"kind": "ticket", "text": shareops.LEAVES["ticket"]}]
    assert (
        review["later_edits"] == shareops.LATER_EDITS
        and "hub_id" in review
        and "readers" in review
        and "widened" in review
    )


def test_five_adding_changes_a_minute_and_each_leaves_a_log_line(served, selective, log):
    labelled(served, "Public note", "public")
    for _n in range(5):
        ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    assert err(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"})) == "host.busy"
    assert len([e for e, _ in log if e == "share.rule_add"]) == 5
    assert ok(req(served, "share.rules", {}))  # reads are not limited


def test_an_agent_cannot_add_or_confirm_sharing(served, selective):
    labelled(served, "Public note", "public")
    before = list(selective.ops)
    for op, args in (("share.rule_add", {"rule": {"kind": "tag", "tag": "public"}, "expect": "p" * 43}),
                     ("share.sync", {"expect": "p" * 43})):  # fmt: skip
        assert err(agent(served, op, args)) in ("op.not_allowed", "human_only"), op
    assert selective.ops == before


def test_a_confirming_sync_checks_the_owner_again_under_the_lock(served, selective, monkeypatch):
    labelled(served, "Public note", "public")
    shown = ok(req(served, "share.preview", {}))["data"]["plan"]
    real = shareops._owner

    def demoted(call):  # the role changed after serve() let the call in
        if call.frame["op"] == "share.sync":
            raise shareops.HostError("role.denied", "share rules are for workspace owners")
        return real(call)

    seen = []
    real_serve = shareops.serve

    def serve(call):
        out = real_serve(call)
        monkeypatch.setattr(shareops, "_owner", demoted)
        seen.append(1)
        return out

    monkeypatch.setattr(shareops, "serve", serve)
    before = list(selective.ops)
    assert err(req(served, "share.sync", {"expect": shown})) == "role.denied"
    assert selective.ops == before and seen


# --- P02-sec: the confirmed grant (who may read, and the description) is part of what the owner confirmed -------------

U1 = "1" * 32
RULE_A = {"kind": "tag", "tag": "public", "audience": {"users": [U1], "groups": []}}
RULE_B = {"kind": "tag", "tag": "extra", "audience": {"users": [], "groups": ["team-b"]}, "description": True}


def widened_setup(served, selective):
    res = ok(agent(served, "new", {"title": "Public note", "message": "What this is about", "label": ["public"]}))
    key = res["key"]
    uid = served.lab.ws.uid(key)
    ok(rule_op(served, "share.rule_add", RULE_A))
    ok(rule_op(served, "share.rule_add", RULE_B))  # matches nothing yet
    assert [i.get("audience") for i in selective.opened] == [{"users": [U1], "groups": []}]
    ok(
        agent(served, "set", {"ref": key, "pairs": ["labels=extra"]})
    )  # now only rule B matches: new readers, and the text
    return uid, key


def test_a_group_or_description_that_appears_after_the_confirmation_is_held_back_and_shown_as_widened(
    served, selective
):
    widened_setup(served, selective)
    ok(req(served, "share.sync"))  # an automatic, unconfirmed pass: the new label may follow, the new readers not
    for item in selective.opened:
        assert item.get("audience") in (None, {"users": [U1], "groups": []}) and "description" not in item["content"]
    st = shareops._load(served.service)
    (grant,) = [g for lg, g in st.confirmed.items() if lg.startswith("ticket:")]
    assert grant["groups"] == [] and grant["description"] is False
    review = ok(req(served, "share.preview"))["data"]["review"]
    (w,) = review["widened"]
    assert w["teams_added"] == ["team-b"] and w["description_added"] is True
    assert review["readers"]["teams"] == ["team-b"]
    sync_shown(served)  # only the owner's confirmation widens
    item = selective.opened[-1]
    assert item["audience"] == {"users": [], "groups": ["team-b"]}
    assert "What this is about" in item["content"]["description"]


def test_narrowing_still_goes_through_automatically(served, selective):
    _uid, key = widened_setup(served, selective)
    sync_shown(served)  # the owner confirms the wider audience and the description
    assert selective.opened[-1]["audience"] == {"users": [], "groups": ["team-b"]}
    ok(agent(served, "set", {"ref": key, "pairs": ["labels=public,extra"]}))  # both rules match: readers U1 + team-b
    sync_shown(served)
    assert selective.opened[-1]["audience"] == {"users": [U1], "groups": ["team-b"]}
    got = ok(rule_op(served, "share.rule_remove", RULE_A))["data"]  # only B matches now: fewer readers, no new ones
    assert got["put"] == 1
    assert selective.opened[-1]["audience"] == {"users": [], "groups": ["team-b"]}


def test_a_review_never_opens_a_hub_link_for_names(monkeypatch):
    from types import SimpleNamespace

    from orch.host import shareops

    def boom(service):
        raise AssertionError("share.preview must not use the network")

    monkeypatch.setattr(shareops, "_audience", boom)
    monkeypatch.setattr(shareops, "_link", boom)
    assert shareops._names_for(SimpleNamespace(), {"a" * 32}) == {}  # cold: the review says "a person on the hub"
    assert shareops._names_for(SimpleNamespace(_share_names={"a" * 32: "Ben"}), {"a" * 32}) == {"a" * 32: "Ben"}


# --- a long review is capped, with the totals beside it (the dashboard refuses an over-long list) ------------------------


def _item(logical, users=(), groups=(), description=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        logical=logical, kind="ticket", audience={"users": list(users), "groups": list(groups)},
        payload={"description": "x"} if description else {}, line=lambda: f"line {logical}",
    )  # fmt: skip


def test_a_long_reader_list_is_capped_and_carries_its_total():
    from types import SimpleNamespace

    service = SimpleNamespace(_share_names={})
    users = [f"{n:032x}" for n in range(shareops.READERS_PEOPLE_CAP + 36)]
    groups = [f"team-{n:03d}" for n in range(shareops.READERS_TEAMS_CAP + 8)]
    got = shareops._readers(service, [_item("a", users, groups)])
    assert len(got["people"]) == shareops.READERS_PEOPLE_CAP and got["people_total"] == len(users)
    assert len(got["teams"]) == shareops.READERS_TEAMS_CAP and got["teams_total"] == len(groups)
    short = shareops._readers(service, [_item("a", users[:2], ["team-x"])])
    assert (short["people_total"], short["teams_total"]) == (2, 1) and len(short["people"]) == 2


def test_a_widened_list_is_capped_and_each_row_carries_its_added_totals():
    from types import SimpleNamespace

    service = SimpleNamespace(_share_names={})
    n = shareops.WIDENED_CAP + 5
    users = [f"{k:032x}" for k in range(shareops.ADDED_PEOPLE_CAP + 3)]
    items = [_item(f"ticket:{k}", users, ["t-a"], True) for k in range(n)]
    st = SimpleNamespace(sent=[i.logical for i in items], confirmed=None)
    out = shareops._widened(service, st, items)
    assert len(out) == shareops.WIDENED_CAP
    assert len(out[0]["people_added"]) == shareops.ADDED_PEOPLE_CAP and out[0]["people_added_total"] == len(users)
    assert out[0]["teams_added_total"] == 1
