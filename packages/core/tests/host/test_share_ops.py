"""V3 C6: the host's share ops on a SELECTIVE workspace joined to a TLS test hub that records ``share_put`` /
``share_revoke``: owner-only edits, evaluation on the real tickets, revoke on rule removal, full mode refused, and the
default (needs-you summaries only). Every put is opened with the test-side port of the hub's 10.2b check."""

from __future__ import annotations

import pytest

from orch import crypto
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.human_helpers import err
from tests.host.test_hub_join_real import genesis, make_card, now_ms
from tests.host.test_hub_ops import keystore  # noqa: F401
from tests.hub import share_open as SO
from tests.hub.tlshub import TlsHub

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
HXK = crypto.generate_private_key()  # the test hub's exchange key: the card carries its public half


class ShareHub:
    def __init__(self, wsk_pub):
        self.ops: list[tuple[str, dict]] = []
        self.opened: list[dict] = []  # every put, opened and checked like the hub does (contract 10.2b)
        self.wsk_pub = wsk_pub
        self.workspace_id = ""

    def handle(self, h, o):
        if o["op"] in ("share_put", "share_revoke"):
            self.ops.append((o["op"], o["body"]))
        if o["op"] == "share_put":
            self.opened.append(
                SO.open_share_item(
                    o["body"],
                    hub_id=h.hub_id,
                    workspace_id=self.workspace_id,
                    hxk_priv=HXK,
                    wsk_pub=self.wsk_pub,
                    dev_mode=True,
                )["item"]  # fmt: skip
            )
        return h.reply(o, body={}, ts_ms=o["ts_ms"])

    def count(self, op):
        return sum(1 for o, _ in self.ops if o == op)


def _join(served, human, mode):
    wsk = crypto.unb64u(genesis(served)["wsk_pub"], 65)
    sh = ShareHub(wsk)
    sh.workspace_id = served.wid
    hub = TlsHub(wsk_pub=wsk, on_request=sh.handle)
    hub.workspace_id = served.wid
    hub.start()
    card = make_card(hub, now_ms(served), HXK)
    human_helpers.sync_dev_store(served)  # the World of live_helpers does not come from the ``served`` fixture
    human.run("hub.join", {"hub_card": card, "mode": mode})
    args = {"invite": "invite-1", "hub_card": card, "addr": f"127.0.0.1:{hub.port}"}
    assert ok(served.client().req("hub.connect", args, idem=True))["data"]["status"] == "bound"
    return hub, sh


@pytest.fixture(autouse=True)
def dev_workspace(monkeypatch):
    from orch.host import hubops

    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)


@pytest.fixture
def selective(human, served):
    hub, sh = _join(served, human, "selective")
    try:
        yield sh
    finally:
        hub.close()


@pytest.fixture
def full(human, served, keystore):  # noqa: F811
    from orch.host import peerops

    hub, sh = _join(served, human, "full")
    try:
        yield sh
    finally:
        for svc in served.h.host.services.values():
            peerops.stop(svc)
        hub.close()


def req(served, op, args=None, **kw):
    return served.client().req(op, args or {}, idem=True if op not in ("share.rules", "share.preview") else None, **kw)


def rule_op(served, op, rule):
    """A rule change as the CLI sends it: preview first, then the op with the plan that was shown."""
    prev = ok(req(served, "share.preview", {("add" if op.endswith("add") else "remove"): rule}))["data"]
    return req(served, op, {"rule": rule, "expect": prev["plan"]})


def sync_shown(served):
    """The owner's explicit sync: preview first, then sync exactly the plan that was shown."""
    prev = ok(req(served, "share.preview"))["data"]
    return ok(req(served, "share.sync", {"expect": prev["plan"]}))["data"]


def agent(served, op, args):
    return served.client().req(op, args, grant=served.grant, session=SESSION, idem=True)


def labelled(served, title, label):
    uid, key = served.ticket(title)
    ok(agent(served, "set", {"ref": key, "pairs": [f"labels={label}"]}))
    return uid, key


def test_the_ops_are_declared():
    from orch.host import shareops

    assert shareops.SHARE_READS == {"share.rules", "share.preview"}
    assert set(shareops.SHARE_OPS) == {
        "share.rules", "share.preview", "share.rule_add", "share.rule_remove", "share.revoke", "share.sync",
        "share.audience",
    }  # fmt: skip


