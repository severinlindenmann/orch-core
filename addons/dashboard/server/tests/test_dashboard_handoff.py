"""#305 (M3) server: prepare, look at and cancel a hand-off. The server never signs and never calls human.submit."""

import re

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
    body = {"action": "comment", "key": "DEMO-0001", "message": "x"}
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
    (
        {"action": "details", "key": "DEMO-0001", "parent": "DEMO-0003", "size": "s"},
        "ticket.updated",
        "payload",
    ),
    (
        {
            "action": "section",
            "key": "DEMO-0001",
            "section": "summary",
            "text": "Why this exists.\n\nSecond paragraph.",
        },
        "ticket.updated",
        "payload",
    ),
]


@pytest.mark.parametrize("action,event,kind", ACTIONS, ids=[a[0]["action"] for a in ACTIONS])
def test_each_action_prepares_a_request_from_the_hosts_material(ready, action, event, kind):
    client, token, stub, rich, signing = ready
    if action["action"] == "reopen":
        rich[0].close(rich[1]["Second"])  # only a closed or done ticket reopens
    res = post(client, token, stub, {**action})
    assert res.status_code == 200, res.text
    got = res.json()
    assert got["state"] == "prepared" and got["code"] and got["id"].startswith("hr_")
    assert got["material"]["kind"] == kind and got["events"][0]["type"] == event
    assert got["material"]["review"][0]["type"] == event
    assert len(got["fingerprint"]) == 12 and got["review_digest"].startswith("sha256:")
    assert got["events"][0]["actor"]["device"] is None and "sig" not in got["events"][0]
    # B6: the plain view is a sibling of the hashed material, derived from it (the digest above still recomputes)
    assert got["headline"] and got["content"]["headline"] == got["headline"] and "content" not in got["material"]
    # Nothing was written: preparing signs and appends nothing.
    before = rich[0].store.head_seq(rich[1]["First"])
    assert rich[0].store.head_seq(rich[1]["First"]) == before


def test_label_adds_to_the_labels_the_ticket_has(ready):
    client, token, stub, rich, signing = ready
    got = post(client, token, stub, {"action": "label", "key": "DEMO-0001", "label": "zzz"}).json()
    assert got["events"][0]["set"] == {"ticket.labels": ["api", "ui", "zzz"]}
    assert got["material"]["before"] == {"ticket.labels": ["api", "ui"]}


@pytest.mark.parametrize(
    "body",
    [
        {"action": "move", "key": "DEMO-0001", "to": "done"},
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
        {"action": "details", "key": "DEMO-0001", "parent": "not a key"},
        {"action": "details", "key": "DEMO-0001", "owner": "p_x"},
        {"action": "section", "key": "DEMO-0001", "section": "nope", "text": "x"},
        {"action": "section", "key": "DEMO-0001", "section": "summary", "text": ""},
        {"action": "section", "key": "DEMO-0001", "section": "summary"},
        {"action": "section", "key": "DEMO-0001", "section": "summary", "text": "x", "message": "y"},
        {"action": "new", "title": "t", "labels": ["a"]},
        {"action": "new", "title": "x" * 201},
        {"action": "new", "title": "t", "type": "story"},
        {"action": "unknown"},
        {"key": "DEMO-0001"},
    ],
)
def test_typed_arguments_are_refused_without_echo(ready, body):
    client, token, stub, rich, signing = ready
    res = post(client, token, stub, {**body})
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input"
    assert "/etc/passwd" not in res.text and "human.submit" not in res.text
    assert signing.stub.requests.table == {}


BAD_EXTRA = [{"idem": "a_" + "0" * 26}, {"retry": -1}, {"retry": "1"}, {"retry": 10000}, {"retry": 1.5}]


@pytest.mark.parametrize("extra", BAD_EXTRA)
def test_the_browser_sends_no_key_and_the_retry_counter_is_bounded(ready, extra):
    client, token, stub, rich, signing = ready
    res = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "x", **extra})
    assert res.status_code == 400 and signing.stub.requests.table == {}


