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
    }  # fmt: skip


def test_without_a_hub_every_share_op_is_hub_not_joined(served):
    assert err(req(served, "share.rules")) == "hub.not_joined"


def test_full_mode_refuses_every_share_op(served, full):
    for op, args in (("share.rules", {}), ("share.rule_add", {"rule": {"kind": "tag", "tag": "x"}}),
                     ("share.sync", {}), ("share.revoke", {"ref": "K-1"})):  # fmt: skip
        res = req(served, op, args)
        assert err(res) == "hub.refused" and "wrong_mode" in res["error"]["message"], op
    assert full.ops == []


def test_an_agent_cannot_read_or_change_the_rules(served, selective):
    for op, args in (("share.rules", {}), ("share.rule_add", {"rule": {"kind": "tag", "tag": "private"}}),
                     ("share.rule_remove", {"rule": {"kind": "needs_you"}}), ("share.sync", {}),
                     ("share.revoke", {"ref": "K-1"})):  # fmt: skip
        res = agent(served, op, args)
        assert err(res) in ("op.not_allowed", "human_only"), (op, res)
    assert selective.ops == []
    rules = ok(req(served, "share.rules"))["data"]["rules"]
    assert [r["kind"] for r in rules] == ["needs_you"]


def test_default_shares_only_needs_you_summaries(served, selective):
    uid, key = labelled(served, "Public note", "public")
    ok(agent(served, "ask", {"ref": key, "text": "which bank?"}))
    prev = ok(req(served, "share.preview"))["data"]
    assert [i["kind"] for i in prev["items"]] == ["summary"] and "Public note" not in str(prev["items"])
    assert prev["rules"] == ["needs-you summaries (type, a one-line title and the deadline)"]


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
    got = ok(req(served, "share.rule_add", {"rule": rule}))["data"]
    assert got["synced"] is True and got["put"] == 1
    assert selective.count("share_put") == 1
    ((_, body),) = selective.ops
    (item,) = selective.opened  # the hub opened it: HXK-sealed, WSK-signed, exact content
    assert item["item_kind"] == "ticket" and item["item_version"] == 1 and item["dev"] is True
    assert item["content"]["title"] == "Public note" and item["content"]["labels"] == ["public"]
    assert item["chain"]["log"] == uid and item["chain"]["seq"] >= 1
    again = ok(req(served, "share.sync"))["data"]
    assert (again["put"], again["revoke"]) == (0, 0) and selective.count("share_put") == 1
    got = ok(req(served, "share.rule_remove", {"rule": rule}))["data"]
    assert got["revoke"] == 1 and selective.count("share_revoke") == 1
    assert selective.ops[-1][1]["item_id"] == body["item_id"]
    assert ok(req(served, "share.sync"))["data"]["revoke"] == 0 and selective.count("share_revoke") == 1


def test_revoke_withdraws_one_item_and_it_stays_withdrawn(served, selective, monkeypatch):
    _, key = labelled(served, "Public note", "public")
    ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))
    got = ok(req(served, "share.revoke", {"ref": key}))["data"]
    assert got["revoke"] == 1 and "cannot be taken back" in got["note"]
    assert ok(req(served, "share.sync"))["data"]["put"] == 0 and selective.count("share_put") == 1
    assert err(req(served, "share.revoke", {"ref": "NOPE-1"})) == "not_found"


def test_a_question_to_the_owner_is_a_summary_and_with_a_rule_the_full_question(served, selective, monkeypatch):
    _, key = served.ticket("Savings")
    ok(agent(served, "ask", {"ref": key, "text": "which bank?"}))
    got = ok(req(served, "share.sync"))["data"]
    assert got["put"] == 1 and selective.opened[0]["item_kind"] == "needs_you"
    assert (
        selective.opened[0]["content"]["type"] == "question"
        and selective.opened[0]["content"]["title"] == "which bank?"
    )
    ok(req(served, "share.rule_add", {"rule": {"kind": "questions"}}))
    (q,) = [i for i in selective.opened if i["item_kind"] == "question"]
    assert q["content"]["text"] == "which bank?" and q["content"]["id"] == "Q1"


def test_a_rule_survives_an_unreachable_hub_and_the_next_sync_finishes(served, selective, monkeypatch):
    labelled(served, "Public note", "public")
    monkeypatch.setattr("orch.host.shareops._link", lambda service: _Down())
    got = ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))["data"]
    assert got["synced"] is False and got["pending"] == "hub_unreachable" and selective.ops == []
    monkeypatch.undo()
    from orch.host import hubops

    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)
    assert ok(req(served, "share.sync"))["data"]["put"] == 1


class _Down:
    def call(self, op, body):
        raise OSError("down")

    def close(self):
        pass


def test_the_item_version_grows_with_every_change_and_revoke_twice_is_the_same_answer(served, selective):
    uid, key = labelled(served, "Public note", "public")
    ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Renamed note"]}))
    assert ok(req(served, "share.sync"))["data"]["put"] == 1
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
    got = ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))["data"]
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


def settle(served, quick, *, need=1, timeout=10.0):
    """Wait until the workspace's auto thread has finished ``need`` more passes (and is idle)."""
    import time

    svc = served.h.host.services[served.wid]
    a = quick.state_of(svc)
    assert a is not None, "the auto thread is not armed"
    target = a.passes + need
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with a.cv:
            if a.passes >= target and a.dirty_since is None:
                return a
        time.sleep(0.02)
    raise AssertionError(f"no auto pass finished (passes={a.passes}, wanted {target})")


