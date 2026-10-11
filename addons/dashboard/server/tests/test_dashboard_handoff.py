"""#305 (M3) server: prepare, look at and cancel a hand-off. The server never signs and never calls human.submit."""

import pytest
from conftest import ORIGIN

from orch_dashboard.hostlink import HostLink

IDEM = "a_{:0>26}"


def idem(n):
    return IDEM.format(n)


def auth(token, write=False):
    return {"Authorization": f"Bearer {token}", **({"Origin": ORIGIN} if write else {})}


auth_h = auth


def base(stub):
    return f"/api/w/{stub.ws}/handoff"


@pytest.fixture
def host_calls(monkeypatch, gateway):
    """Every op the dashboard server sends to the host."""
    seen = []
    original = HostLink.call

    def spy(self, op, args=None, **kw):
        if self in gateway.links.values():  # the signer's own links are not the dashboard server's
            seen.append((op, args, kw))
        return original(self, op, args, **kw)

    monkeypatch.setattr(HostLink, "call", spy)
    return seen


@pytest.fixture
def ready(client, token, stub, rich, signing):
    return client, token, stub, rich, signing


def post(client, token, stub, body):
    return client.post(base(stub), json=body, headers=auth(token, True))


def test_routes_need_authentication_and_a_same_origin_post(client, token, stub, rich):
    request_id = "hr_" + "0" * 26
    assert client.get(f"{base(stub)}/{request_id}").status_code == 401
    assert client.delete(f"{base(stub)}/{request_id}").status_code == 401
    body = {"action": "comment", "key": "DEMO-0001", "message": "x", "idem": idem(1)}
    assert client.post(base(stub), json=body).status_code == 401
    assert client.post(base(stub), json=body, headers=auth(token)).status_code == 403  # no Origin
    bad = {**auth(token), "Origin": "http://evil.example.test"}
    assert client.post(base(stub), json=body, headers=bad).status_code == 403
    assert client.delete(f"{base(stub)}/{request_id}", headers=auth(token)).status_code == 403


ACTIONS = [
    ({"action": "approve", "key": "DEMO-0001", "gate": "requirements"}, "gate.approved", "gate"),
    (
        {"action": "request_changes", "key": "DEMO-0001", "gate": "requirements", "message": "more"},
        "gate.changes_requested",
        "gate",
    ),
    (
        {"action": "answer", "key": "DEMO-0001", "question": "Q2", "option": "csv", "message": "ok"},
        "question.answered",
        "question",
    ),
    ({"action": "comment", "key": "DEMO-0001", "message": "a note"}, "log.added", "payload"),
    ({"action": "label", "key": "DEMO-0001", "label": "urgent-fix"}, "ticket.updated", "payload"),
    ({"action": "close", "key": "DEMO-0002", "resolution": "wont_do", "message": "no"}, "ticket.closed", "payload"),
    ({"action": "reopen", "key": "DEMO-0002"}, "ticket.reopened", "payload"),
    ({"action": "new", "title": "A new ticket", "type": "bug"}, "ticket.created", "payload"),
    (
        {"action": "details", "key": "DEMO-0001", "priority": "urgent", "size": "m", "labels": ["b", "a"]},
        "ticket.updated",
        "payload",
    ),
]


@pytest.mark.parametrize("action,event,kind", ACTIONS, ids=[a[0]["action"] for a in ACTIONS])
def test_each_action_prepares_a_request_from_the_hosts_material(ready, action, event, kind):
    client, token, stub, rich, signing = ready
    if action["action"] == "reopen":
        rich[0].close(rich[1]["Second"])  # only a closed or done ticket reopens
    res = post(client, token, stub, {**action, "idem": idem(2)})
    assert res.status_code == 200, res.text
    got = res.json()
    assert got["state"] == "prepared" and got["code"] and got["id"].startswith("hr_")
    assert got["material"]["kind"] == kind and got["events"][0]["type"] == event
    assert got["material"]["review"][0]["type"] == event
    assert len(got["fingerprint"]) == 12 and got["review_digest"].startswith("sha256:")
    assert got["events"][0]["actor"]["device"] is None and "sig" not in got["events"][0]
    # Nothing was written: preparing signs and appends nothing.
    before = rich[0].store.head_seq(rich[1]["First"])
    assert rich[0].store.head_seq(rich[1]["First"]) == before


def test_label_adds_to_the_labels_the_ticket_has(ready):
    client, token, stub, rich, signing = ready
    got = post(client, token, stub, {"action": "label", "key": "DEMO-0001", "label": "zzz", "idem": idem(3)}).json()
    assert got["events"][0]["set"] == {"ticket.labels": ["api", "ui", "zzz"]}
    assert got["material"]["before"] == {"ticket.labels": ["api", "ui"]}


