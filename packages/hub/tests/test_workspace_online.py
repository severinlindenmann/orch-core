"""Membership and live transport both determine workspace availability."""

from types import SimpleNamespace

import pytest
from webapi_helpers import Web

from orch_hub.contract import vectorgen as G
from orch_hub.link import Registry
from orch_hub.router import Router
from orch_hub.switch import SwitchService


@pytest.fixture
def workspace(tmp_path, make_hub):
    w = Web(tmp_path)
    reg = Registry()
    router = Router(reg)
    hub = make_hub(registry=reg, router=router)
    w.api.d.registry = reg
    w.api.d.router = router
    w.api.d.switch = SwitchService(hub_origin=lambda: None)
    w.api.d.services = SimpleNamespace(snapshot=dict)
    client = hub.open(first=True)
    client.request("join", hub.join_body(mode="full"))
    yield w, hub, client, router, w.login(w.admin)
    for connection in hub.clients:
        connection.close()
    hub.server.close()
    router.queue.close()
    w.close()


def test_workspace_link_drop_is_offline(workspace):
    w, _hub, _client, router, token = workspace
    for path in ("/me/workspaces", "/admin/workspaces", "/rail"):
        assert w.call("GET", path, token=token)[1]["workspaces"][0]["online"] is True
    assert w.call("GET", "/admin/services", token=token)[1]["workspaces"]["online"] == 1
    # Detach the live transport deterministically, as the link server does on disconnect.
    router.detach(G.WS_A, router._live[G.WS_A])
    for path in ("/me/workspaces", "/admin/workspaces", "/rail"):
        status, body, _ = w.call("GET", path, token=token)
        assert status == 200
        assert body["workspaces"][0]["online"] is False
    assert w.call("GET", "/admin/services", token=token)[1]["workspaces"]["online"] == 0


def test_workspace_signed_leave_ends_membership_even_with_live_link(workspace):
    w, hub, client, router, token = workspace
    client.request("binding_put", {"object": G.unbound(seq=1)})
    # Late telemetry on the still-open transport cannot resurrect membership.
    hub.registry.seen(G.WS_A, G.NOW + 1, "127.0.0.1")
    for path in ("/me/workspaces", "/rail"):
        status, body, _ = w.call("GET", path, token=token)
        assert status == 200
        assert body["workspaces"] == []
    row = w.call("GET", "/admin/workspaces", token=token)[1]["workspaces"][0]
    assert row["status"] == "unbound"
    assert row["online"] is False
    assert router.is_online(G.WS_A) is False
    assert w.call("GET", "/admin/services", token=token)[1]["workspaces"]["online"] == 0
