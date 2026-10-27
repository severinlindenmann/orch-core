# ruff: noqa: F811, F401
"""V3 C11 (design-c11 I2, I6): in full hub mode this server signs nothing and holds no hub credential. It reads the
host's signing mode, relays the host's `hub.handoff` read through a closed filter, refuses the click signer, and never
falls back to a local key when the hub is down."""

import ast
import re
from pathlib import Path

import pytest
from conftest import ORIGIN
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_dashboard_click import approve, backup, click_start, hdr, host_check, prepare, script, url  # noqa: F401

from orch_dashboard import click, hubapprove
from orch_dashboard.errors import HostError, fail, host_response

SRC = Path(hubapprove.__file__).parent
WS = "a" * 32
ULID = "01J9ZP0000000000000000000S"
GOOD = {
    "mode": "full",
    "where": "hub",
    "summary": {"kind": "gate", "ticket": "DEMO-0001", "gate": "plan", "gate_gen": 2, "question": None,
                "status": "open", "title": "Ship it <b>x</b> https://evil.example"},
    "short_hash": "abcd ef01 2345",
    "url": f"https://hub.example.test:4443/#/w/{WS}/t/{ULID}/g/plan",
}  # fmt: skip


class Link:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def supports(self, op, cap="ops/1"):
        return op in self.answers

    def call(self, op, args=None):
        self.calls.append((op, args))
        got = self.answers[op]
        if isinstance(got, Exception):
            raise got
        return got


class Gw:
    def __init__(self, link):
        self.link = link

    def enabled(self, ws):
        return self.link


def app_for(link):
    app = FastAPI()
    hubapprove.register(app, Gw(link))

    @app.exception_handler(HostError)
    def _(request, exc):
        return host_response(exc)

    return TestClient(app)


def post(c, body):
    return c.post(f"/api/w/{WS}/hubsign", json=body)


def test_the_route_relays_the_host_read_with_a_closed_shape():
    link = Link({"hub.handoff": GOOD})
    res = post(app_for(link), {"kind": "gate", "ticket": "DEMO-0001", "gate": "plan"})
    assert res.status_code == 200
    assert res.json()["url"] == GOOD["url"] and res.json()["short_hash"] == "abcd ef01 2345"
    assert res.json()["summary"]["title"].startswith("Ship it <b>x</b>")  # data for the page, rendered as text
    assert set(res.json()) == {"mode", "summary", "short_hash", "url"}
    assert link.calls == [("hub.handoff", {"kind": "gate", "ticket": "DEMO-0001", "gate": "plan"})]


@pytest.mark.parametrize(
    "bad",
    [
        {"url": "https://evil.example/x"},
        {"url": f"http://hub.example.test/#/w/{WS}/t/{ULID}"},
        {"url": f"https://hub.example.test/#/w/{WS}/t/{ULID}/../x"},
        {"url": f"https://u@hub.example.test/#/w/{WS}/t/{ULID}"},
        {"url": f"https://hub.example.test/#/w/{WS}/t/{ULID}?x=1"},
        {"url": "javascript:alert(1)"},
        {"url": None},
        {"short_hash": "ABCD EF01 2345"},
        {"short_hash": "abcdef012345"},
        {"short_hash": "abcd ef01 2345 6789"},
        {"mode": "selective"},
        {"summary": "text"},
    ],
)
def test_a_host_answer_off_the_grammar_is_dropped(bad):
    res = post(app_for(Link({"hub.handoff": {**GOOD, **bad}})), {"kind": "status", "ticket": "DEMO-0001"})
    assert res.status_code == 500 or res.json()["error"]["code"] == "internal"


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "gate", "ticket": "../x"},
        {"kind": "gate", "ticket": "DEMO-0001", "gate": "x"},
        {"kind": "nope", "ticket": "DEMO-0001"},
        {"kind": "status", "ticket": "DEMO-0001", "extra": 1},
        {"kind": "question", "ticket": "DEMO-0001", "question": "a/b"},
    ],
)
def test_the_browser_names_only_a_typed_read(body):
    link = Link({"hub.handoff": GOOD})
    assert post(app_for(link), body).status_code in (400, 422)
    assert link.calls == []


def test_i6_hub_down_gives_the_hosts_refusal_and_never_a_local_signature():
    link = Link({"hub.handoff": fail("hub.not_joined", exit=3)})
    res = post(app_for(link), {"kind": "status", "ticket": "DEMO-0001"})
    assert res.status_code >= 400 and res.json()["error"]["code"] == "hub.not_joined"
    assert [op for op, _ in link.calls] == ["hub.handoff"]  # nothing else is tried: no click, no human.prepare


def test_the_route_needs_a_host_that_has_the_op():
    res = post(app_for(Link({})), {"kind": "status", "ticket": "DEMO-0001"})
    assert res.status_code == 501


