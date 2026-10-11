"""#305 (M3) server: prepare, look at and cancel a hand-off. The server never signs and never calls human.submit."""

import pytest
from conftest import ORIGIN

from orch_dashboard.hostlink import HostLink

IDEM = "a_{:0>26}"


def idem(n):
    return IDEM.format(n)


def auth(token, write=False):
    return {"Authorization": f"Bearer {token}", **({"Origin": ORIGIN} if write else {})}


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


def test_one_open_request_per_action_is_reused_not_prepared_again(ready):
    client, token, stub, rich, signing = ready
    body = {"action": "approve", "key": "DEMO-0001", "gate": "requirements"}
    first = post(client, token, stub, {**body, "idem": idem(6)}).json()
    again = post(client, token, stub, {**body, "idem": idem(7)}).json()  # a reopened panel: a new click, a new key
    assert again["id"] == first["id"] and len(signing.stub.requests.table) == 1
    cancelled = client.delete(f"{base(stub)}/{first['id']}", headers=auth(token, True))
    assert cancelled.json() == {"id": first["id"], "state": "cancelled"}
    fresh = post(client, token, stub, {**body, "idem": idem(8)}).json()
    assert fresh["id"] != first["id"]  # a cancelled request is never reused


def test_pending_state_full_view_cancel_and_ids(ready):
    client, token, stub, rich, signing = ready
    got = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "x", "idem": idem(9)}).json()
    url = f"{base(stub)}/{got['id']}"
    state = client.get(url, headers=auth(token)).json()
    assert (
        state["state"] == "prepared"
        and state["key"] == "DEMO-0001"
        and state["event_ids"] == []
        and "request" not in state
    )
    full = client.get(url + "?full=1", headers=auth(token)).json()
    assert full["request"]["material"] == got["material"] and full["request"]["fingerprint"] == got["fingerprint"]
    assert client.get(f"{base(stub)}/hr_{'0' * 26}", headers=auth(token)).status_code == 404
    assert client.get(f"{base(stub)}/nope", headers=auth(token)).status_code == 400
    assert client.get(url + "?x=1", headers=auth(token)).status_code in (200, 400)
    done = client.delete(url, headers=auth(token, True))
    assert done.status_code == 200 and done.json()["state"] == "cancelled"
    assert client.get(url, headers=auth(token)).json()["state"] == "cancelled"
    assert client.delete(url, headers=auth(token, True)).json()["state"] == "cancelled"  # idempotent


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
    stub_only = post(client, token, stub, {"action": "new", "title": "T", "priority": "high", "idem": idem(16)})
    assert stub_only.status_code == 400 and stub_only.json()["error"]["code"] == "invalid.input"


def test_expiry_is_reported_and_stale_content_is_refused_when_signing(ready):
    client, token, stub, rich, signing = ready
    env, ids = rich
    now = [1_000_000.0]
    signing.stub.requests.clock = lambda: now[0]
    got = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "x", "idem": idem(17)}).json()
    now[0] += 601
    state = client.get(f"{base(stub)}/{got['id']}", headers=auth(token)).json()
    assert (
        state["state"] == "expired"
        and "request" not in client.get(f"{base(stub)}/{got['id']}?full=1", headers=auth(token)).json()
    )


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