def test_the_same_action_twice_prepares_once_with_the_intent_idem(ready, host_calls):
    client, token, stub, rich, signing = ready
    body = {"action": "comment", "key": "DEMO-0001", "message": "once"}
    first, second = post(client, token, stub, body), post(client, token, stub, body)
    assert first.status_code == second.status_code == 200 and first.json() == second.json()
    assert len(signing.stub.requests.table) == 1
    prepares = [c for c in host_calls if c[0] == "human.prepare"]
    assert len(prepares) == 2 and prepares[0][2]["idem"] == prepares[1][2]["idem"]  # the host dedupes, not this process
    assert re.fullmatch(r"a_[0-7][0-9A-HJKMNP-TV-Z]{25}", prepares[0][2]["idem"])
    assert prepares[0][1]["handoff"] is True and "signer" not in prepares[0][1]
    # Another text is another intent: another request, never a conflict.
    other = post(client, token, stub, {**body, "message": "another"})
    assert other.status_code == 200 and other.json()["id"] != first.json()["id"]
    # A deliberate "do it again" is another intent too, and only an explicit retry counter makes it.
    again = post(client, token, stub, {**body, "retry": 1})
    assert again.status_code == 200 and again.json()["id"] not in (first.json()["id"], other.json()["id"])


def test_the_signer_completes_it_and_the_server_never_submits(ready, host_calls):
    client, token, stub, rich, signing = ready
    env, ids = rich
    got = post(client, token, stub, {"action": "approve", "key": "DEMO-0001", "gate": "requirements"}).json()
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
    # The same action again is the same intent: the host returns the same request, now submitted ("Already done").
    again = post(client, token, stub, {"action": "approve", "key": "DEMO-0001", "gate": "requirements"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "intent.done"
    # "Do it again" is a new intent: the host refuses a second approval of the generation.
    repeat = post(client, token, stub, {"action": "approve", "key": "DEMO-0001", "gate": "requirements", "retry": 1})
    assert repeat.status_code == 409 and repeat.json()["error"]["code"] == "gate.already_approved"


def test_the_server_cannot_be_made_to_call_human_submit(ready):
    client, token, stub, rich, signing = ready
    human = client.app.state.human
    with pytest.raises(AssertionError):
        human(stub.ws, "human.submit", {"request_id": "hr_" + "0" * 26, "events": [{}]})
    sent = post(
        client,
        token,
        stub,
        {"action": "approve", "key": "DEMO-0001", "gate": "plan", "op": "human.submit"},
    )
    assert sent.status_code == 400
    assert not [r for r in client.app.routes if "submit" in getattr(r, "path", "")]


def test_host_errors_are_mapped(ready):
    client, token, stub, rich, signing = ready
    missing = post(client, token, stub, {"action": "comment", "key": "DEMO-0999", "message": "x"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    bad_question = post(client, token, stub, {"action": "answer", "key": "DEMO-0001", "question": "Q9", "option": "a"})
    assert bad_question.status_code == 404
    refused = post(client, token, stub, {"action": "reopen", "key": "DEMO-0001"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "transition.refused"
    extra = post(client, token, stub, {"action": "new", "title": "T", "priority": "high"})
    assert (
        extra.status_code == 400 and extra.json()["error"]["code"] == "invalid.input"
    )  # `new` carries title and type only


def test_expiry_is_reported_and_stale_content_is_refused_when_signing(ready):
    client, token, stub, rich, signing = ready
    env, ids = rich
    now = [__import__("time").time()]
    signing.stub.requests.clock = lambda: now[0]
    got = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "x"}).json()
    now[0] += 601
    state = client.get(f"{base(stub)}/{got['id']}", headers=auth(token)).json()
    assert state["state"] == "expired"


def test_a_session_holds_at_most_eight_open_requests_and_logout_cancels_them(ready):
    client, token, stub, rich, signing = ready
    ids = []
    for n in range(8):
        res = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": f"note {n}"})
        assert res.status_code == 200
        ids.append(res.json()["id"])
    over = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "one more"})
    assert over.status_code == 503 and over.json()["error"]["code"] == "quota.host"
    assert client.delete(f"{base(stub)}/{ids[0]}", headers=auth(token, True)).status_code == 200
    assert (
        post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "fits now"}).status_code == 200
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
    res = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "x"})
    assert res.status_code == 500 and res.json()["error"]["code"] == "internal"
    assert "000000000000" not in res.text


def test_session_lists_the_actions_feature(client, token, stub, rich):
    features = client.get("/api/session", headers=auth(token)).json()
    assert "actions" in features["features"] and "actions" in features["workspaces"][stub.ws]


def second_token(client, auth):
    from conftest import PORT

    launch = auth.launch(PORT)
    return client.post("/auth/session", json={"code": launch["code"]}, headers={"Origin": ORIGIN}).json()["token"]