def test_mode_of_is_advisory_and_closed():
    assert hubapprove.mode_of(Gw(Link({"hub.status": {"signing": "full"}})), WS) == "full"
    assert hubapprove.mode_of(Gw(Link({"hub.status": {"signing": "weird"}})), WS) is None
    assert hubapprove.mode_of(Gw(Link({"hub.status": fail("role.denied", exit=3)})), WS) is None  # not an owner
    assert hubapprove.mode_of(Gw(Link({})), WS) is None  # an older host


def test_i2_this_server_has_no_hub_code_no_hub_client_and_no_outbound_fetch():
    banned_mod = re.compile(r"orch\.hubclient|orch\.host\.hub|orch\.hub\b")
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for n in names:
                assert not banned_mod.search(n), (path.name, n)
    for name in ("hubapprove.py", "click.py"):  # the modules that touch signing open no connection of their own
        tree = ast.parse((SRC / name).read_text())
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not {
            m for m in mods if m.split(".")[0] in {"requests", "httpx", "urllib", "socket", "http", "aiohttp"}
        }, name


def test_click_status_reports_the_mode_and_prepare_is_refused_in_full_mode(client, token, stub, script, monkeypatch):
    monkeypatch.setattr(click, "mode_of", lambda gateway, ws: "full")
    assert client.get(url(stub, "status"), headers=hdr(token, False)).json()["mode"] == "full"
    monkeypatch.setattr(hubapprove, "mode_of", lambda gateway, ws: "full")
    res = prepare(client, token, stub)
    assert res.status_code == 403 and res.json()["error"]["code"] == "sign.hub_only"
    assert script.prepared == 0  # the signer is never asked


def test_leaving_the_hub_is_prepared_in_full_mode(client, token, stub, script, backup, monkeypatch):
    """e2e-4: hub.leave is the break-glass the host takes by click; refusing it left no way out of full mode."""
    monkeypatch.setattr(hubapprove, "mode_of", lambda gateway, ws: "full")
    res = client.post(f"/api/w/{stub.ws}/click/prepare", json={"action": "hub_leave"}, headers=hdr(token))
    assert res.status_code == 200, res.text
    assert script.prepared == 1  # the signer was asked: the full-mode refusal did not fire


def test_unknown_mode_is_refused_like_full(client, token, stub, script, monkeypatch):
    monkeypatch.setattr(hubapprove, "mode_of", lambda gateway, ws: "unknown")
    assert prepare(client, token, stub).json()["error"]["code"] == "sign.hub_only"


def test_selective_and_standalone_prepare_as_before(client, token, stub, script, monkeypatch):
    for mode in ("selective", "standalone", None):
        monkeypatch.setattr(hubapprove, "mode_of", lambda gateway, ws, m=mode: m)
        assert prepare(client, token, stub).json().get("error", {}).get("code") != "sign.hub_only"


def test_the_today_rows_say_sign_hub_in_full_mode_only(client, token, stub, rich, monkeypatch):
    path = f"/api/w/{stub.ws}/today"
    plain = client.get(path, headers=hdr(token, False)).json()
    assert all("sign" not in n for n in plain["needs_you"])
    monkeypatch.setattr(hubapprove, "mode_of", lambda gateway, ws: "full")
    full = client.get(path, headers=hdr(token, False)).json()
    assert full["needs_you"] and all(n["sign"] == "hub" for n in full["needs_you"])


def test_the_error_code_is_allowed_through_and_is_a_403():
    res = host_response(fail("sign.hub_only", exit=3))
    assert res.status_code == 403
    assert ORIGIN


RID = "hr_01J9ZP0000000000000000HND1"
ADDON = {
    "mode": "full", "where": "hub", "kind": "addon", "pending": True,
    "summary": {"kind": "addon", "name": "wiki", "version": "1.2.0", "package_sha256": "sha256:" + "a" * 64,
                "capabilities": ["network"]},
    "short_hash": "aaaa aaaa aaaa",
    "url": f"https://hub.example.test:4443/#/w/{WS}/addon/eyJhIjoxfQ",
}  # fmt: skip


def test_an_add_on_approval_is_relayed_with_a_closed_shape():
    link = Link({"hub.handoff": ADDON})
    res = post(app_for(link), {"kind": "addon", "request": RID})
    assert res.status_code == 200
    summary = {"kind": "addon", "name": "wiki", "version": "1.2.0", "capabilities": ["network"]}
    assert res.json() == {"mode": "full", "kind": "addon", "summary": summary, "short_hash": "aaaa aaaa aaaa",
                          "url": ADDON["url"]}  # fmt: skip
    assert link.calls == [("hub.handoff", {"kind": "addon", "request": RID})]


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "addon"},
        {"kind": "addon", "request": "x"},
        {"kind": "addon", "request": RID, "ticket": "DEMO-0001"},
        {"kind": "gate", "request": RID},
        {"kind": "gate"},
    ],
)
def test_an_add_on_approval_and_a_ticket_approval_never_mix(body):
    assert post(app_for(Link({"hub.handoff": ADDON})), body).status_code in (400, 422)