def test_without_a_hub_every_share_op_is_hub_not_joined(served):
    assert err(req(served, "share.rules")) == "hub.not_joined"


def test_full_mode_shares_a_ticket_with_named_people(served, full):
    uid, key = labelled(served, "Public note", "public")
    served.ticket("Plain list")
    rule = {"kind": "ticket", "ticket": key, "audience": {"users": ["b" * 32], "groups": []}}
    prev = ok(req(served, "share.preview", {"add": rule}))["data"]
    assert prev["review"]["mode"] == "full" and "already holds this workspace" in prev["review"]["scope_short"]
    got = ok(req(served, "share.rule_add", {"rule": rule, "expect": prev["plan"]}))["data"]
    assert got["synced"] is True and got["put"] == 1 and full.count("share_put") == 1
    (item,) = full.opened
    assert item["item_kind"] == "ticket" and item["audience"]["users"] == ["b" * 32] and item["chain"]["log"] == uid
    assert ok(req(served, "share.revoke", {"ref": key}))["data"]["revoke"] == 1 and full.count("share_revoke") == 1


def test_full_mode_refuses_summaries_and_a_plan_that_was_not_shown(served, full):
    for kind in ("needs_you", "questions"):
        res = req(served, "share.preview", {"add": {"kind": kind}})
        assert err(res) == "invalid.input" and "already sees what needs you" in res["error"]["message"]
    _, key = labelled(served, "Public note", "public")
    rule = {"kind": "ticket", "ticket": key}
    assert err(req(served, "share.rule_add", {"rule": rule, "expect": "p" * 43})) == "invalid.input"
    assert full.ops == []
    rules = ok(req(served, "share.rules"))["data"]["rules"]
    assert all(r["kind"] != "needs_you" for r in rules)


def test_full_mode_default_rule_sends_no_summary(served, full):
    served.ticket("Plain list")
    assert ok(req(served, "share.sync"))["data"]["put"] == 0 and full.ops == []


def test_a_member_and_an_agent_cannot_share_in_full_mode(served, full):
    _, key = labelled(served, "Public note", "public")
    res = agent(served, "share.rule_add", {"rule": {"kind": "ticket", "ticket": key}})
    assert err(res) in ("op.not_allowed", "human_only") and full.ops == []


def test_a_person_who_is_not_an_owner_cannot_share_in_full_mode(served, full, monkeypatch):
    from orch.host import shareops

    _, key = labelled(served, "Public note", "public")
    rule = {"kind": "ticket", "ticket": key}
    prev = ok(req(served, "share.preview", {"add": rule}))["data"]

    def no(_call):
        raise shareops.HostError("role.denied", "share rules are for workspace owners")

    monkeypatch.setattr(shareops, "_owner", no)  # the person's role is not owner
    assert err(req(served, "share.rule_add", {"rule": rule, "expect": prev["plan"]})) == "role.denied"
    assert full.ops == []


def test_a_hub_log_mode_mismatch_is_refused(served, selective, monkeypatch):
    from orch.host import peerops

    real = peerops._hub_state

    def lying(service, **kw):
        st = real(service, **kw)
        st.mode = "full"  # hub.json says full, the signed log says selective
        return st

    monkeypatch.setattr(peerops, "_hub_state", lying)
    assert err(req(served, "share.rules")) == "hub.state_mismatch"


def test_unbind_through_the_host_forgets_what_was_sent(served, selective, human):
    from orch.host import shareops

    _, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "ticket", "ticket": key}))
    svc = served.h.host.services[served.wid]
    assert shareops._load(svc).sent  # noqa: SLF001
    human.run("hub.leave", {})
    ok(served.client().req("hub.unbind", {}, idem=True))
    st = shareops._load(svc)  # noqa: SLF001
    assert not st.sent and st.confirmed is None and st.rules


def test_after_a_switch_to_full_the_automatic_pass_revokes_summaries_and_keeps_the_ticket(
    served, selective, monkeypatch
):
    from orch.host import shareops

    _, key = labelled(served, "Public note", "public")
    ok(agent(served, "ask", {"ref": key, "text": "which bank?"}))
    ok(rule_op(served, "share.rule_add", {"kind": "ticket", "ticket": key}))  # + the default needs-you summary
    svc = served.h.host.services[served.wid]
    sent = set(shareops._load(svc).sent)  # noqa: SLF001
    assert any(lg.startswith("ticket:") for lg in sent) and any(not lg.startswith("ticket:") for lg in sent)
    before = selective.count("share_revoke")
    monkeypatch.setattr(shareops, "_is_full", lambda service: True)  # the log now says full
    monkeypatch.setattr(shareops, "_bound_hub", lambda service: _ok_state(service))
    shareops.auto_sync(svc)
    left = set(shareops._load(svc).sent)  # noqa: SLF001
    assert left and all(lg.startswith("ticket:") for lg in left)
    assert selective.count("share_revoke") > before