def test_an_edit_to_a_shared_ticket_goes_out_as_a_higher_version_without_a_sync(served, selective, quick_auto):
    uid, key = labelled(served, "Public note", "public")
    ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))
    assert [i["item_version"] for i in selective.opened] == [1]
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Renamed note"]}))
    settle(served, quick_auto)
    assert [i["item_version"] for i in selective.opened] == [1, 2]
    assert selective.opened[1]["content"]["title"] == "Renamed note"
    assert selective.opened[0]["chain"]["seq"] < selective.opened[1]["chain"]["seq"]
    assert selective.count("share_revoke") == 0


def test_a_burst_of_edits_is_one_debounced_pass(served, selective, quick_auto, monkeypatch):
    monkeypatch.setattr(quick_auto, "DEBOUNCE_S", 0.5)
    _, key = labelled(served, "Public note", "public")
    ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))
    for n in range(4):
        ok(agent(served, "set", {"ref": key, "pairs": [f"title=Edit {n}"]}))
    settle(served, quick_auto)
    assert [i["item_version"] for i in selective.opened] == [1, 2]
    assert selective.opened[1]["content"]["title"] == "Edit 3"


def test_a_ticket_that_stops_matching_is_revoked(served, selective, quick_auto):
    _, key = labelled(served, "Public note", "public")
    ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))
    ok(agent(served, "set", {"ref": key, "pairs": ["labels=private"]}))
    settle(served, quick_auto)
    assert selective.count("share_revoke") == 1 and selective.count("share_put") == 1


def test_a_ticket_that_starts_matching_after_the_confirmation_is_not_auto_shared(served, selective, quick_auto):
    """The owner confirmed what the preview showed (C6-2). A later match waits for the owner's next confirm, however
    it came about (a new ticket, a label an agent adds, a question an agent asks); an explicit sync then sends it."""
    _, shared = labelled(served, "Public note", "public")
    ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))
    _, plain = served.ticket("Plain note")
    ok(agent(served, "set", {"ref": plain, "pairs": ["labels=public"]}))  # starts matching: an agent widens
    labelled(served, "Brand new public", "public")  # a new ticket that matches
    ok(agent(served, "ask", {"ref": plain, "text": "which bank?"}))  # a new needs-you summary (default rule)
    ok(agent(served, "set", {"ref": shared, "pairs": ["title=Renamed"]}))  # the one thing the pass may send
    settle(served, quick_auto)
    assert [i["content"]["title"] for i in selective.opened] == ["Public note", "Renamed"]
    assert selective.count("share_put") == 2
    got = ok(req(served, "share.sync"))["data"]  # the owner's next confirm shares what matches now
    assert got["put"] == 3 and selective.count("share_put") == 5
    ok(agent(served, "set", {"ref": plain, "pairs": ["title=Plain, edited"]}))  # now confirmed: it follows
    settle(served, quick_auto)
    assert selective.opened[-1]["content"]["title"] == "Plain, edited"


def test_an_item_that_left_and_comes_back_needs_a_new_confirm(served, selective, quick_auto):
    _, key = labelled(served, "Public note", "public")
    ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))
    ok(agent(served, "set", {"ref": key, "pairs": ["labels=private"]}))
    settle(served, quick_auto)
    ok(agent(served, "set", {"ref": key, "pairs": ["labels=public"]}))
    settle(served, quick_auto)
    assert selective.count("share_put") == 1 and selective.count("share_revoke") == 1


def test_full_mode_never_sends_even_when_the_thread_runs(served, full, quick_auto):
    from orch.host import shareauto

    svc = served.h.host.services[served.wid]
    _, key = labelled(served, "Public note", "public")
    shareauto.arm(svc)  # as if a leftover share state armed it
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Renamed note"]}))
    a = settle(served, quick_auto)
    assert a.parked and str(a.last).startswith("parked:") and full.ops == []


def test_no_pass_before_a_selective_join(served, quick_auto):
    from orch.host import shareauto

    svc = served.h.host.services[served.wid]
    _, key = labelled(served, "Public note", "public")
    shareauto.arm(svc)
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Renamed note"]}))
    a = settle(served, quick_auto)
    assert a.parked and "hub.not_joined" in str(a.last)


def test_an_unreachable_hub_is_retried_and_the_edit_arrives_later(served, selective, quick_auto, monkeypatch):
    from orch.host import hubops, shareops

    _, key = labelled(served, "Public note", "public")
    ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))
    real = shareops._link
    monkeypatch.setattr(shareops, "_link", lambda service: _Down())
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Renamed note"]}))
    a = settle(served, quick_auto)
    assert a.last["synced"] is False and selective.count("share_put") == 1
    monkeypatch.setattr(shareops, "_link", real)
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)
    import time

    end = time.monotonic() + 15
    while len(selective.opened) < 2 and time.monotonic() < end:  # the retry, after its backoff
        time.sleep(0.05)
    assert [i["content"]["title"] for i in selective.opened] == ["Public note", "Renamed note"]


def test_a_refused_update_is_visible_to_the_owner_and_marks_the_hub_copy_stale(
    served, selective, quick_auto, monkeypatch
):
    from orch.host import shareops
    from orch.hubclient.errors import HubRefused

    _, key = labelled(served, "Public note", "public")
    ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))
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
    ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))
    assert selective.count("share_put") == 1
    served.restrict(uid, [served.lab.owner])
    ok(agent(served, "set", {"ref": key, "pairs": ["title=Merger with ACME, price 4.2M"]}))  # the owner's agent sees it
    _, other = served.ticket("Unrelated")
    ok(agent(served, "set", {"ref": other, "pairs": ["title=Unrelated, edited"]}))  # any commit wakes the pass
    settle(served, quick_auto)
    sent_titles = [i["content"].get("title") for i in selective.opened]
    assert "Merger with ACME, price 4.2M" not in sent_titles, sent_titles
