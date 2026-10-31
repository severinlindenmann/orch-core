"""Delivery reads use the selection and current digest, without a hub or sockets."""

from dataclasses import replace
from types import SimpleNamespace

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
    state.refused["ticket:u"] = {"code": "untrusted text", "line": "Visible"}
    assert read()["tickets_state"] == {"DEMO-0001": "refused:unknown"}
    state.withdrawn = ["ticket:u"]
    assert read()["tickets_state"] == {}
