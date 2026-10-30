import os
import re
from pathlib import Path

import pytest
from conftest import ORIGIN, PORT

from orch_dashboard.app import SETTINGS_TABS, SPA_BARE_ONLY, create_app
from orch_dashboard.errors import HostError, host_response
from orch_dashboard.gateway import read_pins


def test_closed_route_allowlist():
    expected = {
        "/auth/probe",
        "/auth/session",
        "/auth/logout",
        "/auth/passkey/options",
        "/auth/passkey/login",
        "/auth/enroll/options",
        "/auth/enroll/register",
        "/api/stepup/options",
        "/api/stepup/verify",
        "/api/passkeys",
        "/api/trusted",
        "/api/trusted/enrol",
        "/api/trusted/{kid}",
        "/api/passkeys/enroll-link",
        "/api/passkeys/{credential_id}",
        "/api/w/{ws}/stepup/options",
        "/api/session",
        "/api/events",
        "/api/w/{ws}/head",
        "/api/workspaces",
        "/api/host",
        "/api/me",
        "/api/w/{ws}/identity",
        "/api/w/{ws}/today",
        "/api/w/{ws}/remote",  # R2: remote access (loopback only; refused on every remote listener in routegate)
        "/api/w/{ws}/remote/code",
        "/api/w/{ws}/remote/devices/{device_id}/remove",
        "/api/w/{ws}/remote/off",
        "/api/w/{ws}/tickets",
        "/api/w/{ws}/board",
        "/api/w/{ws}/tickets/{key}",
        "/api/w/{ws}/tickets/{key}/events",
        "/api/w/{ws}/tickets/{key}/changes",
        "/api/w/{ws}/tickets/{key}/artifacts/{name}",
        "/api/w/{ws}/repos/check",
        "/api/w/{ws}/click/status",
        "/api/w/{ws}/hubsign",
        "/api/w/{ws}/hub/approve/prepare",  # FB15: the one hand-off route, an add-on grant for the hub (owner, full)
        "/api/w/{ws}/hub/approve/{request_id}",  # FB15: polls a request that route prepared
        "/api/w/{ws}/hub",
        "/api/w/{ws}/hub/link",
        "/api/w/{ws}/hub/share",
        "/api/w/{ws}/hub/share/audience",
        "/api/w/{ws}/hub/shared",
        "/api/w/{ws}/hub/share/preview",
        "/api/w/{ws}/hub/share/revoke",  # one click: stop sharing a ticket (owner, full session)
        "/api/w/{ws}/hub/share/rule-remove",  # one click: stop sharing by a rule (owner, full session)
        "/api/w/{ws}/hub/share/rule-add",  # one click: share more (owner, full LOCAL session, a plan shown here)
        "/api/w/{ws}/hub/share/sync",  # one click: confirm the shown selection (same rules)
        "/api/w/{ws}/org",  # M3 H-5: company offers (a read)
        "/api/w/{ws}/org/item",  # a read of one item
        "/api/w/{ws}/org/accept",  # one click: turn a company item on (owner, full LOCAL session, the digest shown)
        "/api/w/{ws}/org/decline",  # one click: turn it off (owner, full session)
        "/api/w/{ws}/org/sync",  # one click: get the latest from the hub
        "/api/w/{ws}/org/tool",  # one click: a tool On (local) or Off (owner, full session)
        "/api/w/{ws}/hub/switch",
        "/api/w/{ws}/hub/rail",
        "/auth/switch",
        "/auth/switch/challenge",
        "/api/w/{ws}/hub/connect",
        "/api/w/{ws}/hub/refresh",
        "/api/w/{ws}/click/prepare",
        "/api/w/{ws}/click/view",
        "/api/w/{ws}/click/state",
        "/api/w/{ws}/click/sign",
        "/api/w/{ws}/click/cancel",
        "/api/w/{ws}/click/custody",
        "/api/w/{ws}/click/custody/seal",
        "/api/w/{ws}/click/images",  # R3 part B: pasted-screenshot chunks relayed to the signer (digest-checked)
        "/api/w/{ws}/click/rotate",
        "/api/w/{ws}/click/rotate/seal",
        "/api/w/{ws}/click/github",
        "/api/w/{ws}/click/github/seal",
        "/api/w/{ws}/click/github/remove",
        "/api/w/{ws}/click/runner",
        "/api/w/{ws}/click/runner/review",
        "/api/w/{ws}/settings",
        "/api/w/{ws}/add-ons",
        "/api/w/{ws}/versions",
        "/api/w/{ws}/me/risk_notices",
        "/api/w/{ws}/phone/available",
        "/api/w/{ws}/phone/request",
        "/api/w/{ws}/phone/cancel",
        "/api/w/{ws}/phone/status/{id}",
        "/api/w/{ws}/members",
        "/api/w/{ws}/readers",
        "/api/w/{ws}/grants",
        "/api/w/{ws}/quick",
        "/api/w/{ws}/worktrees",
        "/api/w/{ws}/worktrees/create",
        "/api/w/{ws}/worktrees/remove",
        "/api/w/{ws}/addons",
        "/api/w/{ws}/view/{name}",
        "/api/w/{ws}/wiki",
        "/api/w/{ws}/wiki/page",
        "/api/w/{ws}/wiki/create",
        "/api/w/{ws}/wiki/search",
        "/api/w/{ws}/wiki/backlinks",
        "/api/w/{ws}/explorer",
        "/api/w/{ws}/explorer/tree",
        "/api/w/{ws}/explorer/file",
        "/api/w/{ws}/explorer/names",
        "/api/w/{ws}/explorer/search",
        "/api/w/{ws}/explorer/history",
        "/api/w/{ws}/explorer/commit",
        "/api/w/{ws}/explorer/compare",
        "/api/w/{ws}/explorer/ticket/{key}",
        "/api/w/{ws}/shares",
        "/api/w/{ws}/shares/prepare",
        "/api/w/{ws}/shares/send",
        "/api/w/{ws}/shares/revoke",
        "/api/w/{ws}/shares/discard",
        "/api/w/{ws}/shares/register",
        "/api/w/{ws}/activity",
        "/api/w/{ws}/usage",
        "/api/w/{ws}/followups",
        "/api/w/{ws}/schedules",
        "/api/w/{ws}/records",
        "/api/w/{ws}/records/commit",
        "/api/w/{ws}/records/auto-off",
        "/api/w/{ws}/records/init",
        "/api/w/{ws}/records/auto-on",
        "/api/w/{ws}/claude-trust",
        "/api/w/{ws}/records/remote",
        "/api/w/{ws}/records/push-initial",
        "/api/w/{ws}/records/current",
        "/api/w/{ws}/runs",
        "/api/w/{ws}/terminals/preview",
        "/api/w/{ws}/terminals/open",
        "/api/w/{ws}/runs/{run}/stop",
        "/api/w/{ws}/runs/{run}/transcript",
        "/api/w/{ws}/runs/{run}/link",
        "/api/w/{ws}/runs/{run}/detach",
        "/api/w/{ws}/runs/{run}/sources",
        "/api/w/{ws}/runs/{run}/sources/{source_id}",
        "/api/w/{ws}/tickets/{key}/delegations",
        "/api/w/{ws}/tickets/{key}/worktree-diff",
        "/api/w/{ws}/tickets/{key}/workdir",
        "/api/w/{ws}/workdir/delete",
        "/api/w/{ws}/runs/{run}/attach",
        "/term",
        "/api/w/{ws}/agents/preview",
        "/api/w/{ws}/agents/start",
        "/api/w/{ws}/model-routing",
        "/api/w/{ws}/widgets/catalog",  # Widgets add-on (#763): built-in template pins
        "/widget-frame/{rest:path}",  # sandboxed widget frame, own CSP, exact-match only (#763, Opus Tier A+)
        "/api/w/{ws}/market",  # V3 A10: the Add-ons page
        "/api/w/{ws}/market/installed",
        "/api/w/{ws}/market/stage",
        "/api/w/{ws}/market/stage/{stage_id}",
        "/api/w/{ws}/market/uninstall",
        "/api/w/{ws}/market/refresh",
        "/api/w/{ws}/market/add",
        "/api/w/{ws}/workspace/cosmetic",
        "/api/w/{ws}/add-ons/{name}/settings",
        "/api/w/{ws}/hub/link",  # run-9 feedback 6: read a connect link
        "/api/w/{ws}/runner",
        "/api/w/{ws}/runner/deposits/{deposit_id}",
        "/api/w/{ws}/runner/forget",
        "/api/testrun/{run}",
        "/api/testrun/{run}/previous",
        "/api/testrun/{run}/screenshots.zip",
        "/api/testrun/{run}/steps/{step}",
        "/api/testrun/{run}/steps/{step}/images",
        "/api/testrun/{run}/images/{name}",
        "/auth/setup",
        "/api/setup/{route:path}",
        "/{path:path}",
    }
    assert {route.path for route in create_app(PORT).routes} == expected


