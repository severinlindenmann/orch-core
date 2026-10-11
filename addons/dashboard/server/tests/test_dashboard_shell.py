import os

import pytest
from conftest import ORIGIN, PORT

from orch_dashboard.app import create_app
from orch_dashboard.errors import HostError, host_response
from orch_dashboard.gateway import read_pins


def test_closed_route_allowlist():
    expected = {
        "/auth/probe",
        "/auth/session",
        "/auth/logout",
        "/api/session",
        "/api/events",
        "/api/w/{ws}/head",
        "/api/workspaces",
        "/api/me",
        "/api/w/{ws}/identity",
        "/api/w/{ws}/today",
        "/api/w/{ws}/tickets",
        "/api/w/{ws}/board",
        "/api/w/{ws}/tickets/{key}",
        "/api/w/{ws}/tickets/{key}/events",
        "/api/w/{ws}/tickets/{key}/changes",
        "/api/w/{ws}/tickets/{key}/artifacts/{name}",
        "/{path:path}",
    }
    assert {route.path for route in create_app(PORT).routes} == expected


def test_shell_p1_reads(client, token, stub):
    headers = {"Authorization": f"Bearer {token}"}
    workspaces = client.get("/api/workspaces", headers=headers).json()
    assert workspaces[0]["id"] == stub.ws
    assert workspaces[0]["members"][0]["person"] == stub.person
    every = ["identity", "today", "tickets", "board", "ticket", "changes", "evidence", "activity", "read_ext"]
    assert workspaces[0]["features"] == every
    assert client.get("/api/session", headers=headers).json()["features"] == sorted(every)
    assert client.get("/api/me", headers=headers).json()[0]["person"] == stub.person
    identity = client.get(f"/api/w/{stub.ws}/identity", headers=headers).json()
    assert identity["uuid"] == stub.ws and identity["tier"] == "A"
    tickets = client.get(f"/api/w/{stub.ws}/tickets", headers=headers)
    assert tickets.status_code == 200, tickets.text
    assert tickets.json()["rows"][0]["title"] == "Real P1 ticket"
    assert client.get(f"/api/w/{stub.ws}/today", headers=headers).json()["workspace"] == stub.ws
    assert client.get("/", headers=headers).headers["cache-control"] == "no-store"
    assert "immutable" in client.get("/assets/main-hash.js").headers["cache-control"]
    for path in [
        "/api/dev/reset",
        "/api/raw",
        "/api/w/" + stub.ws + "/handoff",
        "/auth/unknown",
        "/docs",
        "/openapi.json",
    ]:
        assert client.get(path, headers=headers).status_code == 404
    assert client.post(f"/api/w/{stub.ws}/tickets", json={}, headers={**headers, "Origin": ORIGIN}).status_code == 405


@pytest.mark.parametrize(
    "path",
    ["/w/DEMO", "/w/DEMO/today", "/w/DEMO/board", "/w/DEMO/tickets", "/w/DEMO/ticket/DEMO-0001", "/ticket/DEMO-0001"],
)
def test_app_addresses_open_the_app(client, path):
    res = client.get(path)
    assert res.status_code == 200 and "main-hash.js" in res.text and res.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path",
    [
        "/w/DEMO/settings/general",
        "/w/DEMO/ticket/x",
        "/w/DEMO/ticket/DEMO-1/extra",
        "/ticket/DEMO-1/x",
    ],
)
def test_other_addresses_are_not_the_app(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "query", ["limit=201", "limit=0", "sort=status", "file=/etc/passwd", "op=show", "page=../../x", "status=nope"]
)
def test_typed_route_inputs(client, token, stub, query):
    r = client.get(f"/api/w/{stub.ws}/tickets?{query}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400
    assert "/etc/passwd" not in r.text


def test_caps_hide_unavailable(gateway, stub):
    link = gateway.link(stub.ws)
    link.welcome["caps"].remove("query/1")
    assert gateway.features(stub.ws) == ["activity", "read_ext"]  # these two do not depend on query/1
    link.welcome["caps"][:] = [c for c in link.welcome["caps"] if c not in ("read_ext/1", "activity/1")]
    assert gateway.features(stub.ws) == []
    link.welcome["ops"]["whoami"] = 2
    assert gateway.features(stub.ws) == []


def test_disabled_dashboard(client, token, stub, p1):
    p1.store.append(p1.person_event(p1.owner, "workspace", "addon.disabled", name="dashboard"), log="workspace")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/workspaces", headers=headers).json()[0]["host_state"] == "addon.disabled"
    assert client.get(f"/api/w/{stub.ws}/tickets", headers=headers).status_code == 409


def test_error_passthrough():
    error = {
        "code": "host.restarting",
        "message": "wait",
        "hint": "retry",
        "retryable": True,
        "retry_after_ms": 20,
        "fix": {"argv": ["orch", "status"]},
    }
    res = host_response(HostError(error, 9))
    import json

    value = json.loads(res.body)
    assert res.status_code == 503 and value["exit"] == 9
    assert value["error"] == {k: v for k, v in error.items() if k != "fix"}


def test_registry_pin_permissions(tmp_path):
    path = tmp_path / "pins"
    path.write_text('{"workspaces": []}')
    path.chmod(0o600)
    assert read_pins(path) == {}
    path.chmod(0o644)
    with pytest.raises(HostError):
        read_pins(path)
    path.unlink()
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(OSError):
        read_pins(path)
    assert os.getuid() >= 0


@pytest.mark.parametrize("change", [{"workspace": "x"}, {"peer": []}, {"ops": []}, {"caps": {}}])
def test_malformed_workspace_welcome_is_one_error_row(client, token, stub, gateway, monkeypatch, change):
    original = stub.welcome

    def malformed():
        return {**original(), **change}

    # Establish a healthy control link first; only this workspace's welcome is malformed.
    assert client.get("/api/workspaces", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    gateway.links[stub.ws].close()
    monkeypatch.setattr(stub, "welcome", malformed)
    reply = client.get("/api/workspaces", headers={"Authorization": f"Bearer {token}"})
    assert reply.status_code == 200
    assert len(reply.json()) == 1
    assert reply.json()[0]["host_state"] == "frame.invalid"
