"""#1198 R1-4: a session stays on the listener that opened it; the local-only gates refuse a remote session; tickets."""

# ruff: noqa: F811

import dataclasses

import pytest
from conftest import ORIGIN, PORT
from fastapi.testclient import TestClient
from remote_kit import (  # noqa: F401
    HTTPS,
    NAME,
    RUN,
    SOURCE,
    Clock,
    app,
    bearer,
    echo,
    front,
    local,
    loop,
    passkey_store,
    remote,
    socket_dir,
    terminals_app,
    ungated,
)
from test_dashboard_click import (  # noqa: F401
    HASH,
    PASS_ENV,
    REQ,
    agent_access,
    click_start,
    hub_join,
    rotate_body,
    rotate_offer,
    script,
)

from orch_dashboard.front import Front
from orch_dashboard.policy import remote_policy

NEEDS = "Confirm with your fingerprint or PIN first"
#: the variants that act (the reviews that only open a sheet are plain, R1-6)
GATES = [
    ("runner", {"on": True, "view_digest": HASH}),
    (
        "custody",
        {"on": False, "request_id": REQ, "view_digest": HASH, "sealed": {"custody.passphrase": PASS_ENV}},
    ),
    ("rotate", rotate_body()),
    ("custody", {"on": True, "resume": True}),
    ("custody", {"on": True, "passphrase": "correct horse"}),
    ("sign", {"request_id": REQ, "view_digest": HASH, "sealed": {"hub.backup_key": PASS_ENV}}),
]


FEATURES = ["custody", "device_rotate", "runner_switch", "passkey_stepup"]


def step(client, stub, path, body, token, origin):
    return client.post(f"/api/w/{stub.ws}/click/{path}", json=body, headers=bearer(token, origin))


def test_each_session_works_on_its_own_listener_and_nowhere_else(loop, front, local, remote, auth):
    assert loop.get("/api/session", headers=bearer(local)).json()["login"] == "local"
    assert front.get("/api/session", headers=bearer(remote)).json()["login"] == "remote"
    # a remote bearer on loopback (also with the public name, also with the header the front would set): unknown
    assert loop.get("/api/session", headers=bearer(remote)).status_code == 401
    assert loop.get("/api/session", headers={**bearer(remote), "X-Orch-Client": SOURCE}).status_code == 401
    assert loop.post("/auth/logout", headers=bearer(remote, ORIGIN)).status_code == 401
    # and a local bearer on the front
    assert front.get("/api/session", headers=bearer(local)).status_code == 404
    assert front.post("/auth/logout", headers=bearer(local, HTTPS)).status_code == 404
    # refused is not burned: both are still alive on their own listener
    assert auth.valid(remote) and auth.valid(local)
    assert front.get("/api/session", headers=bearer(remote)).status_code == 200
    assert loop.get("/api/session", headers=bearer(local)).status_code == 200


def test_a_header_never_makes_a_request_local_or_picks_the_listener(loop, front, remote, local, stub, script):
    # loopback ignores X-Orch-Client: the same answers with it, nothing about the session changes
    plain = loop.get("/api/session", headers=bearer(local))
    spoofed = loop.get("/api/session", headers={**bearer(local), "X-Orch-Client": "198.51.100.99"})
    assert plain.status_code == spoofed.status_code == 200 and plain.json() == spoofed.json()
    # the name of the listener is not a header: the only way to name it is the scope the accepting server sets
    for header in ("X-Orch-Listener", "orch_listener", "X-Orch-Kind", "X-Orch-Login"):
        got = loop.get("/api/session", headers={**bearer(remote), header: "remote"})
        assert got.status_code == 401, header
    # the public name on loopback and the loopback name on the front are refused whatever else is sent
    assert loop.get("/", headers={"Host": NAME, "X-Orch-Client": SOURCE}).status_code == 421
    assert front.get("/", headers={"Host": f"127.0.0.1:{PORT}"}).status_code == 421
    # a remote client on the front who calls a launch-code exchange gets nothing
    assert front.post("/auth/session", json={"code": "x"}, headers={"Origin": HTTPS}).status_code == 404
    # the strongest forgery a front client can try: headers that name a local session or the loopback listener
    script.features = FEATURES
    forged = {
        **bearer(remote, HTTPS),
        "X-Orch-Client": "127.0.0.1",
        "X-Orch-Listener": "loopback",
        "X-Orch-Login": "local",
    }
    res = front.post(f"/api/w/{stub.ws}/click/runner", json={"on": True, "view_digest": HASH}, headers=forged)
    assert res.status_code == 403 and res.json()["error"]["code"] == "passkey.stepup_required"


