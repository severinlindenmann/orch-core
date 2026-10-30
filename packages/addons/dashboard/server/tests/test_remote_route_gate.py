"""#1198 R1-5, requirement 1: the remote route gate: one table (`routegate.TABLE`), enforced in `Security`.

A route added to the app without an entry in the table fails `test_every_route_of_the_app_has_a_remote_rule`."""

# ruff: noqa: F811

import re

import pytest
from conftest import ORIGIN
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from remote_kit import (  # noqa: F401
    HTTPS,
    NAME,
    RUN,
    SOURCE,
    app,
    bearer,
    front,
    local,
    loop,
    passkey_store,
    remote,
    terminals_app,
)
from starlette.websockets import WebSocketDisconnect

from orch_dashboard import routegate
from orch_dashboard.front import Front
from orch_dashboard.routegate import ALLOWED, PRE_LOGIN, REFUSED, STEP_UP, TABLE, TERMINALS

OPEN_ENDED = {"/api/events"}  # a stream: not walked by the generic test (it never ends)


def app_routes(app):
    # a mount or a plain Starlette Route would be skipped below: the walk must see every kind of route there is
    assert all(isinstance(r, APIRoute | APIWebSocketRoute) for r in app.routes), "a route type the walk cannot read"
    for route in app.routes:
        if isinstance(route, APIRoute):
            yield from ((m, route.path) for m in sorted(route.methods) if m not in ("HEAD", "OPTIONS"))
        elif isinstance(route, APIWebSocketRoute):
            yield ("WEBSOCKET", route.path)


def test_every_route_of_the_app_has_a_remote_rule(app):
    routes = set(app_routes(app))
    assert routes - set(TABLE) == set(), "a route without a remote rule: add it to routegate.TABLE"
    assert set(TABLE) - routes == set(), "a table entry for a route that does not exist"
    assert all(rule in routegate.RULES for rule, _ in TABLE.values())


def test_the_passkey_routes_are_the_only_ones_before_login():
    assert {k for k, (rule, _) in TABLE.items() if rule == PRE_LOGIN} == {
        ("POST", "/auth/passkey/options"),
        ("POST", "/auth/passkey/login"),
        ("POST", "/auth/enroll/options"),
        ("POST", "/auth/enroll/register"),
        ("POST", "/auth/switch"),  # H1: a one-use hub switch ticket, checked by the hub
    }


def fill(template):
    return re.sub(r"\{[^}]*\}", "x", template)


def call(client, method, template, token):
    headers = bearer(token, HTTPS) if token else {"Origin": HTTPS}
    if method == "GET":
        return client.get(fill(template), headers=headers)
    return client.request(method, fill(template), json={}, headers=headers)


def gate_refused(res):
    return res.status_code == 403 and res.json().get("error", {}).get("code") == "remote.refused"


#: Two routes the listener's policy turns off before the table is asked (launch codes, the widget frame): 404.
POLICY_OFF = {("POST", "/auth/session"), ("GET", "/widget-frame/{rest:path}")}


def answered_no(res, key):
    return (res.status_code == 404) if key in POLICY_OFF else gate_refused(res)


WALKED = sorted((m, t, r) for (m, t), (r, _) in TABLE.items() if m != "WEBSOCKET" and t not in OPEN_ENDED)


@pytest.mark.parametrize(("method", "template", "rule"), WALKED)
def test_each_route_follows_its_rule_for_a_remote_session(front, remote, method, template, rule):
    res = call(front, method, template, remote)
    if rule in (REFUSED, TERMINALS):  # terminals are off on the front by default
        assert answered_no(res, (method, template)), (method, template, res.status_code)
    elif rule == PRE_LOGIN and method == "GET" or rule in (ALLOWED, STEP_UP):  # step-up: the handler asks (R1-6)
        assert not gate_refused(res), (method, template, res.status_code, res.text)


@pytest.mark.parametrize(("method", "template", "rule"), WALKED)
def test_a_refused_route_is_invisible_before_login(front, method, template, rule):
    """No session: the same answer as a route that does not exist (404), so nothing is learned before login."""
    if rule in (REFUSED, STEP_UP, TERMINALS) and not template.startswith("/{"):
        res = call(front, method, template, None)
        assert res.status_code == 404 and not gate_refused(res), (method, template, res.status_code)


def test_an_unlisted_route_is_refused_on_remote_and_answers_on_loopback(app, loop, front, remote, local):
    @app.post("/api/w/{ws}/brand-new")
    def brand_new(ws: str):
        return {"ok": True}

    assert ("POST", "/api/w/{ws}/brand-new") not in TABLE
    assert gate_refused(front.post("/api/w/x/brand-new", json={}, headers=bearer(remote, HTTPS)))
    assert loop.post("/api/w/x/brand-new", json={}, headers=bearer(local, ORIGIN)).json() == {"ok": True}