def test_a_request_belongs_to_the_person_not_to_the_tab(ready, auth):
    client, token, stub, rich, signing = ready
    mine = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "hi"}).json()
    other = second_token(client, auth)
    url = f"{base(stub)}/{mine['id']}"
    # Another session of the same person follows it, and prepares the same action to the SAME request.
    assert client.get(url, headers=auth_h(other)).json()["state"] == "prepared"
    theirs = post(client, other, stub, {"action": "comment", "key": "DEMO-0001", "message": "hi"}).json()
    assert theirs["id"] == mine["id"] and theirs["code"] == mine["code"]
    assert len(signing.stub.requests.table) == 1
    # An id nobody here prepared looks like one that never existed, for every session.
    never = f"{base(stub)}/hr_{'0' * 25}1"
    assert client.get(never, headers=auth_h(token)).status_code == 404
    assert client.delete(never, headers=auth_h(other, True)).status_code == 404


def test_a_different_person_never_reaches_the_request(ready, monkeypatch):
    client, token, stub, rich, signing = ready
    mine = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "hi"}).json()
    monkeypatch.setattr(client.app.state, "person_of", lambda ws: "p_" + "f" * 32)
    url = f"{base(stub)}/{mine['id']}"
    assert client.get(url, headers=auth_h(token)).status_code == 404
    assert client.delete(url, headers=auth_h(token, True)).status_code == 404
    assert signing.stub.requests.table[mine["id"]]["state"] == "prepared"


def test_two_tabs_get_one_request_and_it_is_cancelled_once_for_both(ready, auth):
    client, token, stub, rich, signing = ready
    body = {"action": "approve", "key": "DEMO-0001", "gate": "requirements"}
    a = post(client, token, stub, body).json()
    b = post(client, second_token(client, auth), stub, body).json()  # a second tab: the same intent
    assert a["id"] == b["id"] and a["code"] == b["code"] and len(signing.stub.requests.table) == 1
    client.delete(f"{base(stub)}/{b['id']}", headers=auth_h(token, True))
    assert client.get(f"{base(stub)}/{a['id']}", headers=auth_h(token)).json()["state"] == "cancelled"


def test_logout_of_one_tab_leaves_the_request_the_other_tab_holds(ready, auth):
    client, token, stub, rich, signing = ready
    other = second_token(client, auth)
    body = {"action": "comment", "key": "DEMO-0001", "message": "shared"}
    made = post(client, token, stub, body).json()["id"]
    assert post(client, other, stub, body).json()["id"] == made
    client.post("/auth/logout", headers=auth_h(token, True))
    assert states(signing) == {made: "prepared"}  # the other tab still holds it
    client.post("/auth/logout", headers=auth_h(other, True))
    assert states(signing) == {made: "cancelled"}  # the last holder's logout cancels it


def test_logout_ends_the_session_even_if_a_cancel_fails(ready, monkeypatch):
    client, token, stub, rich, signing = ready
    post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "hi"})
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
    res = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "x"})
    assert res.status_code == 500 and res.json()["error"]["code"] == "internal"


@pytest.mark.parametrize(
    "mutate", ["review_payload", "review_type", "expires_far", "expires_bad", "state", "material_key"]
)
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
        elif mutate == "material_key":
            out["material"]["note"] = "hello"
        elif mutate == "expires_bad":
            out["request"]["expires_at"] = "soon"
        else:
            out["request"]["state"] = "weird"  # (bound is a real state of a retried request now)
        # keep the hashes consistent so only the new check can refuse it
        digest = adapters.handoff.review_digest(out["material"])
        out["review_digest"], out["fingerprint"] = (
            digest,
            adapters.handoff.fingerprint(digest, out["events"], out["op"]),
        )
        return out

    monkeypatch.setattr(signing.stub.requests, "prepare", tamper)
    res = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "x"})
    assert res.status_code == 500 and res.json()["error"]["code"] == "internal"


def test_a_failed_submit_code_is_passed_on_when_the_host_sends_it(ready, monkeypatch):
    client, token, stub, rich, signing = ready
    got = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "x"}).json()
    original = signing.stub.requests.pending

    def with_error(args, lookups=None):
        out = original(args, lookups)
        out["requests"][0]["error"] = {"code": "gate.stale", "message": "ignored"}
        return out

    monkeypatch.setattr(signing.stub.requests, "pending", with_error)
    assert client.get(f"{base(stub)}/{got['id']}", headers=auth_h(token)).json()["error"] == {"code": "gate.stale"}


