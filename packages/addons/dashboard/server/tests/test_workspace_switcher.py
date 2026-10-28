"""#306 (M4) server: several workspaces on one host, keyed by UUID. Two workspaces may share a prefix and even a ticket
key; a host that is not running shows as such and the others keep working."""

import json
import os
import socket
import threading
from pathlib import Path

import pytest
from conftest import ORIGIN, PORT
from devhost.host import StubHost
from fastapi.testclient import TestClient

from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth
from orch_dashboard.errors import fail
from orch_dashboard.gateway import Gateway
from orch_dashboard.hostlink import HostLink

SECOND = "abcdef0123456789abcdef01" + "c0ffee00"  # the same prefix as the first, a different UUID (32 hex)


def build(tmp: Path, monkeypatch, workspace_id: str, title: str):
    """A dev workspace made with the store's own signed events under `workspace_id` (the helpers' WS is swapped)."""
    from orch.addons.install import grant_event, package_dir
    from orch.addons.manifest import load_manifest
    from orch.addons.package import read_package

    from tests.store import helpers
    from tests.store.helpers import Env

    original = helpers.WS
    monkeypatch.setattr(helpers, "WS", workspace_id)
    try:
        env = Env(tmp)
        env.bootstrap()
        env.new_ticket(title)
        manifest = Path(__file__).parents[2] / "orch-addon.json"
        directory = package_dir(env.store_dir, "dashboard")
        directory.mkdir(parents=True)
        (directory / "orch-addon.json").write_bytes(manifest.read_bytes())
        event = grant_event(load_manifest(manifest.read_bytes()), read_package(directory))
        typ = event.pop("type")
        env.store.append(env.person_event(env.owner, "workspace", typ, **event), log="workspace")
        (env.root / ".orch-dev").touch()
        env.store.close()
        return env
    finally:
        monkeypatch.setattr(helpers, "WS", original)


