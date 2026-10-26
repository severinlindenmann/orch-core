# ruff: noqa: F811, F401
"""One action intent -> at most one request -> at most one signature. The guard is the host's idem record; these tests
check that the idem is a pure function of the action (never of the tab, session or browser body), and that the
probe of the previous review (two tabs, a prepare racing an in-flight sign) now yields ONE request by construction."""

import re
import threading
import time

import pytest
from test_dashboard_click import (  # noqa: F401  # noqa: F401
    HASH,
    REQ,
    Alive,
    click_start,
    comment,
    hdr,
    host_check,
    other_session,
    prepare,
    script,
    sign_lost,
    url,
)

from orch_dashboard.intent import gate_base, intent_idem, question_base

SHAPE = re.compile(r"^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
P, WS = "p_" + "1" * 32, "2" * 32
ARGS = {"ref": "DEMO-0001", "message": "hi"}


def test_the_idem_has_the_hosts_exact_shape_and_is_deterministic():
    one = intent_idem(P, WS, "log", ARGS, None)
    assert one == "a_1G4FN07BN9DMNPC2W40K0HP35H"  # fixed vector from the original retry=0 derivation
    assert SHAPE.fullmatch(one) and one == intent_idem(P, WS, "log", dict(ARGS), None)
    for n in range(200):  # the shape holds for every input: first char 0-7, 25 Crockford chars
        assert SHAPE.fullmatch(intent_idem(P, WS, "log", {**ARGS, "message": str(n)}, n))


def test_the_idem_does_not_depend_on_key_order():
    a = intent_idem(P, WS, "set", {"ref": "K", "pairs": ["a=1"]}, {"gen": 1, "hash": "x"})
    b = intent_idem(P, WS, "set", {"pairs": ["a=1"], "ref": "K"}, {"hash": "x", "gen": 1})
    assert a == b


def test_every_part_of_the_intent_changes_it():
    base = intent_idem(P, WS, "log", ARGS, "r1")
    others = [
        intent_idem("p_" + "9" * 32, WS, "log", ARGS, "r1"),  # another person
        intent_idem(P, "3" * 32, "log", ARGS, "r1"),  # another workspace
        intent_idem(P, WS, "close", ARGS, "r1"),  # another op
        intent_idem(P, WS, "log", {**ARGS, "message": "ho"}, "r1"),  # other args
        intent_idem(P, WS, "log", ARGS, "r2"),  # the base moved
        intent_idem(P, WS, "log", ARGS, "r1", retry=1),  # an explicit "do it again"
        intent_idem(P, WS, "log", ARGS, "r1", files={"cert": {"b64": "AA=="}}),
    ]
    assert len({base, *others}) == len(others) + 1


def test_bases():
    data = {
        "doc": {"key": "K", "title": "t", "claim": {"live": True}, "waiting": None},
        "status": "open",
        "gates": {"plan": {"gen": 2, "hash": "sha256:ab"}},
    }
    data["questions"] = [{"id": "Q2", "hash": "sha256:ab", "state": "open"}]
    assert question_base(data, "Q2") == {"id": "Q2", "hash": "sha256:ab"}
    data["questions"][0]["state"] = "answered"
    assert question_base(data, "Q2") == {"id": "Q2", "hash": "sha256:ab"}
    assert gate_base(data, "plan") == {"gate": "plan", "gen": 2, "hash": "sha256:ab"}
    moved = {**data, "gates": {"plan": {"gen": 3, "hash": "sha256:cd"}}}
    assert gate_base(data, "plan") != gate_base(moved, "plan")


def test_browser_body_order_and_whitespace_do_not_matter(client, token, stub, script):
    body = {"action": "comment", "key": "DEMO-0001", "message": "hello"}
    reordered = '{"message":  "hello",\n "key": "DEMO-0001",   "action": "comment"}'
    one = client.post(url(stub, "prepare"), json=body, headers=hdr(token))
    two = client.post(
        url(stub, "prepare"), content=reordered, headers={**hdr(token), "Content-Type": "application/json"}
    )
    assert one.status_code == two.status_code == 200 and one.json() == two.json() and script.prepared == 1
    keys = {c[1]["idem"] for c in script.clients[0].calls if c[0] == "prepare"}
    assert len(keys) == 1


def test_a_moved_base_is_a_new_intent(client, token, stub, script, rich, monkeypatch):
    env, ids = rich
    approve = {"action": "approve", "key": "DEMO-0001", "gate": "requirements"}
    first = client.post(url(stub, "prepare"), json=approve, headers=hdr(token)).json()
    from orch_dashboard import intent

    real = intent.gate_base
    monkeypatch.setattr("orch_dashboard.handoff.gate_base", lambda data, gate: {**real(data, gate), "gen": 99})
    second = client.post(url(stub, "prepare"), json=approve, headers=hdr(token)).json()
    assert first["request_id"] != second["request_id"] and script.prepared == 2


def test_probe_two_tabs_a_prepare_racing_an_inflight_sign_is_one_request_and_one_signature(
    client, token, stub, script, auth, rich, host_check
):
    """The previous review's probe (a second request of the same action prepared while the first sign is in flight):
    the second tab now gets the SAME request, so there is nothing second to sign."""
    prepare(client, token, stub, comment(1))
    go, entered = threading.Event(), threading.Event()

    def slow(op, args):
        if op == "sign":
            entered.set()
            go.wait(5)
            raise TimeoutError
        return script.handler(op, args)

    script.clients[0].handler = slow
    script.clients[0].proc = Alive()
    second = other_session(client, auth)
    out = {}
    ta = threading.Thread(target=lambda: out.__setitem__("a", sign_lost(client, token, stub)))
    ta.start()
    assert entered.wait(5)
    tb = threading.Thread(
        target=lambda: out.__setitem__("b", client.post(url(stub, "prepare"), json=comment(1), headers=hdr(second)))
    )
    tb.start()
    time.sleep(0.3)
    go.set()
    ta.join(10)
    tb.join(10)
    assert out["a"].json()["error"]["reason"] == "uncertain"
    assert out["b"].status_code == 200 and out["b"].json()["request_id"] == REQ  # the same request
    assert script.prepared == 1
    script.clients[0].handler = script.handler
    script.view = {"view": {**script.view["view"], "op": "log"}, "view_digest": HASH}
    # The second tab signs the one request: the signer/host record it once (the faked signer marks it submitted).
    signed = client.post(url(stub, "sign"), json={"request_id": REQ, "view_digest": HASH}, headers=hdr(second))
    assert signed.status_code == 200
    assert [c[1]["request_id"] for c in script.clients[0].calls if c[0] == "sign"] == [REQ, REQ]
    assert script.states == {REQ: "submitted"}


def test_logout_and_login_then_retry_never_makes_a_second_request(client, token, stub, script, auth):
    first = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(token)).json()
    other = other_session(client, auth)
    # The tab that holds it logs out while the other session never attached: the request is cancelled at the signer.
    client.post("/auth/logout", headers=hdr(token))
    again = client.post(url(stub, "prepare"), json=comment(1), headers=hdr(other))
    # #794: the cancelled attempt is ended, so pressing again is ONE fresh request (the server steps to the next retry)
    assert again.status_code == 200 and again.json()["request_id"] != REQ
    assert script.prepared == 2 and first["request_id"] == REQ
    assert client.post(url(stub, "prepare"), json=comment(1), headers=hdr(other)).json() == again.json()  # still one
    assert script.prepared == 2
    # A different intent, while its first request is still open: the other session attaches to it.
    third = client.post(url(stub, "prepare"), json=comment(2), headers=hdr(other)).json()
    assert client.post(url(stub, "prepare"), json=comment(2), headers=hdr(other)).json() == third