def _ok_state(service):
    from orch.host import peerops

    return peerops._hub_state(service, full=False)  # noqa: SLF001


def test_an_agent_cannot_read_or_change_the_rules(served, selective):
    for op, args in (("share.rules", {}), ("share.rule_add", {"rule": {"kind": "tag", "tag": "private"}}),
                     ("share.rule_remove", {"rule": {"kind": "needs_you"}}), ("share.sync", {}),
                     ("share.revoke", {"ref": "K-1"})):  # fmt: skip
        res = agent(served, op, args)
        assert err(res) in ("op.not_allowed", "human_only"), (op, res)
        assert "tickets_state" not in res.get("data", {})
        assert "not_sent_count" not in res.get("data", {})
    assert selective.ops == []
    rules = ok(req(served, "share.rules"))["data"]["rules"]
    assert [r["kind"] for r in rules] == ["needs_you"]


def test_default_shares_only_needs_you_summaries(served, selective):
    uid, key = labelled(served, "Public note", "public")
    ok(agent(served, "ask", {"ref": key, "text": "which bank?"}))
    prev = ok(req(served, "share.preview"))["data"]
    assert sorted(i["kind"] for i in prev["items"]) == ["question", "summary"]
    assert "Public note" not in str(prev["items"])
    from orch.hubclient import shares as S

    assert prev["rules"] == [S.describe({"kind": "needs_you"})] and "2000 bytes" in prev["rules"][0]


def test_preview_shows_what_a_rule_would_add_without_changing_anything(served, selective):
    labelled(served, "Public note", "public")
    labelled(served, "Private review", "private")
    prev = ok(req(served, "share.preview", {"add": {"kind": "tag", "tag": "public"}}))["data"]
    assert prev["count"] == 1 and "Public note" in prev["items"][0]["line"] and "Private" not in str(prev)
    assert [r["kind"] for r in ok(req(served, "share.rules"))["data"]["rules"]] == ["needs_you"]


def test_a_rule_change_needs_the_plan_the_owner_was_shown(served, selective, monkeypatch):
    labelled(served, "Public note", "public")
    rule = {"kind": "tag", "tag": "public"}
    shown = ok(req(served, "share.preview", {"add": rule}))["data"]["plan"]
    labelled(served, "Another public note", "public")  # the selection moved after the owner looked
    res = req(served, "share.rule_add", {"rule": rule, "expect": shown})
    assert err(res) == "invalid.input" and "not what you were shown" in res["error"]["message"]
    assert selective.ops == [] and len(ok(req(served, "share.rules"))["data"]["rules"]) == 1


def test_the_sync_sends_exactly_the_confirmed_selection_even_if_a_ticket_changes_meanwhile(
    served, selective, monkeypatch
):
    """C6-2: the confirmed plan is the selection that is sent; a ticket that starts to match a moment later (here: a
    second public ticket appears on every later read of the tickets) is not shared by this confirmation."""
    import dataclasses

    from orch.host import shareops

    labelled(served, "Public note", "public")
    rule = {"kind": "tag", "tag": "public"}
    shown = ok(req(served, "share.preview", {"add": rule}))["data"]["plan"]
    real, calls = shareops.tickets_for, []

    def moving(service, viewer=None):
        facts = real(service, viewer)
        calls.append(1)
        if len(calls) > 1:  # any later read sees one more matching ticket
            facts.append(dataclasses.replace(facts[0], uid="01J9ZP0000000000000000MOVE", key="MOVE-1", title="Later"))
        return facts

    monkeypatch.setattr(shareops, "tickets_for", moving)
    got = ok(req(served, "share.rule_add", {"rule": rule, "expect": shown}))["data"]
    assert got["synced"] is True and got["put"] == 1 and selective.count("share_put") == 1
    assert [i["content"]["title"] for i in selective.opened] == ["Public note"]