def test_an_unmatched_request_is_refused_on_remote(front, remote):
    assert gate_refused(front.post("/api/w/x/no-such-route", json={}, headers=bearer(remote, HTTPS)))
    assert gate_refused(front.request("PATCH", "/api/w/x/today", json={}, headers=bearer(remote, HTTPS)))


def test_a_head_request_is_not_a_route_of_the_app_and_is_refused(front, remote):
    assert front.head("/api/setup/x", headers=bearer(remote)).status_code == 403
    assert front.head("/", headers=bearer(remote)).status_code == 403


# --- the routes the requirement names ---------------------------------------------------------------------------------
NAMED = [
    ("POST", "/api/w/{ws}/agents/start", ALLOWED),
    ("POST", "/api/w/{ws}/runs/{run}/stop", ALLOWED),
    ("POST", "/api/w/{ws}/runs/{run}/attach", TERMINALS),
    ("POST", "/api/w/{ws}/terminals/open", TERMINALS),
    ("POST", "/api/w/{ws}/terminals/preview", TERMINALS),
    ("POST", "/api/w/{ws}/click/prepare", ALLOWED),
    ("POST", "/api/w/{ws}/click/custody/seal", ALLOWED),
    ("POST", "/api/w/{ws}/click/rotate/seal", ALLOWED),
    ("POST", "/api/passkeys/enroll-link", STEP_UP),
    ("DELETE", "/api/passkeys/{credential_id}", STEP_UP),
    ("POST", "/api/stepup/verify", ALLOWED),
    ("POST", "/api/w/{ws}/click/rotate", STEP_UP),
    ("POST", "/api/w/{ws}/hub/connect", STEP_UP),
    ("POST", "/api/w/{ws}/hubsign", REFUSED),
    ("POST", "/auth/session", REFUSED),
    ("POST", "/api/setup/{route:path}", REFUSED),
]


@pytest.mark.parametrize(("method", "template", "rule"), NAMED)
def test_the_named_routes_have_the_decided_rule(front, remote, method, template, rule):
    assert TABLE[(method, template)][0] == rule
    assert answered_no(call(front, method, template, remote), (method, template)) == (rule not in (ALLOWED, STEP_UP))


def test_click_prepare_is_the_only_prepare_route():
    """The terminal hand-off (unbound requests) is gone: no table row and no app route prepares anything else."""
    assert not [k for k in TABLE if "/handoff" in k[1] or k[1].endswith("/hub/command")]
    assert [k for k in TABLE if k[1].endswith("/prepare") and "/click/" in k[1]] == [
        ("POST", "/api/w/{ws}/click/prepare")
    ]


def test_terminals_come_back_with_the_policy_switch(gateway, auth, tmp_path, stub, remote):
    app = terminals_app(gateway, auth, tmp_path, on=True)
    on = TestClient(Front(app), base_url=HTTPS, headers={"X-Orch-Client": SOURCE})
    res = on.post(f"/api/w/{stub.ws}/runs/{RUN}/attach", json={}, headers=bearer(remote, HTTPS))
    assert res.status_code == 200 and "ticket" in res.json()


def test_the_terminal_socket_is_closed_on_remote_unless_switched_on(front, remote):
    good = {"host": NAME, "origin": HTTPS}
    with pytest.raises(WebSocketDisconnect) as closed, front.websocket_connect("/term", headers=good):
        pass
    assert closed.value.code == 1008


def test_loopback_is_not_gated(loop, local, stub):
    for method, template in (
        ("POST", "/api/w/{ws}/terminals/open"),
        ("POST", "/api/w/{ws}/hubsign"),
        ("GET", "/api/host"),
    ):
        path = fill(template).replace("/w/x", f"/w/{stub.ws}")
        res = loop.request(method, path, json={}, headers=bearer(local, ORIGIN))
        assert not gate_refused(res), template


# --- R3: the `remote-device` column is never wider than the front's table (T3) -------
def test_the_device_column_refuses_what_the_front_refuses_or_steps_up():
    """A stream holds no passkey and answers nothing before login: whatever the front refuses, asks a step-up for, or
    answers before login stays refused in a stream. (Its own walk is `test_remote_device_gate.py`.)"""
    for key, (rule, _) in TABLE.items():
        if rule in (REFUSED, STEP_UP, PRE_LOGIN):
            assert routegate.REMOTE_DEVICE[key][0] == REFUSED, key
    assert set(TABLE) <= set(routegate.REMOTE_DEVICE)