def test_shell_p1_reads(client, token, stub):
    headers = {"Authorization": f"Bearer {token}"}
    workspaces = client.get("/api/workspaces", headers=headers).json()
    assert workspaces[0]["id"] == stub.ws
    assert workspaces[0]["members"][0]["person"] == stub.person
    every = [
        "identity",
        "today",
        "tickets",
        "board",
        "ticket",
        "changes",
        "evidence",
        "activity",
        "actions",
        "settings",
        "members",
        "settings_read",
        "grant_list",
        "readers",
        "read_ext",
        "read_ext2",
        *(
            f"do:{op}"
            for op in (
                "settings.set",
                "policy.set",
                "member.add",
                "member.role",
                "member.remove",
                "device.remove",
                "grant.revoke",
            )
        ),
    ]
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
        "/api/w/" + stub.ws + "/handoff",  # the terminal hand-off is gone: no route answers it
        "/api/w/" + stub.ws + "/hub/command",
        "/auth/unknown",
        "/docs",
        "/openapi.json",
    ]:
        assert client.get(path, headers=headers).status_code == 404
    assert client.post(f"/api/w/{stub.ws}/tickets", json={}, headers={**headers, "Origin": ORIGIN}).status_code == 405


@pytest.mark.parametrize(
    "path",
    [
        "/w/DEMO",
        "/w/DEMO/today",
        "/w/DEMO/board",
        "/w/DEMO/tickets",
        "/w/DEMO/settings",
        "/w/DEMO/settings/general",
        "/w/DEMO/settings/grants",
        "/w/DEMO/settings/addons",  # R2-2: the Add-ons tab
        "/w/DEMO/quick",
        "/w/DEMO/activity",
        "/w/DEMO/usage",
        "/w/DEMO/focus",
        "/w/DEMO/focus/DEMO-0001",
        "/w/DEMO/ticket/DEMO-0001",
        "/ticket/DEMO-0001",
    ],
)
def test_app_addresses_open_the_app(client, path):
    res = client.get(path)
    assert res.status_code == 200 and "main-hash.js" in res.text and res.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path",
    [
        "/w/DEMO/settings/nope",
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
    # these do not depend on query/1
    assert gateway.features(stub.ws) == ["activity", "settings_read", "readers", "read_ext", "read_ext2"]
    gone = ("read_ext/1", "read_ext/2", "activity/1", "settings/1", "reader/1")
    link.welcome["caps"][:] = [c for c in link.welcome["caps"] if c not in gone]
    assert gateway.features(stub.ws) == []
    link.welcome["ops"]["whoami"] = 2
    assert gateway.features(stub.ws) == []


def test_disabled_dashboard(client, token, stub, p1):
    p1.store.append(p1.person_event(p1.owner, "workspace", "addon.disabled", name="dashboard"), log="workspace")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/workspaces", headers=headers).json()[0]["host_state"] == "addon.disabled"
    assert client.get(f"/api/w/{stub.ws}/tickets", headers=headers).status_code == 409


def test_a_dashboard_paused_by_a_fresh_join_is_reported_as_paused(client, token, stub, p1, gateway, monkeypatch):
    p1.store.append(p1.person_event(p1.owner, "workspace", "addon.disabled", name="dashboard"), log="workspace")
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(type(gateway), "paused_unverified", lambda self, ws: True)
    assert client.get("/api/workspaces", headers=headers).json()[0]["host_state"] == "addon.paused"
    assert client.get(f"/api/w/{stub.ws}/tickets", headers=headers).status_code == 409  # still closed, only the words


@pytest.mark.parametrize(
    "row,want",
    [
        ({"name": "dashboard", "state": "paused", "paused": {"cause": "unverified"}}, True),
        ({"name": "dashboard", "state": "paused", "paused": {"cause": "policy"}}, False),
        ({"name": "dashboard", "state": "disabled"}, False),
    ],
)
def test_paused_unverified_only_for_the_join_pause(row, want):
    from orch_dashboard.gateway import Gateway

    class Link:
        def call(self, op):
            return {"addons": [row]}

    class G:
        def link(self, ws):
            return Link()

    assert Gateway.paused_unverified(G(), "W") is want


def test_a_plain_text_fix_passes_but_a_structured_one_does_not():
    from orch_dashboard.errors import HostError, host_response

    def body(fix):
        return host_response(
            HostError({"code": "sign.convenience_readers", "message": "m", "retryable": False, "fix": fix})
        ).body

    assert b'"fix":"update the readers first"' in body("update the readers first")
    # a command passes only for the signing-path codes, and only when every word is plain
    assert b'"argv"' in body({"argv": ["orch", "host", "convenience", "off", "--workspace", "DEMO"]})
    assert b'"fix"' not in body({"argv": ["orch", "x\u202e"]})
    assert b'"fix"' not in body({"argv": ["orch"], "extra": 1})

    def other(fix):
        return host_response(
            HostError({"code": "host.unavailable", "message": "m", "retryable": True, "fix": fix})
        ).body

    assert b'"fix"' not in other({"argv": ["orch", "status"]})


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


def test_artifact_get_needs_the_diff_capability(gateway, stub):
    from orch_dashboard.errors import HostError

    link = gateway.link(stub.ws)
    link.welcome["caps"].remove("diff/1")  # query/1 stays: artifact.get belongs to diff/1 (contract 0.4)
    assert "evidence" not in gateway.features(stub.ws) and "ticket" in gateway.features(stub.ws)
    with pytest.raises(HostError) as err:
        link.call("artifact.get", {"ref": "DEMO-0001", "name": "x", "offset": 0})
    assert err.value.error["code"] == "op.unsupported"


def test_fastapi_telemetry_is_off_even_when_the_environment_asks_for_it(monkeypatch):
    monkeypatch.setenv("FASTAPI_OTEL_AUTO_CONFIGURE", "true")
    app = create_app(PORT)
    config = app._telemetry
    assert not any(config[k] for k in ("tracing", "metrics", "logs", "operation_spans", "auto_configure"))
    assert app._native_telemetry.enabled() is False


ROUTER = Path(__file__).resolve().parents[2] / "src" / "app" / "router.tsx"
TABS_TS = Path(__file__).resolve().parents[2] / "src" / "app" / "pages" / "settings" / "tabs.ts"
NOT_SERVED = {"__layout/$name"}  # the mockup's layout preview: never on a real host
SAMPLE = {"$key": "DEMO-0001", "$ticket": "DEMO-0001", "$tab": "general", "$name": "guide", "$page": "main"}


def _client_routes():
    src = ROUTER.read_text()
    paths = re.findall(r"createRoute\(\{[^}]*?\bpath:\s*'([^']*)'", src, re.S)
    assert len(paths) >= 15, paths  # the walk found the router's routes
    return paths


def _sample(path):
    return "/".join(SAMPLE.get(seg, seg) for seg in path.split("/") if seg)


def test_every_client_route_is_served_on_reload(client):
    for route in _client_routes():
        if route in NOT_SERVED:
            continue
        tail = _sample(route)
        url = f"/{tail}" if route in SPA_BARE_ONLY else f"/w/DEMO/{tail}".rstrip("/")
        res = client.get(url)
        assert res.status_code == 200 and "main-hash.js" in res.text, (route, url)


def test_every_real_host_settings_tab_is_served(client):
    block = TABS_TS.read_text().split("export const HOST_TABS")[1].split("] as const")[0]
    tabs = re.findall(r"id:\s*'([a-z]+)'", block)
    assert tabs and set(tabs) == set(SETTINGS_TABS), tabs
    for tab in tabs:
        assert client.get(f"/w/DEMO/settings/{tab}").status_code == 200, tab


@pytest.mark.parametrize(
    "path",
    [
        "/w/DEMO/guide",
        "/w/DEMO/worktrees",
        "/w/DEMO/shares",
        "/w/DEMO/artifacts",
        "/w/DEMO/addon/guide/main",
        "/w/DEMO/settings/addon/x",
    ],
)
def test_reload_addresses_named_by_the_e2e(client, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", ["/w/DEMO/guide/x/y", "/w/DEMO/__layout/x", "/guide", "/worktrees"])
def test_api_and_unknown_stay_404(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", ["/api/nope", "/api/w/DEMO/guide", "/api/w/DEMO/worktrees/x"])
def test_unknown_api_paths_stay_json_404(client, token, path):
    res = client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 404 and "main-hash.js" not in res.text