def test_the_pre_login_surface_of_the_front_gets_the_address_for_the_login_bucket(auth):
    """The seam for R1-5: a passkey route reads `request.state.client` and asks `auth.remote_attempt` with it."""
    from orch_dashboard.policy import loopback_policy
    from orch_dashboard.security import Security

    ungated_policy = dataclasses.replace(remote_policy(NAME), route_gate=False)  # the table has its own tests
    seen = []
    stack = Front(Security(echo(seen), auth, loopback_policy(PORT), others=(ungated_policy,)))
    front = TestClient(stack, base_url=HTTPS, headers={"X-Orch-Client": "2001:db8::7"})
    assert front.get("/auth/passkey/login").status_code == 200
    assert seen[-1]["state"]["client"] == "2001:db8::7" and auth.remote_attempt(seen[-1]["state"]["client"])
    assert front.get("/api/session").status_code == 404  # nothing else answers without a remote session
    # a listener name that is not configured is refused, not served by the default policy
    bare = TestClient(Security(echo(seen), auth, loopback_policy(PORT)), base_url=HTTPS)
    odd = Security(echo(seen), auth, loopback_policy(PORT))
    assert odd.policy_of({"orch_listener": "remote"}) is None and odd.policy_of({}).name == "loopback"
    assert bare.get("/", headers={"host": f"127.0.0.1:{PORT}"}).status_code == 200


def prepared_id(client, stub, token, origin=HTTPS):
    """Prepare a real (non-remote-step) request in this session; its id is known to the click routes."""
    approve = {"action": "approve", "key": "DEMO-0001", "gate": "requirements"}
    res = step(client, stub, "prepare", approve, token, origin)
    assert res.status_code == 200, res.text
    return res.json()["request_id"]


def test_an_unprepared_request_id_on_the_sign_route_is_not_found(ungated, remote, stub, script):
    """Unknown id means refuse (R2 authorize), before any step-up check: nothing reaches the signer."""
    script.features = FEATURES
    body = dict(GATES[-1][1])
    res = step(ungated, stub, "sign", body, remote, HTTPS)
    assert res.status_code == 404 and res.json()["error"]["code"] == "not_found", res.text
    assert not [c for cl in script.clients for c in cl.calls if c[0] in ("prepare", "sign")]


def test_an_unprepared_request_id_is_not_found_for_a_local_session_too(loop, local, stub, script):
    script.features = FEATURES
    res = step(loop, stub, "sign", dict(GATES[-1][1]), local, ORIGIN)
    assert res.status_code == 404 and res.json()["error"]["code"] == "not_found", res.text
    assert not [c for cl in script.clients for c in cl.calls if c[0] == "sign"]


@pytest.mark.parametrize(("path", "body"), GATES)
def test_a_remote_session_on_the_front_gets_each_step_up_refusal(ungated, remote, stub, script, path, body):
    """The handler's own refusal, with the route table off (the table lets these through to the handler anyway)."""
    script.features = FEATURES
    if path == "sign":  # a sign only reaches the step-up check for a request this session really prepared
        body = {**body, "request_id": prepared_id(ungated, stub, remote)}
    res = step(ungated, stub, path, body, remote, HTTPS)
    assert res.status_code == 403, res.text
    error = res.json()["error"]
    assert error["code"] == "passkey.stepup_required" and NEEDS in error["message"]
    assert [
        c for cl in script.clients for c in cl.calls if c[0] in ("runner", "rotate", "custody_on", "custody_off")
    ] == []
    calls = [c[0] for cl in script.clients for c in cl.calls if c[0] in ("prepare", "sign")]
    assert calls == (["prepare"] if path == "sign" else [])  # sign: only the set-up prepare, never a signature


@pytest.mark.parametrize(("path", "body"), GATES)
@pytest.mark.parametrize("kind", ["remote", "passkey", "hub", "", None])
def test_any_session_kind_that_is_not_local_is_refused_by_the_route_itself(
    loop, local, auth, stub, script, path, body, kind
):
    """Defence in depth: the route does not rely on the listener binding. A session of another kind that reached it
    (the binding broken, a new kind added) is refused, fail closed."""
    script.features = FEATURES
    if path == "sign":  # prepared while still a plain local session; then the kind changes under it
        body = {**body, "request_id": prepared_id(loop, stub, local, ORIGIN)}
    auth.kinds[local] = kind
    res = step(loop, stub, path, body, local, ORIGIN)
    assert res.status_code == 403, (kind, res.text)
    assert res.json()["error"]["code"] == "passkey.stepup_required"
    assert not [
        c for cl in script.clients for c in cl.calls if c[0] in ("runner", "rotate", "custody_on", "custody_off")
    ]