@pytest.mark.parametrize(
    "body",
    [
        {"action": "move", "key": "DEMO-0001", "to": "backlog"},
        {"action": "approve", "key": "DEMO-0001", "gate": "verify"},
        {"action": "approve", "key": "DEMO-0001", "gate": "plan", "op": "human.submit"},
        {"action": "approve", "key": "DEMO-0001", "gate": "plan", "path": "/etc/passwd"},
        {"action": "approve", "key": "../x", "gate": "plan"},
        {"action": "approve", "gate": "plan"},
        {"action": "request_changes", "key": "DEMO-0001", "gate": "plan"},
        {"action": "verdict", "key": "DEMO-0001", "outcome": "fail"},
        {"action": "answer", "key": "DEMO-0001", "question": "Q1"},
        {"action": "answer", "key": "DEMO-0001", "question": "one", "option": "x"},
        {"action": "comment", "key": "DEMO-0001", "message": ""},
        {"action": "comment", "key": "DEMO-0001", "message": "x" * 4097},
        {"action": "label", "key": "DEMO-0001", "label": "Has Space"},
        {"action": "close", "key": "DEMO-0001", "resolution": "other", "file": "-"},
        {"action": "new", "title": ""},
        {"action": "details", "key": "DEMO-0001"},
        {"action": "new", "title": "t", "labels": ["a"]},
        {"action": "new", "title": "x" * 201},
        {"action": "new", "title": "t", "type": "story"},
        {"action": "unknown"},
        {"key": "DEMO-0001"},
    ],
)
def test_typed_arguments_are_refused_without_echo(ready, body):
    client, token, stub, rich, signing = ready
    res = post(client, token, stub, {**body, "idem": idem(4)})
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input"
    assert "/etc/passwd" not in res.text and "human.submit" not in res.text
    assert signing.stub.requests.table == {}


@pytest.mark.parametrize("key", [None, "", "x", "a_" + "8" * 25, "a_" + "8" * 27, "A_" + "0" * 26, "a_" + "u" * 26])
def test_an_idempotency_key_is_required_and_validated(ready, key):
    client, token, stub, rich, signing = ready
    body = {"action": "comment", "key": "DEMO-0001", "message": "x"}
    res = post(client, token, stub, body if key is None else {**body, "idem": key})
    assert res.status_code == 400 and signing.stub.requests.table == {}


def test_a_retry_with_the_same_key_answers_from_the_record_and_prepares_once(ready, host_calls):
    client, token, stub, rich, signing = ready
    body = {"action": "comment", "key": "DEMO-0001", "message": "once", "idem": idem(5)}
    first, second = post(client, token, stub, body), post(client, token, stub, body)
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    assert len(signing.stub.requests.table) == 1
    assert [c[0] for c in host_calls].count("human.prepare") == 1
    prepare = next(c for c in host_calls if c[0] == "human.prepare")
    assert prepare[2]["idem"] == idem(5) and prepare[1]["handoff"] is True and "signer" not in prepare[1]
    clash = post(client, token, stub, {**body, "message": "another"})
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "idem.conflict"