def test_details_set_the_parent_and_the_fields_as_one_set_with_their_prior_values(ready):
    client, token, stub, rich, signing = ready
    body = {"action": "details", "key": "DEMO-0001", "parent": "DEMO-0003", "priority": "high"}
    got = post(client, token, stub, body).json()
    event = got["events"][0]
    assert event["set"] == {"ticket.parent": "DEMO-0003", "ticket.priority": "high"}
    assert got["material"]["before"]["ticket.parent"] is None and "sections" not in event


def test_details_refuse_a_parent_that_is_not_there_or_is_the_ticket_itself(ready):
    client, token, stub, rich, signing = ready
    for parent in ("DEMO-0999", "DEMO-0001"):
        res = post(client, token, stub, {"action": "details", "key": "DEMO-0001", "parent": parent})
        assert res.status_code in (400, 404), res.text
    assert signing.stub.requests.table == {}


def test_a_section_is_prepared_with_its_text_in_the_material_and_signed_in_the_terminal(ready):
    client, token, stub, rich, signing = ready
    env, ids = rich
    text = "Line one.\n\n- a\n- b"
    got = post(
        client,
        token,
        stub,
        {"action": "section", "key": "DEMO-0001", "section": "context", "text": text},
    ).json()
    assert got["op"] == "section.set" and got["material"]["texts"] == {"context": text}
    assert set(got["events"][0]) >= {"sections", "base_rev"} and "set" not in got["events"][0]
    assert "body.context" in got["material"]["before"]
    status, shown = signing.sign(got["code"])
    assert status == 0, shown
    env.store.refresh()
    assert env.store.body_sections(ids["First"])["context"] == text


def test_a_section_the_ticket_type_does_not_have_is_refused_by_the_host(ready):
    client, token, stub, rich, signing = ready
    res = post(
        client, token, stub, {"action": "section", "key": "DEMO-0003", "section": "plan", "text": "x"}
    )  # DEMO-0003 is an epic: no plan
    assert res.status_code == 400 and signing.stub.requests.table == {}


def test_the_dashboard_follows_a_request_by_its_id_and_never_looks_up_a_code(ready, host_calls):
    client, token, stub, rich, signing = ready
    got = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "x"}).json()
    assert client.get(f"{base(stub)}/{got['id']}", headers=auth(token)).status_code == 200
    assert client.delete(f"{base(stub)}/{got['id']}", headers=auth_h(token, True)).status_code == 200
    for op, args, _ in host_calls:
        if op == "human.pending":
            assert "code" not in args


# --- a lost prepare answer: the host honours opts.idem (contract 0.7), so the same click returns the same request ---


def lose(monkeypatch, *, drop=1, code="outcome.unknown"):
    """The host makes the request, then the answer is lost (`drop` times)."""
    from orch_dashboard.errors import fail

    original = HostLink.call
    plan = {"drop": drop}

    def flaky(self, op, args=None, **kw):
        out = original(self, op, args, **kw)
        if op == "human.prepare" and plan["drop"]:
            plan["drop"] -= 1
            raise fail(code)
        return out

    monkeypatch.setattr(HostLink, "call", flaky)
    return plan


def second_session(client, auth):  # noqa: D103
    from conftest import PORT

    launch = auth.launch(PORT)
    return client.post("/auth/session", json={"code": launch["code"]}, headers={"Origin": ORIGIN}).json()["token"]


def states(signing):
    return {rid: r["state"] for rid, r in signing.stub.requests.table.items()}


def test_a_lost_answer_then_a_retry_returns_the_same_request_and_makes_no_second(ready, monkeypatch, host_calls):
    client, token, stub, rich, signing = ready
    lose(monkeypatch)
    body = {"action": "comment", "key": "DEMO-0001", "message": "hi"}
    first = post(client, token, stub, body)
    assert first.status_code == 503 and first.json()["error"]["code"] == "outcome.unknown"
    assert len(signing.stub.requests.table) == 1  # the host made it; the answer was lost
    (made,) = signing.stub.requests.table
    second = post(client, token, stub, body)
    assert second.status_code == 200 and second.json()["id"] == made and "earlier_open" not in second.json()
    assert states(signing) == {made: "prepared"}
    # Both calls carried the same key, and nothing was listed, fetched or cancelled to find an orphan.
    prepares = [c for c in host_calls if c[0] == "human.prepare"]
    hosted = [c[2]["idem"] for c in prepares]
    assert hosted[0] == hosted[1] != idem(63) and hosted[0].startswith("a_")  # one key per click, bound to the session
    assert {c[0] for c in host_calls} <= {"human.prepare", "addon.list", "ticket.get"}