def test_the_same_requests_pass_the_gates_for_a_local_session(loop, local, stub, script):
    script.features = FEATURES
    script.rotation = rotate_offer(stub.ws)
    assert step(loop, stub, "runner/review", {"on": True}, local, ORIGIN).status_code == 200
    assert step(loop, stub, "rotate/seal", {}, local, ORIGIN).status_code == 200
    script.enabled = script.available = False
    assert step(loop, stub, "custody", {"on": True, "resume": True}, local, ORIGIN).status_code == 200


def test_turning_things_off_stays_a_plain_click_on_a_remote_session(front, remote, stub, script):
    script.features = FEATURES
    res = step(front, stub, "runner/review", {"on": False}, remote, HTTPS)
    assert res.status_code == 200
    assert step(front, stub, "runner", {"on": False, "view_digest": HASH}, remote, HTTPS).status_code == 200
    assert step(front, stub, "custody", {"on": False}, remote, HTTPS).status_code == 200


def test_no_ticket_is_minted_on_a_listener_with_terminals_off(gateway, auth, tmp_path, stub, remote):
    app = terminals_app(gateway, auth, tmp_path, on=False)
    front = TestClient(Front(app), base_url=HTTPS, headers={"X-Orch-Client": SOURCE, "X-Orch-Click-Contract": "4"})
    res = front.post(f"/api/w/{stub.ws}/runs/{RUN}/attach", json={}, headers=bearer(remote, HTTPS))
    assert res.status_code == 403 and "ticket" not in res.text  # the route gate: terminals are off here
    assert app.state.term_tickets.items == {}


def test_a_ticket_redeems_only_on_the_listener_it_was_minted_for(gateway, auth, tmp_path, stub, remote, local):
    from starlette.websockets import WebSocketDisconnect

    app = terminals_app(gateway, auth, tmp_path, on=True)
    loop = TestClient(app, base_url=ORIGIN, headers={"X-Orch-Click-Contract": "4"})
    front = TestClient(Front(app), base_url=HTTPS, headers={"X-Orch-Client": SOURCE, "X-Orch-Click-Contract": "4"})
    minted = loop.post(f"/api/w/{stub.ws}/runs/{RUN}/attach", json={}, headers=bearer(local, ORIGIN))
    assert minted.status_code == 200
    ticket = minted.json()["ticket"]
    # the same ticket on the front's socket: refused (4401) whatever the front's terminal switch says
    with front.websocket_connect("/term", headers={"host": NAME, "origin": HTTPS}) as ws:
        ws.send_json({"ticket": ticket})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4401
    assert app.state.term_tickets.redeem(ticket) is None  # and it is spent
    # a remote session can mint on the front when the switch is on; that ticket is dead on loopback
    ticket = front.post(f"/api/w/{stub.ws}/runs/{RUN}/attach", json={}, headers=bearer(remote, HTTPS)).json()["ticket"]
    with loop.websocket_connect("/term", headers={"host": f"127.0.0.1:{PORT}", "origin": ORIGIN}) as ws:
        ws.send_json({"ticket": ticket})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4401


def test_a_ticket_whose_session_moved_listener_is_refused(gateway, auth, tmp_path, stub, local):
    from starlette.websockets import WebSocketDisconnect

    app = terminals_app(gateway, auth, tmp_path, on=True)
    loop = TestClient(app, base_url=ORIGIN, headers={"X-Orch-Click-Contract": "4"})
    ticket = loop.post(f"/api/w/{stub.ws}/runs/{RUN}/attach", json={}, headers=bearer(local, ORIGIN)).json()["ticket"]
    auth.listeners[local] = "remote"  # defence in depth: the session no longer belongs to the socket's listener
    with loop.websocket_connect("/term", headers={"host": f"127.0.0.1:{PORT}", "origin": ORIGIN}) as ws:
        ws.send_json({"ticket": ticket})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4401


def test_terminals_open_is_refused_on_a_listener_with_terminals_off(gateway, auth, tmp_path, stub, remote):
    app = terminals_app(gateway, auth, tmp_path, on=False)
    front = TestClient(Front(app), base_url=HTTPS, headers={"X-Orch-Client": SOURCE, "X-Orch-Click-Contract": "4"})
    res = front.post(
        f"/api/w/{stub.ws}/terminals/open",
        json={"spec": "a" * 64, "idem": "a_" + "0" * 26},
        headers=bearer(remote, HTTPS),
    )
    assert res.status_code == 403