def test_rule_add_puts_exactly_the_matching_ticket_then_remove_revokes_once(served, selective, monkeypatch):
    uid, _ = labelled(served, "Public note", "public")
    labelled(served, "Private review", "private")
    served.ticket("Plain list")
    rule = {"kind": "tag", "tag": "public"}
    got = ok(rule_op(served, "share.rule_add", rule))["data"]
    assert got["synced"] is True and got["put"] == 1
    assert selective.count("share_put") == 1
    ((_, body),) = selective.ops
    (item,) = selective.opened  # the hub opened it: HXK-sealed, WSK-signed, exact content
    assert item["item_kind"] == "ticket" and item["item_version"] == 1 and item["dev"] is True
    assert item["content"]["title"] == "Public note" and item["content"]["labels"] == ["public"]
    assert item["chain"]["log"] == uid and item["chain"]["seq"] >= 1
    again = ok(req(served, "share.sync"))["data"]
    assert (again["put"], again["revoke"]) == (0, 0) and selective.count("share_put") == 1
    got = ok(rule_op(served, "share.rule_remove", rule))["data"]
    assert got["revoke"] == 1 and selective.count("share_revoke") == 1
    assert selective.ops[-1][1]["item_id"] == body["item_id"]
    assert ok(req(served, "share.sync"))["data"]["revoke"] == 0 and selective.count("share_revoke") == 1


def test_revoke_withdraws_one_item_and_it_stays_withdrawn(served, selective, monkeypatch):
    _, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    got = ok(req(served, "share.revoke", {"ref": key}))["data"]
    assert got["revoke"] == 1 and "cannot be taken back" in got["note"]
    assert ok(req(served, "share.sync"))["data"]["put"] == 0 and selective.count("share_put") == 1
    assert err(req(served, "share.revoke", {"ref": "NOPE-1"})) == "not_found"


def test_a_question_to_the_owner_is_a_summary_and_with_a_rule_the_full_question(served, selective, monkeypatch):
    _, key = served.ticket("Savings")
    ok(agent(served, "ask", {"ref": key, "text": "which bank?"}))
    got = sync_shown(served)
    # V3-D31: the needs-you summary has no f1 (never answerable); the question item carries the full text and the f1
    assert got["put"] == 2
    (summ,) = [i for i in selective.opened if i["item_kind"] == "needs_you"]
    assert (
        summ["content"]["type"] == "question" and summ["content"]["title"] == "which bank?" and summ.get("f1") is None
    )
    (q,) = [i for i in selective.opened if i["item_kind"] == "question"]
    assert set(q["f1"]["questions"]) == {"Q1"}
    ok(rule_op(served, "share.rule_add", {"kind": "questions"}))
    assert len([i for i in selective.opened if i["item_kind"] == "question"]) == 1  # the same item: nothing re-sent
    assert q["content"]["text"] == "which bank?" and q["content"]["id"] == "Q1"


def test_a_rule_survives_an_unreachable_hub_and_the_next_sync_finishes(served, selective, monkeypatch):
    labelled(served, "Public note", "public")
    monkeypatch.setattr("orch.host.shareops._link", lambda service: _Down())
    got = ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))["data"]
    assert got["synced"] is False and got["pending"] == "hub_unreachable" and selective.ops == []
    monkeypatch.undo()
    from orch.host import hubops

    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)
    assert ok(req(served, "share.sync"))["data"]["put"] == 1  # the rule was confirmed; a plain sync finishes it


class _Down:
    def call(self, op, body):
        raise OSError("down")

    def close(self):
        pass


def test_the_item_version_grows_with_every_change_and_revoke_twice_is_the_same_answer(served, selective):
    uid, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Renamed note"]}))
    assert sync_shown(served)["put"] == 1
    assert [i["item_version"] for i in selective.opened] == [1, 2]
    assert selective.opened[1]["content"]["title"] == "Renamed note"
    assert selective.opened[0]["item_id"] == selective.opened[1]["item_id"]
    first = ok(req(served, "share.revoke", {"ref": key}))["data"]
    again = ok(req(served, "share.revoke", {"ref": key}))["data"]  # idempotent: same answer, no error
    assert first["revoke"] == 1 and again["revoke"] == 0 and selective.count("share_revoke") == 1