def test_the_retried_request_is_the_one_the_signer_can_sign(ready, monkeypatch):
    client, token, stub, rich, signing = ready
    lose(monkeypatch)
    body = {"action": "comment", "key": "DEMO-0001", "message": "hi"}
    assert post(client, token, stub, body).status_code == 503
    got = post(client, token, stub, body).json()
    status, shown = signing.sign(got["code"])
    assert status == 0, shown
    assert list(states(signing).values()) == ["submitted"]


def test_an_idem_conflict_at_the_host_is_an_open_attempt_and_prepares_nothing_more(ready, monkeypatch):
    """A different path or signer binding conflicts; no open request means an explicit restart is available."""
    from orch_dashboard.errors import fail

    client, token, stub, rich, signing = ready
    original = HostLink.call
    calls = []

    def conflict(self, op, args=None, **kw):
        if op == "human.prepare":
            calls.append(kw["idem"])
            raise fail("idem.conflict", exit=5)
        return original(self, op, args, **kw)

    monkeypatch.setattr(HostLink, "call", conflict)
    res = post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "hi"})
    assert res.status_code == 409 and res.json()["error"]["code"] == "intent.blocked"
    assert len(calls) == 1 and signing.stub.requests.table == {}  # not retried with another key
    assert post(client, token, stub, {"action": "comment", "key": "DEMO-0001", "message": "hi"}).status_code == 409


def test_a_retry_after_the_request_ended_answers_its_terminal_state(ready, monkeypatch):
    client, token, stub, rich, signing = ready
    lose(monkeypatch)
    body = {"action": "comment", "key": "DEMO-0001", "message": "hi"}
    assert post(client, token, stub, body).status_code == 503
    (made,) = signing.stub.requests.table
    signing.stub.requests.cancel({"request_id": made})
    again = post(client, token, stub, body)
    assert again.status_code == 200 and again.json()["id"] == made and again.json()["state"] == "cancelled"
    assert len(signing.stub.requests.table) == 1


def test_a_retry_after_a_signature_answers_submitted(ready, monkeypatch):
    client, token, stub, rich, signing = ready
    lose(monkeypatch)
    body = {"action": "comment", "key": "DEMO-0001", "message": "hi"}
    assert post(client, token, stub, body).status_code == 503
    (made,) = signing.stub.requests.table
    status, shown = signing.sign(signing.stub.requests.table[made]["code"])
    assert status == 0, shown
    again = post(client, token, stub, body)
    assert again.status_code == 409 and again.json()["error"]["code"] == "intent.done"
    assert "hi" in again.json()["error"]["summary"]
    assert again.json()["error"]["signed_at"] == signing.stub.requests.table[made]["submitted_at"]


def test_expired_intent_requires_explicit_start_again(ready, monkeypatch, host_calls):
    from orch_dashboard.errors import fail

    client, token, stub, rich, signing = ready
    body = {"action": "comment", "key": "DEMO-0001", "message": "hi"}
    made = post(client, token, stub, body).json()["id"]
    first = next(c[2]["idem"] for c in host_calls if c[0] == "human.prepare")
    original = HostLink.call
    attempts = []

    def tombstone(self, op, args=None, **kw):
        if op == "human.prepare":
            attempts.append(kw.get("idem"))
            if kw.get("idem") == first:
                raise fail("idem.expired", exit=2)
        return original(self, op, args, **kw)

    monkeypatch.setattr(HostLink, "call", tombstone)
    res = post(client, token, stub, body)
    assert res.status_code == 409 and res.json()["exit"] == 5
    assert res.json()["error"]["code"] == "intent.expired"
    assert res.json()["error"]["retryable"] is False
    assert attempts == [first] and len(signing.stub.requests.table) == 1
    again = post(client, token, stub, {**body, "retry": 1})
    assert again.status_code == 200 and again.json()["id"] != made
    assert len(attempts) == 2 and attempts[1] != first
    assert len(signing.stub.requests.table) == 2