@pytest.fixture
def two(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_DEV", "1")
    first = build(tmp_path / "one", monkeypatch, "705d40abbb8c1c90354a1acaa94c935c", "Billing export")
    second = build(tmp_path / "two", monkeypatch, SECOND, "Mobile sign-in")
    stubs = [StubHost(e.root, e.host_state, e.owner.ref) for e in (first, second)]
    for stub in stubs:
        stub.peers = stubs
    registry = tmp_path / "registry"
    (registry / "host").mkdir(parents=True, mode=0o700)
    pins = [
        {
            "workspace_id": s.ws,
            "host_uid": os.getuid(),
            "peers": {str(os.getuid()): {"class": "person", "person": s.person}},
        }
        for s in stubs
    ]
    (registry / "host/workspaces.json").write_text(json.dumps({"workspaces": pins}))
    (registry / "host/workspaces.json").chmod(0o600)
    down: set[str] = set()
    threads = []

    def connect(path, uid, ws):
        stub = stubs[0] if ws is None else next((s for s in stubs if s.ws == ws), None)
        if ws in down or stub is None:
            raise fail("host.unavailable")
        client, host = socket.socketpair()
        thread = threading.Thread(target=stub.serve_connection, args=(host, ws is None), daemon=True)
        thread.start()
        threads.append(thread)
        return HostLink(client, uid, ws)

    monkeypatch.setattr(HostLink, "connect", connect)
    gateway = Gateway(registry)
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html></html>")
    with TestClient(
        create_app(PORT, gateway, Auth(), static), base_url=ORIGIN, headers={"X-Orch-Click-Contract": "4"}
    ) as client:
        auth = client.app.state.auth
        res = client.post("/auth/session", json={"code": auth.launch(PORT)["code"]}, headers={"Origin": ORIGIN})
        token = res.json()["token"]
        yield client, {"Authorization": f"Bearer {token}"}, stubs, down, gateway
    gateway.close()


def test_two_workspaces_with_one_prefix_stay_two_workspaces_keyed_by_uuid(two):
    client, headers, stubs, down, gateway = two
    got = client.get("/api/workspaces", headers=headers).json()
    assert [w["id"] for w in got] == [s.ws for s in stubs]
    assert {w["prefix"] for w in got} == {"DEMO"} and len({w["id"] for w in got}) == 2
    assert all(w["host_state"] == "ok" for w in got)
    assert stubs[0].ws[-8:] != stubs[1].ws[-8:]  # the last 8 hex tell them apart in an address (`?ws=`)


def test_the_same_ticket_key_reads_each_workspaces_own_ticket(two):
    client, headers, stubs, down, gateway = two
    titles = {}
    for stub in stubs:
        res = client.get(f"/api/w/{stub.ws}/tickets/DEMO-0001", headers=headers)
        assert res.status_code == 200
        titles[stub.ws] = res.json()["title"]
    assert sorted(titles.values()) == ["Billing export", "Mobile sign-in"]
    boards = [client.get(f"/api/w/{s.ws}/tickets", headers=headers).json()["rows"] for s in stubs]
    assert [[r["key"] for r in rows] for rows in boards] == [["DEMO-0001"], ["DEMO-0001"]]
    assert boards[0][0]["title"] != boards[1][0]["title"]


def test_a_sign_request_belongs_to_the_workspace_it_was_prepared_in(two):
    client, headers, stubs, down, gateway = two
    a, b = stubs
    write = {**headers, "Origin": ORIGIN}
    owner = client.get(f"/api/w/{a.ws}/members", headers=headers).json()[0]
    signer = {"device": owner["devices"][0]["id"], "auth": "convenience"}
    # A request bound to the owner's one-click signer, prepared in workspace a and held by this dashboard (as
    # click/prepare leaves it).
    made = gateway.link(a.ws).call(
        "human.prepare",
        {"op": "log", "args": {"ref": "DEMO-0001", "message": "hello"}, "signer": signer},
        write=True,
        idem="a_" + "0" * 25 + "1",
    )
    request_id = made["request"]["id"]
    client.app.state.handoffs.own(owner["person"], a.ws, request_id, 600)
    step = {"request_id": request_id}
    assert client.post(f"/api/w/{a.ws}/click/state", json=step, headers=write).json()["state"] == "bound"
    # The same id under the other workspace's address is a request that never existed there.
    assert client.post(f"/api/w/{b.ws}/click/state", json=step, headers=write).status_code == 404
    assert client.post(f"/api/w/{b.ws}/click/view", json=step, headers=write).status_code == 404
    assert client.post(f"/api/w/{b.ws}/click/cancel", json=step, headers=write).status_code == 404
    assert a.requests.find(request_id)["state"] == "bound"


def test_a_host_that_is_not_running_is_marked_and_the_other_keeps_working(two):
    client, headers, stubs, down, gateway = two
    a, b = stubs
    assert client.get(f"/api/w/{b.ws}/today", headers=headers).status_code == 200  # b has a live link
    down.add(b.ws)
    gateway.links[b.ws].close()  # the host went away under an open link
    got = {w["id"]: w for w in client.get("/api/workspaces", headers=headers).json()}
    assert got[a.ws]["host_state"] == "ok" and got[a.ws]["prefix"] == "DEMO"
    assert got[b.ws]["host_state"] == "host.unavailable"
    assert got[b.ws]["prefix"] == "" and got[b.ws]["name"] == b.ws  # nothing is made up for a host that cannot answer
    assert got[b.ws]["members"] == [] and got[b.ws]["features"] == []
    session = client.get("/api/session", headers=headers).json()
    assert session["workspaces"][b.ws] == [] and "tickets" in session["workspaces"][a.ws]
    assert client.get(f"/api/w/{a.ws}/tickets/DEMO-0001", headers=headers).json()["title"] in {
        "Billing export",
        "Mobile sign-in",
    }
    res = client.get(f"/api/w/{b.ws}/tickets/DEMO-0001", headers=headers)
    assert res.status_code == 503 and res.json()["error"]["code"] == "host.unavailable"
    down.discard(b.ws)  # started again: it is back, with its own data
    again = {w["id"]: w for w in client.get("/api/workspaces", headers=headers).json()}
    assert again[b.ws]["host_state"] == "ok" and again[b.ws]["prefix"] == "DEMO"


def test_a_workspace_the_registry_does_not_pin_is_not_there(two):
    client, headers, stubs, down, gateway = two
    unknown = "f" * 32
    assert client.get(f"/api/w/{unknown}/tickets", headers=headers).status_code == 404
    assert client.get(f"/api/w/{unknown}/settings", headers=headers).status_code == 404
    assert (
        client.post(
            f"/api/w/{unknown}/click/prepare",
            json={"action": "comment", "key": "DEMO-0001", "message": "x"},
            headers={**headers, "Origin": ORIGIN},
        ).status_code
        == 404
    )


def test_settings_are_per_workspace_too(two):
    client, headers, stubs, down, gateway = two
    reads = [client.get(f"/api/w/{s.ws}/members", headers=headers).json() for s in stubs]
    assert reads[0][0]["person"] != reads[1][0]["person"]  # each workspace has its own owner
    a, b = stubs
    from pydantic import TypeAdapter

    from orch_dashboard.signing import PrepareBody, plan

    planned = plan(
        gateway, a.ws, TypeAdapter(PrepareBody).validate_python({"action": "settings_set", "grant_hours": 12})
    )
    device = reads[0][0]["devices"][0]["id"]
    made = client.app.state.human(
        a.ws,
        "human.prepare",
        {"op": planned.op, "args": planned.args, "signer": {"device": device, "auth": "convenience"}},
        idem=planned.idem(a.ws, 0),
    )
    assert made["events"][0]["set"] == {"grant_hours": 12}
    assert len(a.requests.table) == 1 and b.requests.table == {}


def test_a_workspace_name_with_hidden_characters_is_not_listed_as_that_name(two):
    client, headers, stubs, down, gateway = two
    stubs[1].name = "Main\u202e"
    got = {w["id"]: w for w in client.get("/api/workspaces", headers=headers).json()}
    assert got[stubs[1].ws]["host_state"] == "workspace.name_invalid" and got[stubs[1].ws]["name"] == stubs[1].ws
    assert got[stubs[0].ws]["host_state"] == "ok"


@pytest.mark.parametrize("value", [None, 7, ["x"]])
def test_a_non_string_name_is_name_invalid_not_a_crash(two, value):
    client, headers, stubs, down, gateway = two
    stubs[1].name = value
    got = {w["id"]: w for w in client.get("/api/workspaces", headers=headers).json()}
    assert got[stubs[1].ws]["host_state"] == "workspace.name_invalid"
    assert got[stubs[0].ws]["host_state"] == "ok"


def test_a_member_name_with_hidden_characters_is_shown_as_the_person_id_not_hidden_with_the_workspace(two):
    """Opus RH review L1: one such member must not take the workspace (and the roster) down; the name is never shown
    or repaired, the person id stands in for it, flagged so the page can say why."""
    from orch_dashboard import adapters

    ok = {"members": [{"person": "p_tom", "name": "Tom", "role": "member", "devices": []}]}
    assert adapters.members(ok)[0].name == "Tom" and not adapters.members(ok)[0].name_hidden
    for name in ("To\u202em", "T\u2800", "x\ue000", None):
        bad = {
            "members": [
                {**ok["members"][0], "name": name},
                {"person": "p_ana", "name": "Ana", "role": "owner", "devices": []},
            ]
        }
        listed = adapters.members(bad)
        assert (listed[0].name, listed[0].name_hidden) == ("p_tom", True)
        assert (listed[1].name, listed[1].name_hidden) == ("Ana", False)
        roster = adapters.settings.roster(bad)
        assert (roster[0].name, roster[0].name_hidden) == ("p_tom", True) and roster[1].name == "Ana"