def test_send_prepare_expiry_does_not_renew_a_baseless_intent():
    from orch_dashboard.errors import HostError, fail
    from orch_dashboard.handoff import Plan, send_prepare

    plan = Plan("new", {"title": "A ticket"}, None, None, P)
    calls = []

    def prepare(idem):
        calls.append(idem)
        if idem == plan.idem(WS):
            raise fail("idem.expired", exit=2)
        return "new request"

    with pytest.raises(HostError) as caught:
        send_prepare(plan, WS, 0, prepare)
    assert caught.value.error["code"] == "intent.expired"
    assert "may have been signed" in caught.value.error["message"]  # Opus re-check N2
    assert caught.value.exit == 5 and caught.value.error["retryable"] is False
    assert calls == [plan.idem(WS)]
    assert send_prepare(plan, WS, 1, prepare) == "new request"
    assert calls == [plan.idem(WS), plan.idem(WS, retry=1)]


@pytest.mark.parametrize("click", [True, False])
@pytest.mark.parametrize(
    "action",
    [
        {"action": "comment", "message": "same comment"},
        {"action": "label", "label": "new-label"},
        {"action": "answer", "question": "Q2", "option": "csv"},
    ],
)
def test_agent_edits_between_tabs_keep_one_request(client, token, stub, script, rich, auth, click, action):
    env, _ = rich
    script.enabled = click
    body = {"key": "DEMO-0001", **action}
    endpoint = url(stub, "prepare") if click else f"/api/w/{stub.ws}/handoff"
    one = client.post(endpoint, json=body, headers=hdr(token))
    assert one.status_code == 200, one.json()
    uid = next(t.uid for t in env.store.state.tickets.values() if t.key == "DEMO-0001")
    env.tick()
    env.update(uid, body={"current_state": "agent working on T1"})
    env.tick()
    env.update(uid, {"ticket.priority": "low"})
    two = client.post(endpoint, json=body, headers=hdr(other_session(client, auth)))
    assert two.status_code == 200, two.json()
    field = "request_id" if click else "id"
    assert one.json()[field] == two.json()[field]
    if click:
        assert script.prepared == 1