def test_c6r3_2_the_chain_head_and_the_ticket_snapshot_are_read_under_one_lock(served, selective, monkeypatch):
    """The chain claim of an item is read with its content, inside one ``store.locked()``; the builder no longer
    reads the log head later (a commit in between would have bound seq N content to head N+1)."""
    import contextlib

    from orch.host import shareops

    uid, _ = labelled(served, "Public note", "public")
    store = served.h.host.services[served.wid].store
    depth, reads = [0], []
    real_locked, real_seq, real_head = store.locked, store.log_seq, store.head_at

    @contextlib.contextmanager
    def locked():
        depth[0] += 1
        try:
            with real_locked():
                yield
        finally:
            depth[0] -= 1

    def seq(log):
        reads.append(("seq", depth[0] > 0))
        return real_seq(log)

    def head(log, n):
        reads.append(("head", depth[0] > 0))
        return real_head(log, n)

    monkeypatch.setattr(store, "locked", locked)
    monkeypatch.setattr(store, "log_seq", seq)
    monkeypatch.setattr(store, "head_at", head)
    facts = {t.uid: t for t in shareops.tickets_for(served.h.host.services[served.wid])}
    assert reads and all(held for _, held in reads), reads
    assert facts[uid].chain == (real_seq(uid), real_head(uid, real_seq(uid)))
    monkeypatch.undo()
    from orch.host import hubops

    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)
    got = ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))["data"]
    assert got["put"] == 1
    assert selective.opened[0]["chain"]["seq"] == real_seq(uid)


# --- automatic sync on ticket change (C6 follow-up) -----------------------------------------------------------------


@pytest.fixture
def quick_auto(monkeypatch):
    from orch.host import shareauto

    monkeypatch.setattr(shareauto, "DEBOUNCE_S", 0.1)
    monkeypatch.setattr(shareauto, "MAX_WAIT_S", 1.0)
    monkeypatch.setattr(shareauto, "RETRY_MIN_S", 0.2)
    return shareauto


class _Manual:
    """A manual clock for shareauto: nothing happens until the test steps it, and a step waits for its pass."""

    def __init__(self, shareauto):
        self.shareauto = shareauto
        self.t = 1000.0

    def now(self):
        return self.t

    def step(self, served, seconds):
        a = self.shareauto.state_of(served.h.host.services[served.wid])
        assert a is not None, "the auto thread is not armed"
        with a.cv:
            before, due = a.passes, a.dirty_since is not None
        self.t += seconds
        self.shareauto.tick()
        if due:
            with a.cv:  # a pass is due only if its deadline has been reached; wait for it, or for a quiet thread
                a.cv.wait_for(lambda: a.passes > before or self._not_yet(a), timeout=30)
        with a.cv:
            return a

    def _not_yet(self, a):
        if a.dirty_since is None:
            return False
        due = max(
            min(a.last_touch + self.shareauto.DEBOUNCE_S, a.dirty_since + self.shareauto.MAX_WAIT_S), a.not_before
        )
        return self.t < due


@pytest.fixture
def manual_auto(monkeypatch):
    from orch.host import shareauto

    m = _Manual(shareauto)
    monkeypatch.setattr(shareauto, "_CLOCK", m.now)
    monkeypatch.setattr(shareauto, "_TIMED", False)
    return m


def settle(served, quick, *, timeout=10.0):
    """Wait until a finished auto pass has seen every commit made so far, and the thread is idle.

    Not "one more pass than now": the pass a commit wakes may already have finished when this is called (debounce
    0.1 s, a slow test thread), and then no further pass ever comes (CI flake 29 Oct, "passes=3, wanted 4")."""
    import time

    svc = served.h.host.services[served.wid]
    a = quick.state_of(svc)
    assert a is not None, "the auto thread is not armed"
    with a.cv:
        target = a.marks  # every commit so far has marked (on_commit runs inside the commit)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with a.cv:
            if a.covered >= target and a.dirty_since is None:
                return a
        time.sleep(0.02)
    raise AssertionError(f"no auto pass covered the commits (covered={a.covered}, marks={target}, passes={a.passes})")


def test_an_edit_to_a_shared_ticket_goes_out_as_a_higher_version_without_a_sync(served, selective, quick_auto):
    uid, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    assert [i["item_version"] for i in selective.opened] == [1]
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Renamed note"]}))
    settle(served, quick_auto)
    assert [i["item_version"] for i in selective.opened] == [1, 2]
    assert selective.opened[1]["content"]["title"] == "Renamed note"
    assert selective.opened[0]["chain"]["seq"] < selective.opened[1]["chain"]["seq"]
    assert selective.count("share_revoke") == 0