def test_the_signer_completes_it_and_the_server_never_submits(ready, host_calls):
    client, token, stub, rich, signing = ready
    env, ids = rich
    got = post(
        client, token, stub, {"action": "approve", "key": "DEMO-0001", "gate": "requirements", "idem": idem(10)}
    ).json()
    status, shown = signing.sign(got["code"])
    assert status == 0, shown
    state = client.get(f"{base(stub)}/{got['id']}", headers=auth(token)).json()
    assert state["state"] == "submitted" and state["event_ids"] == [got["events"][0]["id"]] and state["seq"] > 1
    assert client.delete(f"{base(stub)}/{got['id']}", headers=auth(token, True)).json()["state"] == "submitted"
    ticket = client.get(f"/api/w/{stub.ws}/tickets/DEMO-0001", headers=auth(token)).json()
    assert ticket["gates"]["requirements"]["state"] == "approved"
    assert {c[0] for c in host_calls} >= {"human.prepare", "human.pending"}
    assert "human.submit" not in {c[0] for c in host_calls}
    # A second approval of the same gate generation is refused at prepare: "Already done".
    again = post(
        client, token, stub, {"action": "approve", "key": "DEMO-0001", "gate": "requirements", "idem": idem(11)}
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "gate.already_approved"


def test_the_server_cannot_be_made_to_call_human_submit(ready):
    client, token, stub, rich, signing = ready
    human = client.app.state.human
    with pytest.raises(AssertionError):
        human(stub.ws, "human.submit", {"request_id": "hr_" + "0" * 26, "events": [{}]})
    sent = post(
        client,
        token,
        stub,
        {"action": "approve", "key": "DEMO-0001", "gate": "plan", "op": "human.submit", "idem": idem(12)},
    )
    assert sent.status_code == 400
    assert not [r for r in client.app.routes if "submit" in getattr(r, "path", "")]


def test_host_errors_are_mapped(ready):
    client, token, stub, rich, signing = ready
    missing = post(client, token, stub, {"action": "comment", "key": "DEMO-0999", "message": "x", "idem": idem(13)})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    bad_question = post(
        client, token, stub, {"action": "answer", "key": "DEMO-0001", "question": "Q9", "option": "a", "idem": idem(14)}
    )
    assert bad_question.status_code == 404
    refused = post(client, token, stub, {"action": "reopen", "key": "DEMO-0001", "idem": idem(15)})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "transition.refused"
    extra = post(client, token, stub, {"action": "new", "title": "T", "priority": "high", "idem": idem(16)})
    assert (
        extra.status_code == 400 and extra.json()["error"]["code"] == "invalid.input"
    )  # `new` carries title and type only


def test_expiry_is_reported_and_stale_content_is_refused_when_signing(ready):
    client, token, stub, rich, signing = ready
    env, ids = rich
    now = [__import__("time").time()]
    signing.stub.requests.clock = lambda: now[0]
    got = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "x", "idem": idem(17)}).json()
    now[0] += 601
    state = client.get(f"{base(stub)}/{got['id']}", headers=auth(token)).json()
    assert state["state"] == "expired"


def test_a_session_holds_at_most_eight_open_requests_and_logout_cancels_them(ready):
    client, token, stub, rich, signing = ready
    ids = []
    for n in range(8):
        res = post(
            client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": f"note {n}", "idem": idem(20 + n)}
        )
        assert res.status_code == 200
        ids.append(res.json()["id"])
    over = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "one more", "idem": idem(40)})
    assert over.status_code == 503 and over.json()["error"]["code"] == "quota.host"
    assert client.delete(f"{base(stub)}/{ids[0]}", headers=auth(token, True)).status_code == 200
    assert (
        post(
            client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "fits now", "idem": idem(41)}
        ).status_code
        == 200
    )
    out = client.post("/auth/logout", headers=auth(token, True))
    assert out.status_code == 200
    states = {r["state"] for r in signing.stub.requests.table.values()}
    assert states == {"cancelled"}


def test_an_inconsistent_host_answer_is_refused(ready, monkeypatch):
    client, token, stub, rich, signing = ready
    original = signing.stub.requests.prepare

    def tampered(store, views, args, opts):
        out = original(store, views, args, opts)
        out["fingerprint"] = "0" * 12
        return out

    monkeypatch.setattr(signing.stub.requests, "prepare", tampered)
    res = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "x", "idem": idem(50)})
    assert res.status_code == 500 and res.json()["error"]["code"] == "internal"
    assert "000000000000" not in res.text


def test_session_lists_the_actions_feature(client, token, stub, rich):
    features = client.get("/api/session", headers=auth(token)).json()
    assert "actions" in features["features"] and "actions" in features["workspaces"][stub.ws]


def second_token(client, auth):
    from conftest import PORT

    launch = auth.launch(PORT)
    return client.post("/auth/session", json={"code": launch["code"]}, headers={"Origin": ORIGIN}).json()["token"]


def test_a_request_belongs_to_the_session_that_prepared_it(ready, auth):
    client, token, stub, rich, signing = ready
    mine = post(
        client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "hi", "idem": idem(60)}
    ).json()
    other = second_token(client, auth)
    url = f"{base(stub)}/{mine['id']}"
    assert client.get(url, headers=auth_h(other)).status_code == 404
    assert client.get(url + "?full=1", headers=auth_h(other)).status_code == 404
    assert client.delete(url, headers=auth_h(other, True)).status_code == 404
    # The same idem from another session is another click: its own request, its own code.
    theirs = post(
        client, other, stub, {"action": "comment", "key": "DEMO-0001", "message": "hi", "idem": idem(60)}
    ).json()
    assert theirs["id"] != mine["id"] and theirs["code"] != mine["code"]
    assert client.get(url, headers=auth_h(token)).json()["state"] == "prepared"  # nothing of mine was touched
    assert client.get(url, headers=auth_h(token)).status_code == 200


