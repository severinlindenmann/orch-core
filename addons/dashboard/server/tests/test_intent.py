# ruff: noqa: F811, F401
"""One action intent -> at most one request -> at most one signature. The guard is the host's idem record; these tests
check that the idem is a pure function of the action (never of the tab, session or browser body), and that the
probe of the previous review (two tabs, a prepare racing an in-flight sign) now yields ONE request by construction."""

import re
import threading
import time

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

from orch_dashboard.intent import gate_base, intent_idem, ticket_revision

SHAPE = re.compile(r"^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
P, WS = "p_" + "1" * 32, "2" * 32
ARGS = {"ref": "DEMO-0001", "message": "hi"}


def test_the_idem_has_the_hosts_exact_shape_and_is_deterministic():
    one = intent_idem(P, WS, "log", ARGS, None)
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
        intent_idem(P, WS, "log", ARGS, "r1", epoch=1),  # the host forgot the idem
        intent_idem(P, WS, "log", ARGS, "r1", files={"cert": {"b64": "AA=="}}),
    ]
    assert len({base, *others}) == len(others) + 1


def test_bases():
    data = {
        "doc": {"key": "K", "title": "t", "claim": {"live": True}, "waiting": None},
        "status": "open",
        "gates": {"plan": {"gen": 2, "hash": "sha256:ab"}},
    }
    flap = {**data, "doc": {**data["doc"], "claim": {"live": False}, "waiting": "x"}}
    assert ticket_revision(data) == ticket_revision(flap)  # an agent working does not move the revision
    assert ticket_revision(data) != ticket_revision({**data, "doc": {**data["doc"], "title": "u"}})
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
    assert again.json()["error"]["code"] == "intent.ended"  # not a new request: "Start again" is an explicit click
    assert script.prepared == 1 and first["request_id"] == REQ
    # And while the first is still open, the other session attaches to it rather than making one.
    third = client.post(url(stub, "prepare"), json=comment(2), headers=hdr(other)).json()
    assert client.post(url(stub, "prepare"), json=comment(2), headers=hdr(other)).json() == third