def test_a_burst_of_edits_is_one_debounced_pass(served, selective, manual_auto):
    """On the manual clock: the edits come half a debounce apart, so each one moves the deadline and only the quiet
    after the last one lets the pass run. (On the wall clock with a 0.5 s debounce, a test thread slower than 0.5 s
    between two edits made a second pass, CI flake 29 Oct "[1, 2, 3] == [1, 2]".)"""
    from orch.host import shareauto

    _, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    for n in range(4):
        ok(agent(served, "set", {"ref": key, "pairs": [f"title=Edit {n}"]}))
        a = manual_auto.step(served, shareauto.DEBOUNCE_S / 2)  # inside the debounce: no pass yet
        assert a.passes == 0
    a = manual_auto.step(served, shareauto.DEBOUNCE_S)  # quiet for a whole debounce: the one pass
    assert a.passes == 1
    assert [i["item_version"] for i in selective.opened] == [1, 2]
    assert selective.opened[1]["content"]["title"] == "Edit 3"


def test_settle_also_counts_a_pass_that_finished_before_it_was_called(served, selective, quick_auto):
    """Repro of the settle race (CI 29 Oct, "no auto pass finished (passes=3, wanted 4)"): the pass a commit woke can
    finish before the test starts to wait; settle must still return."""
    _, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    a = quick_auto.state_of(served.h.host.services[served.wid])
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Renamed note"]}))
    with a.cv:  # the pass has run and the thread is idle before settle is called
        assert a.cv.wait_for(lambda: a.covered >= a.marks and a.dirty_since is None, timeout=10)
    settle(served, quick_auto, timeout=2.0)
    assert [i["item_version"] for i in selective.opened] == [1, 2]


def test_a_ticket_that_stops_matching_is_revoked(served, selective, quick_auto):
    _, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    ok(agent(served, "set", {"ref": key, "pairs": ["labels=private"]}))
    settle(served, quick_auto)
    assert selective.count("share_revoke") == 1 and selective.count("share_put") == 1


def test_a_ticket_that_starts_matching_after_the_confirmation_is_not_auto_shared(served, selective, quick_auto):
    """The owner confirmed what the preview showed (C6-2). A later match waits for the owner's next confirm, however
    it came about (a new ticket, a label an agent adds, a question an agent asks); an explicit sync then sends it."""
    _, shared = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    _, plain = served.ticket("Plain note")
    ok(agent(served, "set", {"ref": plain, "pairs": ["labels=public"]}))  # starts matching: an agent widens
    labelled(served, "Brand new public", "public")  # a new ticket that matches
    ok(agent(served, "ask", {"ref": plain, "text": "which bank?"}))  # a new needs-you summary (default rule)
    ok(agent(served, "set", {"ref": shared, "pairs": ["title=Renamed"]}))  # the one thing the pass may send
    settle(served, quick_auto)
    assert [i["content"]["title"] for i in selective.opened] == ["Public note", "Renamed"]
    assert selective.count("share_put") == 2
    got = sync_shown(served)  # the owner's next confirm shares what matches now
    assert got["put"] == 4 and selective.count("share_put") == 6  # plain: summary + its question item
    ok(agent(served, "set", {"ref": plain, "pairs": ["title=Plain, edited"]}))  # now confirmed: it follows
    settle(served, quick_auto)
    assert selective.opened[-1]["content"]["title"] == "Plain, edited"


def test_an_item_that_left_and_comes_back_needs_a_new_confirm(served, selective, quick_auto):
    _, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    ok(agent(served, "set", {"ref": key, "pairs": ["labels=private"]}))
    settle(served, quick_auto)
    ok(agent(served, "set", {"ref": key, "pairs": ["labels=public"]}))
    settle(served, quick_auto)
    assert selective.count("share_put") == 1 and selective.count("share_revoke") == 1


def test_full_mode_default_rule_pass_sends_nothing_when_the_thread_runs(served, full, quick_auto):
    from orch.host import shareauto

    svc = served.h.host.services[served.wid]
    _, key = labelled(served, "Public note", "public")
    shareauto.arm(svc)
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Renamed note"]}))
    settle(served, quick_auto)
    assert full.ops == []  # the default needs-you rule never leaves in full mode