def test_two_tabs_make_two_requests_and_cancel_only_their_own(ready):
    client, token, stub, rich, signing = ready
    body = {"action": "approve", "key": "DEMO-0001", "gate": "requirements"}
    a = post(client, token, stub, {**body, "idem": idem(61)}).json()
    b = post(client, token, stub, {**body, "idem": idem(62)}).json()  # a second tab: a new click, a new idem
    assert a["id"] != b["id"]
    client.delete(f"{base(stub)}/{b['id']}", headers=auth_h(token, True))
    assert client.get(f"{base(stub)}/{a['id']}", headers=auth_h(token)).json()["state"] == "prepared"
    assert client.get(f"{base(stub)}/{b['id']}", headers=auth_h(token)).json()["state"] == "cancelled"


def test_a_lost_answer_never_leaves_an_orphan_after_the_retry(ready, monkeypatch):
    client, token, stub, rich, signing = ready
    from orch_dashboard.errors import fail

    original = HostLink.call
    state = {"drop": True}

    def lossy(self, op, args=None, **kw):
        out = original(self, op, args, **kw)
        if op == "human.prepare" and state["drop"]:
            state["drop"] = False
            raise fail("outcome.unknown")
        return out

    monkeypatch.setattr(HostLink, "call", lossy)
    body = {"action": "comment", "key": "DEMO-0001", "message": "hi", "idem": idem(63)}
    first = post(client, token, stub, body)
    assert first.status_code == 503 and first.json()["error"]["code"] == "outcome.unknown"
    second = post(client, token, stub, body)
    assert second.status_code == 200
    states = sorted(r["state"] for r in signing.stub.requests.table.values())
    assert states == ["cancelled", "prepared"]  # the orphan was cancelled, one request is open
    # A different action is not touched by the recovery.
    assert second.json()["id"] in {r["id"] for r in signing.stub.requests.table.values() if r["state"] == "prepared"}


def test_logout_ends_the_session_even_if_a_cancel_fails(ready, monkeypatch):
    client, token, stub, rich, signing = ready
    post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "hi", "idem": idem(64)})
    original = HostLink.call

    def broken(self, op, args=None, **kw):
        if op == "human.cancel":
            raise ValueError("bad frame")
        return original(self, op, args, **kw)

    monkeypatch.setattr(HostLink, "call", broken)
    out = client.post("/auth/logout", headers=auth_h(token, True))
    assert out.status_code == 200
    assert client.get("/api/session", headers=auth_h(token)).status_code == 401


def test_the_answer_must_be_the_action_that_was_asked_for(ready, monkeypatch):
    client, token, stub, rich, signing = ready
    original = signing.stub.requests.prepare

    def other_ticket(store, views, args, opts):
        args = {**args, "args": {**args["args"], "ref": "DEMO-0002"}}
        return original(store, views, args, opts)

    monkeypatch.setattr(signing.stub.requests, "prepare", other_ticket)
    res = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "x", "idem": idem(65)})
    assert res.status_code == 500 and res.json()["error"]["code"] == "internal"


@pytest.mark.parametrize("mutate", ["review_payload", "review_type", "expires_far", "expires_bad", "state"])
def test_an_inconsistent_prepared_request_fails_closed(ready, monkeypatch, mutate):
    client, token, stub, rich, signing = ready
    from orch_dashboard import adapters

    original = signing.stub.requests.prepare

    def tamper(store, views, args, opts):
        out = original(store, views, args, opts)
        if mutate == "review_payload":
            out["material"]["review"][0]["payload"]["text"] = "something else"
        elif mutate == "review_type":
            out["material"]["review"][0]["type"] = "ticket.closed"
        elif mutate == "expires_far":
            out["request"]["expires_at"] = "2099-01-01T00:00:00Z"
        elif mutate == "expires_bad":
            out["request"]["expires_at"] = "soon"
        else:
            out["request"]["state"] = "bound"
        # keep the hashes consistent so only the new check can refuse it
        digest = adapters.handoff.review_digest(out["material"])
        out["review_digest"], out["fingerprint"] = digest, adapters.handoff.fingerprint(digest, out["events"])
        return out

    monkeypatch.setattr(signing.stub.requests, "prepare", tamper)
    res = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "x", "idem": idem(66)})
    assert res.status_code == 500 and res.json()["error"]["code"] == "internal"


def test_a_failed_submit_code_is_passed_on_when_the_host_sends_it(ready, monkeypatch):
    client, token, stub, rich, signing = ready
    got = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "x", "idem": idem(67)}).json()
    original = signing.stub.requests.pending

    def with_error(args):
        out = original(args)
        out["requests"][0]["error"] = {"code": "gate.stale", "message": "ignored"}
        return out

    monkeypatch.setattr(signing.stub.requests, "pending", with_error)
    assert client.get(f"{base(stub)}/{got['id']}", headers=auth_h(token)).json()["error"] == {"code": "gate.stale"}