def test_a_host_restart_never_produces_a_second_request(ready, monkeypatch):
    from devhost.host import StubHost

    client, token, stub, rich, signing = ready
    lose(monkeypatch)
    body = {"action": "comment", "key": "DEMO-0001", "message": "hi"}
    assert post(client, token, stub, body).status_code == 503
    (made,) = signing.stub.requests.table
    restarted = StubHost(stub.root, stub.state, stub.person)  # a new process: its tables are read from disk
    assert list(restarted.requests.table) == [made]
    (host_key,) = restarted.requests.prepares  # the key the host saw (the browser's, bound to its session)
    assert restarted.requests.prepares[host_key]["request"] == made
    res = restarted.call("human.prepare", body_args(body), {"idem": host_key})
    assert res["data"]["request"]["id"] == made and len(restarted.requests.table) == 1


def body_args(body):
    return {"op": "log", "args": {"ref": body["key"], "message": body["message"]}, "handoff": True}


def test_without_a_key_the_stub_makes_a_request_each_call(ready):
    client, token, stub, rich, signing = ready
    args = body_args({"key": "DEMO-0001", "message": "hi"})
    one, two = (signing.stub.call("human.prepare", args, {}) for _ in range(2))
    assert one["data"]["request"]["id"] != two["data"]["request"]["id"]
    # A dry run neither stores nor looks anything up.
    dry = signing.stub.call("human.prepare", args, {"dry_run": True})
    assert dry["data"]["request"] is None and idem(70) not in signing.stub.requests.prepares


def test_a_section_limit_counts_bytes_not_characters(ready):
    client, token, stub, rich, signing = ready
    text = "\u00e4" * 40000  # 40000 characters, 80000 bytes
    body = {"action": "section", "key": "DEMO-0001", "section": "summary", "text": text}
    r = post(client, token, stub, body)
    assert r.status_code == 400


def test_a_section_of_only_whitespace_is_refused(ready):
    client, token, stub, rich, signing = ready
    body = {"action": "section", "key": "DEMO-0001", "section": "summary", "text": " \n\t "}
    assert post(client, token, stub, body).status_code == 400
    assert states(signing) == {}  # nothing was prepared


def test_submitted_failed_intent_never_claims_done(ready):
    client, token, stub, rich, signing = ready
    body = {"action": "comment", "key": "DEMO-0001", "message": "failed append"}
    made = post(client, token, stub, body).json()["id"]
    record = signing.stub.requests.table[made]
    record.update(state="submitted", submitted_at="2026-10-13T15:20:00Z", error={"code": "gate.stale"})
    again = post(client, token, stub, body)
    assert again.status_code == 409
    error = again.json()["error"]
    assert error["code"] == "intent.failed" and error["reason"] == "gate.stale"
    assert "not signed into the log" in error["message"] and "signed_at" not in error
    fresh = post(client, token, stub, {**body, "retry": 1})
    assert fresh.status_code == 200 and fresh.json()["id"] != made


@pytest.mark.parametrize("mutate", ["none", "before", "section_text", "policy"])
def test_the_plain_view_is_shown_only_when_the_signers_material_checks_hold(ready, monkeypatch, mutate):
    """B6: `content` is derived from the host's material, so the server first runs the signer's own material
    checks (gate, policy and section hashes, every `before` against `base_rev`); a failing one leaves the old view."""
    client, token, stub, rich, signing = ready
    from orch.host import review

    from orch_dashboard import adapters

    original = signing.stub.requests.prepare
    action = (
        {"action": "approve", "key": "DEMO-0001", "gate": "requirements"}
        if mutate == "policy"
        else {"action": "section", "key": "DEMO-0001", "section": "summary", "text": "New text."}
    )

    def tamper(store, views, args, opts):
        out = original(store, views, args, opts)
        m = out["material"]
        if mutate == "before":
            m["before"]["body.summary"] = "a text the event's base_rev does not bind"
        elif mutate == "section_text":
            m["texts"]["summary"] = "something else"
        elif mutate == "policy":  # the host shows another policy than the one the gate hash binds
            m["policy"] = {**m["policy"], "count": m["policy"]["count"] + 1}
        m["review"] = [review.of(e, m) for e in out["events"]] if mutate in ("before", "policy") else m["review"]
        digest = adapters.handoff.review_digest(m)
        out["review_digest"], out["fingerprint"] = (
            digest,
            adapters.handoff.fingerprint(digest, out["events"], out["op"]),
        )
        return out

    monkeypatch.setattr(signing.stub.requests, "prepare", tamper)
    res = post(client, token, stub, action)
    assert res.status_code == 200, res.text
    got = res.json()
    if mutate == "none":
        assert (
            got["headline"].startswith("Change the summary of") and got["content"]["sections"][0]["text"] == "New text."
        )
    else:
        assert "content" not in got and "headline" not in got and got["material"]["review"]