def test_no_pass_before_a_selective_join(served, quick_auto):
    from orch.host import shareauto

    svc = served.h.host.services[served.wid]
    _, key = labelled(served, "Public note", "public")
    shareauto.arm(svc)
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Renamed note"]}))
    a = settle(served, quick_auto)
    assert a.parked and "hub.not_joined" in str(a.last)


def test_an_unreachable_hub_is_retried_and_the_edit_arrives_later(served, selective, manual_auto, monkeypatch):
    from orch.host import hubops, shareauto, shareops

    _, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    real = shareops._link
    monkeypatch.setattr(shareops, "_link", lambda service: _Down())
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Renamed note"]}))
    a = manual_auto.step(served, shareauto.DEBOUNCE_S)  # the debounce elapses: one pass, the hub is down
    assert a.last["synced"] is False and selective.count("share_put") == 1
    monkeypatch.setattr(shareops, "_link", real)
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)
    manual_auto.step(served, shareauto.RETRY_MIN_S - 1)  # still inside the backoff: no retry
    assert len(selective.opened) == 1
    manual_auto.step(served, 1)  # the backoff ends: the retry
    assert [i["content"]["title"] for i in selective.opened] == ["Public note", "Renamed note"]


def test_a_refused_update_is_visible_to_the_owner_and_marks_the_hub_copy_stale(
    served, selective, quick_auto, monkeypatch
):
    from orch.host import shareops
    from orch.hubclient.errors import HubRefused

    _, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    real = shareops._link

    class Refusing:
        def __init__(self, service):
            self.inner = real(service)

        def call(self, op, body):
            if op == "share_put":
                raise HubRefused("too_large")
            return self.inner.call(op, body)

        def close(self):
            self.inner.close()

    monkeypatch.setattr(shareops, "_link", Refusing)
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Renamed note"]}))
    settle(served, quick_auto)
    want = [{"item": f"ticket {key}: Renamed note", "code": "too_large", "stale": True}]
    assert ok(req(served, "share.rules"))["data"]["refused_items"] == want  # shown to the owner, not only in --json
    got = ok(req(served, "share.sync"))["data"]
    assert got["refused_items"] == want and got["refused"] and selective.count("share_put") == 1


def test_a_restricted_ticket_is_never_in_a_preview_or_a_share_even_for_the_owner(served, selective):
    uid, _ = labelled(served, "Secret plan", "public")
    served.restrict(uid, [served.lab.owner])
    labelled(served, "Open note", "public")
    prev = ok(req(served, "share.preview", {"add": {"kind": "tag", "tag": "public"}}))["data"]
    assert prev["count"] == 1 and "Open note" in str(prev) and "Secret" not in str(prev)


def test_shareable_is_the_viewers_workspace_visible_tickets_only():
    from types import SimpleNamespace as NS

    from orch.host import shareops

    pub, secret = NS(visibility="workspace"), NS(visibility={"restricted": ["p_a"]})
    tickets = {"u1": pub, "u2": secret}
    state = NS(
        tickets=tickets,
        visible_to=lambda who: {u: t for u, t in tickets.items() if t is pub or who in t.visibility["restricted"]},
    )
    assert shareops._shareable(state, "p_a") == {"u1": pub}  # noqa: SLF001  (A's own restricted ticket stays out)
    assert shareops._shareable(state, "p_b") == {"u1": pub}  # noqa: SLF001
    assert shareops._shareable(state, None) == {"u1": pub}  # noqa: SLF001


# --- l2-c14 adversarial (Opus security review of the C6 follow-ups, 22 Oct) -----------------------------------------


def test_l2_c6fu_1_a_ticket_restricted_after_the_confirmation_is_not_auto_sent(served, selective, quick_auto):
    """L2-C6fu-1 (P1): the owner confirmed a workspace-visible ticket. Someone then restricts it (a strong "this is
    sensitive now" signal) and its title changes. An automatic pass must not push the restricted ticket's new content to
    the hub (it should withdraw it, or hold it for the owner's confirm): the owner never confirmed sharing a restricted
    ticket, and the hub's share readers are not on its list."""
    uid, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    assert selective.count("share_put") == 1
    served.restrict(uid, [served.lab.owner])
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Merger with ACME, price 4.2M"]}))  # the owner's agent sees it
    _, other = served.ticket("Unrelated")
    ok(agent(served, "set", {"ref": other, "pairs": ["title=Unrelated, edited"]}))  # any commit wakes the pass
    settle(served, quick_auto)
    sent_titles = [i["content"].get("title") for i in selective.opened]
    assert "Merger with ACME, price 4.2M" not in sent_titles, sent_titles


