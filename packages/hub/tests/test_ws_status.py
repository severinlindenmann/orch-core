"""Workspace status is display-only; roles are a read of the access model."""

import pytest
from webapi_helpers import WS, WS_SEL, Web

from orch_hub.contract import vectorgen as G
from orch_hub.contract.phase4 import render_text
from orch_hub.link import LinkError, Registry
from orch_hub.versions import MIN_SUPPORTED_VERSION, version_state
from orch_hub.webapi.api import ROUTES, SWITCH_HANDLERS, route_scopes

STATUS = {"version": "2.0.0.dev0", "commit": "aB123", "host": "host\u202e"}


@pytest.mark.parametrize("mode", ["full", "selective"])
def test_status_stores_rows_and_socket_address(hub, mode):
    c = hub.open(first=True)
    c.request("join", hub.join_body(mode=mode))
    assert c.request("status_put", STATUS) == {}
    status = hub.registry.status(G.WS_A)
    with hub.registry.lock, hub.registry._db:
        hub.registry._db.execute("INSERT OR REPLACE INTO owner_alias VALUES (?, ?)", (G.WS_A, G.U_ALICE))
    host = render_text(STATUS["host"]) if mode == "full" else ""
    assert status == {**STATUS, "host": host, "address": "127.0.0.1", "last_seen_ms": G.NOW}
    hub.server._now = lambda: G.NOW + 1
    c.request("owner_get", {})
    assert hub.registry.status(G.WS_A)["last_seen_ms"] == G.NOW + 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {**STATUS, "peer_addr": "evil"},
        {**STATUS, "version": ""},
        {**STATUS, "version": "a" * 33},
        {**STATUS, "version": "2/0"},
        {**STATUS, "commit": "g"},
        {**STATUS, "commit": "a" * 41},
        {**STATUS, "host": "x" * 81},
        {**STATUS, "host": 1},
    ],
)
def test_malformed_status_vectors(hub, body):
    c = hub.open(first=True)
    c.request("join", hub.join_body())
    with pytest.raises(LinkError) as error:
        c.request("status_put", body)
    assert error.value.code == "malformed"
    assert hub.registry.status(G.WS_A)["version"] is None


def test_unknown_pin(hub):
    with pytest.raises(LinkError) as error:
        hub.open(first=True).request("status_put", STATUS)
    assert error.value.code == "unknown_workspace"


def test_registry_migration_and_persistence(tmp_path):
    import sqlite3

    path = str(tmp_path / "registry.db")
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE workspace (workspace_id TEXT PRIMARY KEY, owner_person_id TEXT NOT NULL, wsk_pub TEXT NOT NULL, pk_pub TEXT NOT NULL, card TEXT NOT NULL, binding_state TEXT NOT NULL)"
    )
    db.close()
    (tmp_path / "registry.db").chmod(0o600)
    reg = Registry(path)
    reg.commit_join(G.WS_A, G.P_ALICE, G.K.b("wsk_a"), G.K.b("pk_alice"), {}, {})
    assert reg.status(G.WS_A)["last_seen_ms"] is None
    reg.put_status(G.WS_A, STATUS)
    reg.close()
    reg = Registry(path)
    assert reg.status(G.WS_A)["version"] == STATUS["version"]
    reg.close()
    reg.seen(G.WS_A, G.NOW, "")  # telemetry failure never escapes


@pytest.mark.parametrize(
    "v,hub,minimum,expected",
    [
        ("2.0.0.dev0", "2.0", "2.0.0", "newest"),
        ("2.1+abc", "2.1.0", "2", "newest"),
        ("2.2", "2.1", "2", "newest"),
        ("2.1", "2.10", "2", "older"),
        ("1.9", "2.1", "2.0.0.dev0", "unsupported"),
        (None, "2", "2", "unknown"),
        ("garbage", "2", "2", "unknown"),
        ("2", None, "2", "unknown"),
        ("2", "2", None, "unknown"),
    ],
)
def test_version_state(v, hub, minimum, expected):
    assert version_state(v, hub, minimum) == expected


