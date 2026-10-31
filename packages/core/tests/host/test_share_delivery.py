"""Delivery reads use the selection and current digest, without a hub or sockets."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from orch.host import shareops
from orch.hubclient import shares as S
from orch.hubclient.errors import HubRefused


def test_share_delivery_states_and_confirming_sync(monkeypatch):
    fact = S.TicketFact("u", "DEMO-0001", "feature", "Visible", "open", (), None)
    service = SimpleNamespace(store=SimpleNamespace(state=SimpleNamespace(workspace=SimpleNamespace(hub={}))))
    call = SimpleNamespace(service=service, viewer="owner")
    state = S.ShareState(rules=[{"kind": "type", "type": "feature"}])
    monkeypatch.setattr(shareops, "_load", lambda _: state)
    monkeypatch.setattr(shareops, "tickets_for", lambda svc, viewer: [fact])

    def read():
        result = shareops._read(call, "share.rules", {})["data"]
        assert not {"line", "item_id", "digest"} & set(result)
        assert "secret" not in str(result)
        return result

    # A rule retained from before joining has never confirmed its selected ticket.
    assert read()["tickets_state"] == {"DEMO-0001": "not_sent"}
    assert read()["not_sent_count"] == 1
    selected = S.select(state.rules, [fact])
    assert state.auto_wanted(selected) == []
    state.confirm(selected, S.plan_of(selected))
    assert read()["tickets_state"] == {"DEMO-0001": "waiting"}
    link = SimpleNamespace(call=lambda *a: {})
    S.sync(state, selected, link=link, build=lambda *a: ("sealed", "hash"), save=lambda _: None)
    assert read()["tickets_state"] == {"DEMO-0001": "sent"}
    assert read()["not_sent_count"] == 0

    # A changed item is waiting even while the hub holds its previous version.
    fact = replace(fact, title="Edited")
    assert read()["tickets_state"] == {"DEMO-0001": "waiting"}

    def refuse(*args):
        raise HubRefused("too_large")

    selected = S.select(state.rules, [fact])
    S.sync(state, selected, link=link, build=refuse, save=lambda _: None)
    assert read()["tickets_state"] == {"DEMO-0001": "refused:too_large"}
    S.sync(state, selected, link=link, build=lambda *a: ("sealed", "hash2"), save=lambda _: None)
    assert read()["tickets_state"] == {"DEMO-0001": "sent"}
    assert not state.refused
    # A legacy confirmed=None state falls back to sent.
    state.confirmed = None
    assert read()["tickets_state"] == {"DEMO-0001": "sent"}
    # Invisible facts never get a key in this read, even if cached state contains them.
    state.sent["ticket:secret"] = {"digest": "secret", "item_id": "secret"}
    fact = replace(fact, title="Changed again")
    state.refused["ticket:u"] = {"code": "untrusted text", "line": "Visible"}
    assert read()["tickets_state"] == {"DEMO-0001": "refused:unknown"}
    state.withdrawn = ["ticket:u"]
    assert read()["tickets_state"] == {}


def test_share_delivery_worst_item_and_ticket_count(monkeypatch):
    fact = S.TicketFact(
        "u",
        "DEMO-0001",
        "feature",
        "Visible",
        "open",
        (),
        None,
        questions=(S.QuestionFact("Q1", "Question", True, "a" * 64),),
    )
    service = SimpleNamespace(store=SimpleNamespace(state=SimpleNamespace(workspace=SimpleNamespace(hub={}))))
    call = SimpleNamespace(service=service, viewer="owner")
    state = S.ShareState(rules=[{"kind": "type", "type": "feature"}, {"kind": "questions"}], confirmed={})
    monkeypatch.setattr(shareops, "_load", lambda _: state)
    monkeypatch.setattr(shareops, "tickets_for", lambda *_: [fact])
    selected = S.select(state.rules, [fact])
    assert {i.kind for i in selected} == {"ticket", "question"}
    ranks = ["refused:too_large", "not_sent", "waiting", "sent"]
    # Both directions: iteration order must not decide the ticket's state.
    for first in ranks:
        for second in ranks:
            state.sent, state.confirmed, state.refused = {}, {}, {}
            for item, status in zip(selected, (first, second), strict=True):
                if status == "sent":
                    state.sent[item.logical] = {"digest": item.digest}
                elif status == "waiting":
                    state.confirmed[item.logical] = dict(S.EMPTY_GRANT)
                elif status.startswith("refused:"):
                    state.refused[item.logical] = {"code": "too_large", "line": item.line()}
            result = shareops._read(call, "share.rules", {})["data"]
            worst = min((first, second), key=ranks.index)
            assert result["tickets_state"] == {fact.key: worst}
            assert result["not_sent_count"] == int(worst == "not_sent")


def test_share_delivery_sent_digest_precedes_confirmation_and_stale_refusal(monkeypatch):
    fact = S.TicketFact("u", "DEMO-0001", "feature", "Visible", "open", (), None, description="Description")
    service = SimpleNamespace(store=SimpleNamespace(state=SimpleNamespace(workspace=SimpleNamespace(hub={}))))
    call = SimpleNamespace(service=service, viewer="owner")
    state = S.ShareState(rules=[{"kind": "type", "type": "feature", "description": True}], confirmed={})
    monkeypatch.setattr(shareops, "_load", lambda _: state)
    monkeypatch.setattr(shareops, "tickets_for", lambda *_: [fact])
    selected = S.select(state.rules, [fact])
    S.sync(
        state,
        selected,
        link=SimpleNamespace(call=lambda *a: {}),
        build=lambda *a: ("sealed", "hash"),
        save=lambda _: None,
    )
    item = selected[0]
    assert state.sent[item.logical]["digest"] == item.digest
    state.refused[item.logical] = {"code": "too_large", "line": item.line()}
    assert shareops._read(call, "share.rules", {})["data"]["tickets_state"] == {fact.key: "sent"}
    state.refused.clear()
    # Readers and embedded description both participate in the same digest sync stores.
    state.rules[0]["audience"] = {"users": ["b" * 32], "groups": []}
    assert S.select(state.rules, [fact])[0].digest != item.digest
    assert shareops._read(call, "share.rules", {})["data"]["tickets_state"] == {fact.key: "not_sent"}
    state.rules[0].pop("audience")
    fact = replace(fact, description="Changed description")
    assert S.select(state.rules, [fact])[0].digest != item.digest
    assert shareops._read(call, "share.rules", {})["data"]["tickets_state"] == {fact.key: "not_sent"}


@pytest.mark.parametrize(("column", "role", "code"), [("P", "member", "role.denied"), ("A", "owner", "op.not_allowed")])
def test_share_delivery_dispatch_refuses_non_owners(monkeypatch, column, role, code):
    workspace = SimpleNamespace(members={"viewer": SimpleNamespace(role=role)}, hub={})
    store = SimpleNamespace(state=SimpleNamespace(workspace=workspace), refresh=lambda: None)
    call = SimpleNamespace(
        service=SimpleNamespace(store=store, state="ready"),
        viewer="viewer",
        relay=None,
        column=SimpleNamespace(name=column),
        frame={"op": "share.rules", "args": {}},
    )

    # Refuse before loading rules or per-ticket state, including for a person who is a member.
    def unexpected_read(*args):
        pytest.fail("non-owner reached delivery state")

    monkeypatch.setattr(shareops, "_read", unexpected_read)
    with pytest.raises(shareops.HostError) as error:
        shareops.serve(call)
    assert error.value.code == code