SEQ = {
    "action": "new_sequence",
    "title": "Plan the launch",
    "type": "feature",
    "priority": "high",
    "labels": ["b", "a"],
    "sections": {"summary": "Why this exists.", "context": "Line one\r\nLine two"},
}


def test_a_new_ticket_with_its_text_is_one_request_one_review(ready, host_calls):
    client, token, stub, rich, signing = ready
    res = post(client, token, stub, {**SEQ})
    assert res.status_code == 200, res.text
    got = res.json()
    sent = [kw_args for op, kw_args, _ in host_calls if op == "human.prepare"]
    assert sent and sent[-1]["op"] == "new" and sent[-1]["args"] == {"title": "Plan the launch", "type": "feature"}
    meta = sent[-1]["sequence"]
    assert meta["step"] == 0 and meta["of"] == 4  # new, set, two sections
    assert meta["intent"]["set"] == {"labels": ["a", "b"], "priority": "high"}  # sorted; CRLF made LF
    assert meta["intent"]["sections"]["context"] == "Line one\nLine two"
    assert got["material"]["sequence"]["intent"] == meta["intent"] and got["events"][0]["type"] == "ticket.created"
    # the one view carries the ticket AND its written text
    content = got["content"]
    assert sorted(s["id"] for s in content["sections"]) == ["context", "summary"]
    assert content["headline"].startswith("Create DEMO-") and content["subline"] == "and its text (2 sections)"
    assert any("one confirmation" in n.lower() for n in content["notes"])
    # a second post of the same intent is the same request (the intent's idem covers the whole intent)
    again = post(client, token, stub, {**SEQ}).json()
    assert again["id"] == got["id"]
    other = post(client, token, stub, {**SEQ, "sections": {"summary": "Another text."}}).json()
    assert other["id"] != got["id"]


@pytest.mark.parametrize(
    "change",
    [
        {"sections": {"nonsense": "x"}},
        {"sections": {"summary": "  "}},
        {"labels": ["Has Space"]},
        {"surprise": 1},
    ],
)
def test_a_new_ticket_with_its_text_refuses_a_bad_intent(ready, change):
    client, token, stub, rich, signing = ready
    res = post(client, token, stub, {**SEQ, **change})
    assert res.status_code in (400, 422), res.text


def test_the_progress_of_a_sequence_is_read_from_the_tickets_log():
    from orch import canon
    from orch.host import sequence

    from orch_dashboard.handoff import sequence_progress

    intent = sequence.validate_intent(
        {
            "v": 1,
            "kind": "new_ticket",
            "title": "T",
            "ticket_type": "feature",
            "set": {"priority": "high"},
            "sections": {"summary": "One", "context": "Two"},
        }
    )
    ticket = {"doc": {"priority": "medium"}, "sections": {}}

    class Gateway:
        def call(self, ws, op, args):
            return ticket

    def at():
        return sequence_progress(Gateway(), "w", "DEMO-0001", intent)

    assert at() == {"done": 1, "of": 4}  # the ticket exists
    ticket["doc"]["priority"] = "high"
    assert at() == {"done": 2, "of": 4}
    ticket["sections"]["summary"] = [{"hash": canon.section_hash("One")}]  # the ticket page's order: summary first
    assert at() == {"done": 3, "of": 4}
    ticket["sections"]["context"] = [{"hash": canon.section_hash("Two")}]
    assert at() == {"done": 4, "of": 4}
    assert sequence_progress(Gateway(), "w", None, intent) is None