def test_roles_and_api_rows(tmp_path):
    w = Web(tmp_path)
    try:
        admin, ann = w.login(w.admin), w.login(w.ann)
        w.reg.ws[WS]["owner"] = w.ann
        route = f"/workspaces/{WS}/roles"
        assert w.call("GET", route)[1] == {"error": "no_session"}
        assert w.call("GET", route, token=admin)[:2] == (
            200,
            {
                "owner": "ann",
                "profile": None,
                "groups": [{"group": "team", "role": "edit", "members": ["ann"]}],
            },
        )
        w.env.acc.grant(WS, "team", "admin")
        assert w.call("GET", route, token=ann)[0] == 200
        w.env.acc.grant(WS, "team", "read")
        assert w.call("GET", route, token=ann)[1] == {"error": "unknown_workspace"}
        stranger = w.env.user("stranger")
        assert w.call("GET", route, token=w.login(stranger))[1] == {"error": "unknown_workspace"}
        w.reg.add(WS_SEL, "selective", owner=w.ann)
        assert w.call("GET", f"/workspaces/{WS_SEL}/roles", token=ann)[:2] == (
            200,
            {"owner": "ann", "groups": [], "profile": None},
        )
        assert w.call("GET", f"/workspaces/{WS_SEL}/roles", token=admin)[1] == {"error": "unknown_workspace"}
        result = w.call("GET", "/me/workspaces", token=ann)[1]
        assert result["hub"]["min_supported"] == MIN_SUPPORTED_VERSION
        for row in result["workspaces"] + w.call("GET", "/admin/workspaces", token=admin)[1]["workspaces"]:
            assert {"version", "commit", "version_state"} <= row.keys()
            assert row["version_state"] == "unknown"
        # Private telemetry requires hub-admin membership, even for workspace owners/admins.
        private = {"host", "address", "last_seen_ms"}
        assert all(private.isdisjoint(r) for r in result["workspaces"])
        assert any(r["role"] == "read" for r in result["workspaces"])
        assert any(r["role"] == "owner" for r in result["workspaces"])
        w.env.acc.grant(WS, "team", "admin")
        rows = w.call("GET", "/me/workspaces", token=ann)[1]["workspaces"]
        assert any(r["role"] == "admin" for r in rows)
        assert all(private.isdisjoint(r) for r in rows)
        assert all(private <= r.keys() for r in w.call("GET", "/admin/workspaces", token=admin)[1]["workspaces"])
        assert all(private <= r.keys() for r in w.call("GET", "/me/workspaces", token=admin)[1]["workspaces"])
    finally:
        w.close()


def test_roles_route_gates():
    (route,) = [r for r in ROUTES if r.handler == "workspace_roles"]
    assert route.auth == "session"
    assert route.path == "/api/v1/workspaces/{workspace_id}/roles"
    assert route_scopes(route) == {"full"}
    assert route.handler not in SWITCH_HANDLERS


@pytest.mark.parametrize("method", ["set_binding_state", "commit_join"])
def test_selective_mode_change_clears_stored_host(method):
    reg = Registry()
    try:
        full = {"status": "bound", "binding": {"mode": "full"}}
        selective = {"status": "bound", "binding": {"mode": "selective"}}
        reg.commit_join("ws", "owner", "key", "pk", {}, full)
        reg.put_status("ws", STATUS)
        assert reg.status("ws")["host"]
        if method == "commit_join":
            reg.commit_join("ws", "owner", "key", "pk", {}, selective)
        else:
            reg.set_binding_state("ws", selective)
        assert reg.status("ws")["host"] == ""
        reg.put_status("ws", STATUS)
        assert reg.status("ws")["host"] == ""
    finally:
        reg.close()
