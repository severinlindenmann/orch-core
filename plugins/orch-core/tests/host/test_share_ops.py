"""V3 C6: the host's share ops on a SELECTIVE workspace joined to a TLS test hub that records ``share_put`` /
``share_revoke``: owner-only edits, evaluation on the real tickets, revoke on rule removal, full mode refused, and the
default (needs-you summaries only). The sealed bytes are not in the contract yet: those tests are xfail."""

from __future__ import annotations

import pytest

from orch import crypto
from orch.hubclient import shares as S
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.human_helpers import err
from tests.host.test_hub_join_real import genesis, make_card, now_ms
from tests.host.test_hub_ops import keystore  # noqa: F401
from tests.hub.tlshub import TlsHub

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
SEALED_GAP = "the contract does not specify the sealed share item yet (V3-D23): build_sealed_item refuses"


class ShareHub:
    def __init__(self):
        self.ops: list[tuple[str, dict]] = []

    def handle(self, h, o):
        if o["op"] in ("share_put", "share_revoke"):
            self.ops.append((o["op"], o["body"]))
        return h.reply(o, body={}, ts_ms=o["ts_ms"])

    def count(self, op):
        return sum(1 for o, _ in self.ops if o == op)


def _join(served, human, mode):
    sh = ShareHub()
    wsk = crypto.unb64u(genesis(served)["wsk_pub"], 65)
    hub = TlsHub(wsk_pub=wsk, on_request=sh.handle)
    hub.workspace_id = served.wid
    hub.start()
    card = make_card(hub, now_ms(served))
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


def fake_builder(monkeypatch):
    monkeypatch.setattr(S, "build_sealed_item", lambda item_id, item: f"sealed:{item.logical}")


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
    fake_builder(monkeypatch)
    labelled(served, "Public note", "public")
    rule = {"kind": "tag", "tag": "public"}
    shown = ok(req(served, "share.preview", {"add": rule}))["data"]["plan"]
    labelled(served, "Another public note", "public")  # the selection moved after the owner looked
    res = req(served, "share.rule_add", {"rule": rule, "expect": shown})
    assert err(res) == "invalid.input" and "not what you were shown" in res["error"]["message"]
    assert selective.ops == [] and len(ok(req(served, "share.rules"))["data"]["rules"]) == 1


def test_rule_add_puts_exactly_the_matching_ticket_then_remove_revokes_once(served, selective, monkeypatch):
    fake_builder(monkeypatch)
    labelled(served, "Public note", "public")
    labelled(served, "Private review", "private")
    served.ticket("Plain list")
    rule = {"kind": "tag", "tag": "public"}
    got = ok(req(served, "share.rule_add", {"rule": rule}))["data"]
    assert got["synced"] is True and got["put"] == 1
    assert selective.count("share_put") == 1
    ((_, body),) = selective.ops
    assert body["sealed"].startswith("sealed:ticket:")  # the isolated builder produced the bytes
    again = ok(req(served, "share.sync"))["data"]
    assert (again["put"], again["revoke"]) == (0, 0) and selective.count("share_put") == 1
    got = ok(req(served, "share.rule_remove", {"rule": rule}))["data"]
    assert got["revoke"] == 1 and selective.count("share_revoke") == 1
    assert selective.ops[-1][1]["item_id"] == body["item_id"]
    assert ok(req(served, "share.sync"))["data"]["revoke"] == 0 and selective.count("share_revoke") == 1


def test_revoke_withdraws_one_item_and_it_stays_withdrawn(served, selective, monkeypatch):
    fake_builder(monkeypatch)
    _, key = labelled(served, "Public note", "public")
    ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))
    got = ok(req(served, "share.revoke", {"ref": key}))["data"]
    assert got["revoke"] == 1 and "cannot be taken back" in got["note"]
    assert ok(req(served, "share.sync"))["data"]["put"] == 0 and selective.count("share_put") == 1
    assert err(req(served, "share.revoke", {"ref": "NOPE-1"})) == "not_found"


def test_a_question_to_the_owner_is_a_summary_and_with_a_rule_the_full_question(served, selective, monkeypatch):
    fake_builder(monkeypatch)
    _, key = served.ticket("Savings")
    ok(agent(served, "ask", {"ref": key, "text": "which bank?"}))
    got = ok(req(served, "share.sync"))["data"]
    assert got["put"] == 1 and selective.ops[0][1]["sealed"].startswith("sealed:summary:")
    ok(req(served, "share.rule_add", {"rule": {"kind": "questions"}}))
    assert any(b["sealed"].startswith("sealed:question:") for _, b in selective.ops)


def test_a_rule_survives_an_unreachable_hub_and_the_next_sync_finishes(served, selective, monkeypatch):
    fake_builder(monkeypatch)
    labelled(served, "Public note", "public")
    monkeypatch.setattr("orch.host.shareops._link", lambda service: _Down())
    got = ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))["data"]
    assert got["synced"] is False and got["pending"] == "hub_unreachable" and selective.ops == []
    monkeypatch.undo()
    fake_builder(monkeypatch)
    from orch.host import hubops

    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)
    assert ok(req(served, "share.sync"))["data"]["put"] == 1


class _Down:
    def call(self, op, body):
        raise OSError("down")

    def close(self):
        pass


def test_the_sealed_format_gap_is_reported_and_nothing_is_sent(served, selective):
    labelled(served, "Public note", "public")
    got = ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))["data"]
    assert got["synced"] is False and got["pending"] == "sealed_format_pending" and selective.ops == []


@pytest.mark.xfail(reason=SEALED_GAP, strict=True)
def test_contract_sealed_item_is_hxk_sealed_and_wsk_signed(served, selective):
    """Needs the phase-4 contract extension (V3-D23): the hub opens ``sealed`` and checks WSK state + chain head."""
    labelled(served, "Public note", "public")
    ok(req(served, "share.rule_add", {"rule": {"kind": "tag", "tag": "public"}}))
    assert selective.count("share_put") == 1