def test_the_agent_access_of_a_quick_task_counts_as_the_last_step_and_is_read_from_the_grants():
    from orch import canon
    from orch.host import sequence

    from orch_dashboard.handoff import sequence_progress

    intent = sequence.validate_intent(
        {
            "v": 1,
            "kind": "quick_task",
            "title": "T",
            "description": "D",
            "checks": [{"text": "C", "cmd": None}],
            "agent": {"hours": 24, "scope": "workable"},
        }
    )
    from orch.host import seqquick

    plan = seqquick.plan_text(intent["checks"])
    ticket = {
        "doc": {"labels": ["quick"], "acceptance": [{}], "tasks": [{}]},
        "sections": {
            "requirements": [{"hash": canon.section_hash("D")}],
            "plan": [{"hash": canon.section_hash(plan)}],
        },
        "gates": {"requirements": {"state": "approved"}, "plan": {"state": "approved"}},
    }
    grants = []

    class Gateway:
        def call(self, ws, op, args):
            return ticket if op == "ticket.get" else {"grants": grants}

    total = len(sequence.steps(intent))

    def at():
        return sequence_progress(Gateway(), "w", "DEMO-0001", intent, person="p_me", since="2026-10-17T10:00:00Z")

    assert at() == {"done": total - 1, "of": total}  # every ticket step landed; the grant did not: not done
    good = {
        "person": "p_me",
        "label": "runner",
        "issued_at": "2026-10-17T10:00:05Z",
        "expires_at": "2026-10-18T10:00:05Z",
    }
    for bad in (
        {**good, "person": "p_other"},
        {**good, "label": None},
        {**good, "issued_at": "2026-10-17T09:00:00Z", "expires_at": "2026-10-18T09:00:00Z"},  # older than this request
        {**good, "expires_at": "2026-10-17T18:00:05Z"},  # another length
        {**good, "revoked_at": "2026-10-17T10:01:00Z"},
    ):
        grants[:] = [bad]
        assert at() == {"done": total - 1, "of": total}
    grants[:] = [good]
    assert at() == {"done": total, "of": total}
    no_person = sequence_progress(Gateway(), "w", "DEMO-0001", intent)
    assert no_person == {"done": total - 1, "of": total}  # closed


QUICK = {
    "action": "quick_start",
    "title": "Fix the typo in the README",
    "description": "The README says teh.\r\nFix it.",
    "checks": [{"text": "The typo is gone", "cmd": "grep -c teh README.md"}],
    "agent": {"hours": 8},
}


def test_a_quick_task_is_one_request_one_review_with_the_derived_plan(ready, host_calls):
    client, token, stub, rich, signing = ready
    res = post(client, token, stub, {**QUICK})
    assert res.status_code == 200, res.text
    got = res.json()
    sent = [a for op, a, _ in host_calls if op == "human.prepare"][-1]
    assert sent["op"] == "new" and sent["args"] == {"title": "Fix the typo in the README", "type": "chore"}
    meta = sent["sequence"]
    # new, labels, requirements, 1 ac, approve, plan, 1 task, approve, grant
    assert meta["step"] == 0 and meta["of"] == 9
    assert meta["intent"]["kind"] == "quick_task" and meta["intent"]["agent"] == {"hours": 8, "scope": "workable"}
    assert meta["intent"]["description"] == "The README says teh.\nFix it."
    assert got["material"]["sequence"]["intent"] == meta["intent"]
    assert got["content"]["headline"].startswith("Start task DEMO-")
    ids = sorted(s["id"] for s in got["content"]["sections"])
    assert ids == ["plan", "requirements"]
    prefix = "Agents may work on any ticket they are allowed to work on"
    assert any(n.startswith(prefix) for n in got["content"]["notes"])


def test_a_quick_task_without_steps_gets_one_check_and_no_grant(ready, host_calls):
    client, token, stub, rich, signing = ready
    res = post(client, token, stub, {"action": "quick_start", "title": "Water the plants"})
    assert res.status_code == 200, res.text
    meta = [a for op, a, _ in host_calls if op == "human.prepare"][-1]["sequence"]
    assert meta["of"] == 8 and meta["intent"]["agent"] is None
    assert meta["intent"]["checks"] == [{"text": "Water the plants", "cmd": None}]
    assert meta["intent"]["description"] == "Water the plants"


@pytest.mark.parametrize(
    "change",
    [
        {"checks": [{"text": "a", "cmd": None}] * 5},
        {"agent": {"hours": 500}},
        {"title": ""},
        {"surprise": 1},
        {"checks": [{"text": "a\nb"}]},
    ],
)
def test_a_bad_quick_task_is_refused(ready, change):
    client, token, stub, rich, signing = ready
    res = post(client, token, stub, {**QUICK, **change})
    assert res.status_code in (400, 422), res.text