BAD_ADDON = [
    {"url": f"https://hub.example.test/#/w/{WS}/addon/a b"},
    {"url": "https://evil.example/x"},
    {"summary": {"name": 1}},
]


@pytest.mark.parametrize("bad", BAD_ADDON)
def test_an_add_on_answer_off_the_grammar_is_dropped(bad):
    res = post(app_for(Link({"hub.handoff": {**ADDON, **bad}})), {"kind": "addon", "request": RID})
    assert res.status_code == 500 or res.json()["error"]["code"] == "internal"


def test_the_add_on_link_is_moved_to_the_hubs_web_console_origin(monkeypatch):
    """Round 3: the host's link names the hub LINK port; the person signs in at the web console."""
    from orch_dashboard import switch

    monkeypatch.setattr(switch, "browser_origin", lambda request: "http://127.0.0.1:8200")
    rail = {"hub": {"name": "Hub", "origin": "http://127.0.0.1:8100"}, "workspaces": []}
    link = Link({"hub.handoff": ADDON, "hub.switch": rail})
    res = post(app_for(link), {"kind": "addon", "request": RID})
    assert res.json()["url"] == f"http://127.0.0.1:8100/#/w/{WS}/addon/eyJhIjoxfQ"
    assert ("hub.switch", {"kind": "rail", "origin": "http://127.0.0.1:8200"}) in link.calls


def test_the_add_on_link_for_this_computer_names_the_other_device_form(monkeypatch):
    from orch_dashboard import switch

    monkeypatch.setattr(switch, "browser_origin", lambda request: "http://127.0.0.1:8200")
    rail = {"hub": {"name": "Hub", "origin": "http://127.0.0.1:8100", "network_origin": "http://100.71.14.98:8100"}}
    res = post(app_for(Link({"hub.handoff": ADDON, "hub.switch": rail})), {"kind": "addon", "request": RID})
    assert res.json()["url"] == f"http://127.0.0.1:8100/#/w/{WS}/addon/eyJhIjoxfQ"
    assert res.json()["other_url"] == f"http://100.71.14.98:8100/#/w/{WS}/addon/eyJhIjoxfQ"
    rail["hub"]["network_origin"] = "javascript:alert(1)"  # off the origin grammar: dropped
    res = post(app_for(Link({"hub.handoff": ADDON, "hub.switch": rail})), {"kind": "addon", "request": RID})
    assert "other_url" not in res.json()


def test_without_a_console_origin_the_hosts_link_stands():
    res = post(
        app_for(Link({"hub.handoff": ADDON, "hub.switch": RuntimeError("no rail")})), {"kind": "addon", "request": RID}
    )
    assert res.json()["url"] == ADDON["url"]


SETTINGS = {
    "mode": "full", "where": "hub", "kind": "settings", "short_hash": None,
    "summary": {"kind": "settings", "set": {"grant_hours": 24}},
    "url": f"https://hub.example.test:4443/#/w/{WS}/settings/eyJncmFudF9ob3VycyI6MjR9",
}  # fmt: skip


def test_a_settings_change_is_relayed_with_a_closed_shape():
    link = Link({"hub.handoff": SETTINGS})
    res = post(app_for(link), {"kind": "settings", "set": {"grant_hours": 24}})
    assert res.status_code == 200
    summary = {"kind": "settings", "set": {"grant_hours": 24}}
    assert res.json() == {"mode": "full", "kind": "settings", "summary": summary, "short_hash": None,
                          "url": SETTINGS["url"]}  # fmt: skip
    assert link.calls == [("hub.handoff", {"kind": "settings", "set": {"grant_hours": 24}})]


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "settings"},
        {"kind": "settings", "set": {"grant_hours": 1}, "request": RID},
        {"kind": "gate", "set": {"grant_hours": 1}},
    ],
)
def test_a_settings_change_names_only_its_settings(body):
    assert post(app_for(Link({"hub.handoff": SETTINGS})), body).status_code in (400, 422)


@pytest.mark.parametrize(
    "bad",
    [
        {"summary": {"kind": "settings", "set": {"retention_days": 1}}},  # not a setting the grammar knows
        {"summary": {"kind": "settings", "set": {"grant_hours": 999}}},
        {"url": "https://evil.example/x"},
    ],
)
def test_a_settings_answer_off_the_grammar_is_dropped(bad):
    res = post(app_for(Link({"hub.handoff": {**SETTINGS, **bad}})), {"kind": "settings", "set": {"grant_hours": 24}})
    assert res.status_code == 500 or res.json()["error"]["code"] == "internal"