def test_reprepare_after_signer_restart_adopts_request_before_sweep(client, token, stub, script, gateway, host_check):
    first = prepare(client, token, stub, comment(1)).json()

    class Dead:
        def poll(self):
            return 1

    script.clients[0].proc = Dead()
    gateway.click.last_spawn -= 10
    second = prepare(client, token, stub, comment(1))
    assert second.status_code == 200, second.json()
    assert second.json()["request_id"] == first["request_id"]
    assert first["request_id"] not in host_check.cancelled
    assert script.prepared == 1


def test_new_dashboard_link_keeps_same_host_request_without_auth_session(
    client, token, stub, script, gateway, rich, monkeypatch
):
    from conftest import ORIGIN, PORT
    from fastapi.testclient import TestClient

    from orch_dashboard.app import create_app
    from orch_dashboard.auth import Auth
    from orch_dashboard.hostlink import HostLink

    sent = []
    send = HostLink.send

    def capture(self, frame):
        sent.append(frame)
        return send(self, frame)

    monkeypatch.setattr(HostLink, "send", capture)
    script.enabled = False
    endpoint = f"/api/w/{stub.ws}/handoff"
    one = client.post(endpoint, json=comment(1), headers=hdr(token))
    assert one.status_code == 200
    gateway.links.pop(stub.ws).close()
    # A fresh app, authentication and all request tracking; only the host keeps the idem record.
    auth = Auth()
    restarted = TestClient(create_app(PORT, gateway, auth), base_url=ORIGIN)
    try:
        new_token = other_session(restarted, auth)
        two = restarted.post(endpoint, json=comment(1), headers=hdr(new_token))
        assert two.status_code == 200, two.json()
        assert two.json()["id"] == one.json()["id"]
        prepares = [f for f in sent if f.get("op") == "human.prepare"]
        assert len(prepares) == 2
        assert all("session" not in (f.get("auth") or {}) for f in prepares)
    finally:
        restarted.close()


@pytest.mark.parametrize("submitted_at", [None, "2026-10-13T15:20:00Z"])
@pytest.mark.parametrize("source", ["request", "row"])
def test_done_time_requires_committed_pending_sequence(submitted_at, source):
    from orch_dashboard.handoff import done_error

    prepared = {"request": {"state": "submitted"}, "op": "log"}
    row = {"state": "submitted", "seq": 7}
    (prepared["request"] if source == "request" else row)["submitted_at"] = submitted_at
    error = done_error(prepared, row).error
    assert error["code"] == "intent.done"
    assert error["signed_at"] == submitted_at
    row.update(seq=None, error={"code": "gate.stale"})
    error = done_error(prepared, row).error
    assert error["code"] == "intent.failed" and error["reason"] == "gate.stale"
    assert "did not land (gate.stale)" in error["message"]
    assert "not signed into the log" in error["message"]
    assert "signed_at" not in error and "Already done" not in error["message"]


def test_busy_and_host_fix_mapping():
    import json

    from orch_dashboard.errors import HostError, fail, host_response

    busy = host_response(fail("lock.busy"))
    assert busy.status_code == 503
    assert json.loads(busy.body)["exit"] == 9
    assert json.loads(busy.body)["error"]["retryable"] is True
    fix = {"argv": ["orch", "host", "convenience", "off", "--workspace", "DEMO"]}
    response = host_response(HostError({"code": "sign.convenience_readers", "fix": fix}))
    assert json.loads(response.body)["error"]["fix"] == fix