# --- c6fu2: restricted-after-confirm withdrawal, audit of auto passes, share.sync needs the shown plan ---


def _audit_lines(served, event):
    import json

    path = served.lab.state / "host" / "audit-security.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rows if r["event"] == event]


def test_a_ticket_restricted_after_the_confirmation_is_revoked_and_needs_a_new_confirm(served, selective, quick_auto):
    uid, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    served.restrict(uid, [served.lab.owner])
    _, other = served.ticket("Unrelated")
    ok(agent(served, "set", {"ref": other, "pairs": ["title=Unrelated, edited"]}))  # any commit wakes the pass
    settle(served, quick_auto)
    assert selective.count("share_revoke") == 1  # withdrawn at the hub, no owner act
    served.restrict(uid, None)  # shareable again
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Public again"]}))
    settle(served, quick_auto)
    assert selective.count("share_put") == 1 and selective.count("share_revoke") == 1  # waits for the owner
    assert sync_shown(served)["put"] == 1 and selective.opened[-1]["content"]["title"] == "Public again"


def test_an_automatic_pass_that_changes_the_hubs_copy_writes_an_audit_entry(served, selective, quick_auto):
    uid, key = labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    assert _audit_lines(served, "share.auto") == []
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Renamed note"]}))
    settle(served, quick_auto)
    (row,) = _audit_lines(served, "share.auto")
    assert row["put"] == [f"ticket:{uid}"] and row["revoked"] == [] and "Renamed" not in str(row)
    ok(agent(served, "set", {"ref": key, "pairs": ["labels=private"]}))
    settle(served, quick_auto)
    assert [r["revoked"] for r in _audit_lines(served, "share.auto")][-1] == [f"ticket:{uid}"]


def test_a_plain_sync_confirms_nothing_new_and_a_stale_plan_is_refused(served, selective):
    labelled(served, "Public note", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    labelled(served, "Brand new public", "public")
    assert ok(req(served, "share.sync"))["data"]["put"] == 0  # not shown to the owner: not sent
    prev = ok(req(served, "share.preview"))["data"]
    labelled(served, "Another public", "public")  # appears after the preview
    assert err(req(served, "share.sync", {"expect": prev["plan"]})) == "invalid.input"
    assert selective.count("share_put") == 1
    assert sync_shown(served)["put"] == 2  # the two new ones, now shown


# --- l2-c6fud adversarial (Opus delta of core#1030 052da4f5, 23 Oct) --------------------------------------------------


def test_l2_c6fud_1_revoking_one_item_does_not_confirm_unseen_new_matches(served, selective, quick_auto):
    """L2-C6fud-1: the owner revokes one shared item. A ticket that started matching after the last confirm (an agent
    labelled it) was never shown to the owner; the revoke must not confirm or send it (same class as L2-C6fu-3)."""
    _, first = labelled(served, "Public note", "public")
    labelled(served, "Second public", "public")
    ok(rule_op(served, "share.rule_add", {"kind": "tag", "tag": "public"}))
    assert selective.count("share_put") == 2
    _, unseen = served.ticket("Agent wrote this later")
    ok(agent(served, "set", {"ref": unseen, "pairs": ["labels=public"]}))
    settle(served, quick_auto)
    assert selective.count("share_put") == 2  # the auto pass holds it back (correct)
    ok(req(served, "share.revoke", {"ref": first}))
    titles = [i["content"].get("title") for i in selective.opened]
    assert "Agent wrote this later" not in titles, titles


def test_guard_every_caller_of_share_state_confirm_passes_a_checked_plan():
    """C6fud-1, structural: ``ShareState.confirm`` is the only way to widen what automatic passes may send. Its only
    caller is ``shareops._confirm`` (which maps a mismatch to invalid.input) and it always passes ``expect``."""
    import ast
    from pathlib import Path

    import orch

    callers = []
    root = Path(orch.__file__).parent
    for path in [*(root / "host").rglob("*.py"), *(root / "hubclient").rglob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "confirm":
                callers.append((path.name, len(node.args) + len(node.keywords)))
    assert callers == [("shareops.py", 2)], callers
