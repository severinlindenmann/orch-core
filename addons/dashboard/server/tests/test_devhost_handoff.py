"""The dev stub's `human.*` (host contract v1.4 section 0.8.1) and the stub signer, against the real P1 store."""

import hashlib
import json

import pytest
from conftest import DevBackend  # noqa: F401
from devhost import review
from devhost import sign as stub_signer

from orch_dashboard.errors import HostError

PREPARE = "human.prepare"


def prepare(link, op, args, **extra):
    return link.call(PREPARE, {"op": op, "args": args, "handoff": True, **extra}, write=True, idem="a_" + "0" * 26)


def pending(link, **args):
    return link.call("human.pending", args)["requests"]


def test_prepare_returns_the_v14_shape_and_recomputable_hashes(signing, rich):
    link = signing.link()
    got = prepare(link, "approve", {"ref": "DEMO-0001", "gate": "requirements"})
    request = got["request"]
    assert request["id"].startswith("hr_") and len(request["id"]) == 29
    assert len(request["code"]) == 9 and request["code"][4] == "-" and request["state"] == "prepared"
    assert got["op"] == "approve" and got["workspace_id"] == signing.ws and len(got["events"]) == 1
    event = got["events"][0]
    assert event["type"] == "gate.approved" and event["actor"]["device"] is None and event["auth"] is None
    assert got["material"]["kind"] == "gate" and got["material"]["ticket"]["key"] == "DEMO-0001"
    # The signer recomputes everything from material and events.
    assert got["material"]["review"] == review.reviews_of(got["events"], got["material"])
    digest = "sha256:" + hashlib.sha256(review.cj(got["material"])).hexdigest()
    assert got["review_digest"] == digest and got["fingerprint"] == review.fingerprint(digest, got["events"])
    assert got["origin"]["client"] and set(got["origin"]) == {"uid", "pid", "client", "created_at"}
    # `human.get` without a signer returns the same bytes in any state, and binds nothing.
    again = link.call("human.get", {"request_id": request["id"]})
    assert again["events"] == got["events"] and again["request"]["state"] == "prepared"


def test_fingerprint_vectors_are_stable():
    material = {"kind": "payload", "ticket": {"key": "DEMO-0001", "uid": "01J9ZP0000000000000000000A"}, "review": []}
    event = {
        "v": 2,
        "id": "01J9ZP0000000000000000000B",
        "type": "log.added",
        "actor": {"kind": "person", "id": "p_a", "device": "d_" + "a" * 16},
        "auth": "passphrase",
        "hash_v": 1,
        "roster_v": 1,
        "based_on": None,
        "text": "hello",
    }
    digest = review.digest_of(material)
    # The device and auth do not change the fingerprint; the text does.
    other = {**event, "actor": {"kind": "person", "id": "p_a", "device": None}, "auth": None}
    assert review.fingerprint(digest, [event]) == review.fingerprint(digest, [other])
    assert review.fingerprint(digest, [event]) != review.fingerprint(digest, [{**event, "text": "hallo"}])
    body = {"review_digest": digest, "events": [{k: v for k, v in other.items() if k not in ("auth",)}]}
    body["events"][0]["actor"] = {"kind": "person", "id": "p_a"}
    assert review.fingerprint(digest, [event]) == hashlib.sha256(review.cj(body)).hexdigest()[:12]


def test_signer_signs_and_the_store_appends_through_p1(signing, rich, stub):
    env, ids = rich
    link = signing.link()
    got = prepare(link, "approve", {"ref": "DEMO-0001", "gate": "requirements"})
    status, shown = signing.sign(got["request"]["code"])
    assert status == 0, shown
    text, word = shown[0]
    assert word == "DEMO-0001" and got["fingerprint"] in text and "only sign a code you created yourself" in text
    assert signing.backend.signed and "approve" in signing.backend.signed[0]
    env.store.refresh()
    assert env.store.state.tickets[ids["First"]].gates["requirements"].decisions
    row = pending(link, request_id=got["request"]["id"])[0]
    assert row["state"] == "submitted" and row["event_ids"] == [got["events"][0]["id"]] and row["seq"] > 1
    assert row["code"] is None


def test_replay_of_a_submit_is_refused_or_answered_from_the_tombstone(signing, rich):
    link = signing.link()
    got = prepare(link, "log", {"ref": "DEMO-0001", "message": "a note"})
    rid = got["request"]["id"]
    bound = link.call(
        "human.get", {"request_id": rid, "signer": {"device": signing.p1.owner.device, "auth": "passphrase"}}
    )
    assert bound["request"]["state"] == "bound" and bound["events"][0]["actor"]["device"] == signing.p1.owner.device
    from orch import canon, crypto

    event = dict(bound["events"][0])
    event["sig"] = crypto.b64u(signing.p1.owner.dk_sign(canon.person_signing_bytes(signing.ws, got["log"], event)))
    stub = signing.stub
    first = stub.call("human.submit", {"request_id": rid, "events": [event]}, {"idem": "a_" + "1" * 26})
    assert first["data"] == {"op": "log"} and first["seq"] > 1 and first["duplicate"] is False
    # The same signed events under a fresh idem: the stored answer, flagged duplicate. No second append.
    again = stub.call("human.submit", {"request_id": rid, "events": [event]}, {"idem": "a_" + "2" * 26})
    assert again["duplicate"] is True and again["seq"] == first["seq"]
    # The same idem with other events: idem.conflict. Other events under a fresh idem: human.request_state.
    other = {**event, "text": "another note"}
    with pytest.raises(HostError, match="idem.conflict"):
        link.call("human.submit", {"request_id": rid, "events": [other]}, idem="a_" + "1" * 26)
    with pytest.raises(HostError, match="human.request_state"):
        link.call("human.submit", {"request_id": rid, "events": [other]}, idem="a_" + "3" * 26)
    events = signing.p1.store.events(got["log"])
    assert sum(1 for e in events if e["type"] == "log.added" and e.get("text") == "a note") == 1


def test_wrong_request_id_and_event_counts(signing, rich):
    link = signing.link()
    with pytest.raises(HostError, match="not_found"):
        link.call("human.submit", {"request_id": "hr_" + "0" * 26, "events": [{}]}, idem="a_" + "4" * 26)
    with pytest.raises(HostError, match="not_found"):
        link.call("human.get", {"request_id": "nope"})
    got = prepare(link, "log", {"ref": "DEMO-0001", "message": "x"})
    for events in ([], [{}] * 17):  # the host refuses them too (the link refuses them first, so ask the stub itself)
        with pytest.raises(HostError, match="invalid.input"):
            signing.stub.call(
                "human.submit", {"request_id": got["request"]["id"], "events": events}, {"idem": "a_" + "5" * 26}
            )
    with pytest.raises(HostError, match="idem.required"):
        signing.stub.call("human.submit", {"request_id": got["request"]["id"], "events": [{}]}, {})


def test_submit_needs_a_bound_request_and_the_exact_events(signing, rich):
    link = signing.link()
    got = prepare(link, "log", {"ref": "DEMO-0001", "message": "x"})
    rid = got["request"]["id"]
    signed = {**got["events"][0], "sig": "AAAA"}
    with pytest.raises(HostError, match="human.request_state"):  # unbound
        link.call("human.submit", {"request_id": rid, "events": [signed]}, idem="a_" + "6" * 26)
    link.call("human.get", {"request_id": rid, "signer": {"device": signing.p1.owner.device, "auth": "passphrase"}})
    with pytest.raises(HostError, match="human.request_mismatch"):  # the unbound event is not the bound one
        link.call("human.submit", {"request_id": rid, "events": [signed]}, idem="a_" + "7" * 26)


def test_expiry_cancel_and_lookup_by_code(signing, rich, stub):
    now = [1_000_000.0]
    stub.requests.clock = lambda: now[0]
    link = signing.link()
    got = prepare(link, "log", {"ref": "DEMO-0001", "message": "x"})
    code, rid = got["request"]["code"], got["request"]["id"]
    assert pending(link, code=code)[0]["id"] == rid
    assert link.call("human.cancel", {"request_id": rid}) == {"state": "cancelled"}
    assert link.call("human.cancel", {"request_id": rid}) == {"state": "cancelled"}
    assert pending(link, request_id=rid)[0]["state"] == "cancelled"
    device = {"device": signing.p1.owner.device, "auth": "passphrase"}
    with pytest.raises(HostError, match="human.request_state"):
        link.call("human.get", {"request_id": rid, "signer": device})
    second = prepare(link, "log", {"ref": "DEMO-0001", "message": "y"})
    now[0] += 601
    assert pending(link, request_id=second["request"]["id"])[0]["state"] == "expired"
    with pytest.raises(HostError, match="human.request_expired"):
        link.call("human.get", {"request_id": second["request"]["id"], "signer": device})
    assert signing.sign(second["request"]["code"])[0] == 1  # the signer finds no open request


def test_stale_between_prepare_and_submit(signing, rich):
    env, ids = rich
    link = signing.link()
    got = prepare(link, "approve", {"ref": "DEMO-0001", "gate": "requirements"})
    env.tick()
    env.update(ids["First"], {}, {"requirements": "changed after the prepare"})
    status, shown = signing.sign(got["request"]["code"])
    assert status == 1 and "gate.stale" in shown[-1]
    assert not [d for d in env.store.state.tickets[ids["First"]].gates["requirements"].decisions]


def test_dry_run_stores_nothing_and_move_is_not_served(signing, rich):
    link = signing.link()
    got = link.call(PREPARE, {"op": "log", "args": {"ref": "DEMO-0001", "message": "x"}, "handoff": True}, dry_run=True)
    assert got["request"] is None and got["fingerprint"]
    assert pending(link) == []
    with pytest.raises(HostError, match="op.unsupported"):
        prepare(link, "move", {"ref": "DEMO-0001", "to": "backlog"})
    with pytest.raises(HostError, match="op.not_allowed"):
        prepare(link, "grant", {})
    with pytest.raises(HostError, match="invalid.input"):
        link.call(
            PREPARE, {"op": "log", "args": {"ref": "DEMO-0001", "message": "x"}}, write=True, idem="a_" + "0" * 26
        )


def test_every_person_operation_prepares_and_signs(signing, rich):
    env, ids = rich
    link = signing.link()
    cases = [
        ("log", {"ref": "DEMO-0001", "message": "a comment"}),
        ("set", {"ref": "DEMO-0001", "pairs": ["labels=api,ui,new-one"]}),
        ("request_changes", {"ref": "DEMO-0001", "gate": "requirements", "message": "more detail"}),
        ("answer", {"ref": "DEMO-0001", "question": "Q2", "option": "csv"}),
        ("close", {"ref": "DEMO-0002", "resolution": "wont_do", "message": "no longer needed"}),
        ("reopen", {"ref": "DEMO-0002"}),
        ("new", {"title": "Made in the dashboard", "type": "bug"}),
    ]
    for op, args in cases:
        got = prepare(link, op, args)
        status, shown = signing.sign(got["request"]["code"])
        assert status == 0, (op, shown)
        env.tick()
    env.store.refresh()
    env.store.load_all()
    state = env.store.state
    assert "new-one" in state.tickets[ids["First"]].fields["labels"]
    assert any(t.title == "Made in the dashboard" for t in state.tickets.values())
    assert state.tickets[ids["Second"]].status == "open"


def test_extra_fields_of_new_are_refused_by_the_stub(signing, rich):
    link = signing.link()
    with pytest.raises(HostError, match="invalid.input"):
        prepare(link, "new", {"title": "T", "priority": "high"})


def test_typed_arguments_are_checked(signing, rich):
    link = signing.link()
    for op, args in [
        ("approve", {"ref": "../x", "gate": "plan"}),
        ("approve", {"ref": "DEMO-0001", "gate": "verify"}),
        ("approve", {"ref": "DEMO-0001", "gate": "plan", "extra": 1}),
        ("log", {"ref": "DEMO-0001", "message": ""}),
        ("set", {"ref": "DEMO-0001", "pairs": ["owner=p_x"]}),
        ("answer", {"ref": "DEMO-0001", "question": "x"}),
    ]:
        with pytest.raises(HostError, match="invalid.input"):
            prepare(link, op, args)
    with pytest.raises(HostError, match="not_found"):
        prepare(link, "log", {"ref": "DEMO-0999", "message": "x"})


def test_pending_limit_and_lookup_rate(signing, rich):
    link = signing.link()
    for _ in range(20):
        prepare(link, "log", {"ref": "DEMO-0001", "message": "x"})
    with pytest.raises(HostError, match="quota.host"):
        prepare(link, "log", {"ref": "DEMO-0001", "message": "x"})
    for _ in range(5):
        assert pending(link, code="0000-0000") == []
    with pytest.raises(HostError, match="rate.limited"):
        pending(link, code="0000-0000")


def test_the_signer_refuses_what_it_cannot_recompute(signing, rich):
    link = signing.link()
    got = prepare(link, "approve", {"ref": "DEMO-0001", "gate": "requirements"})
    device = signing.p1.owner.device
    bound = link.call(
        "human.get", {"request_id": got["request"]["id"], "signer": {"device": device, "auth": "passphrase"}}
    )
    args = {"workspace_id": signing.ws, "person": signing.stub.person, "device": device, "auth": "passphrase"}
    stub_signer.check(bound, **args)
    import copy

    def broken(mutate):
        copy_ = copy.deepcopy(bound)
        mutate(copy_)
        with pytest.raises(stub_signer.Refused):
            stub_signer.check(copy_, **args)

    broken(lambda p: p.__setitem__("fingerprint", "0" * 12))
    broken(lambda p: p.__setitem__("review_digest", "sha256:" + "0" * 64))
    broken(lambda p: p["material"]["review"][0]["payload"].__setitem__("gate", "plan"))
    broken(lambda p: p["events"][0].__setitem__("hash", "sha256:" + "0" * 64))
    broken(lambda p: p["events"][0]["actor"].__setitem__("id", "p_other"))
    broken(lambda p: p["events"][0].__setitem__("type", "ticket.closed"))
    broken(lambda p: p["material"]["input"].__setitem__("fields", {"x": 1}))
    broken(lambda p: p.__setitem__("workspace_id", "0" * 32))
    broken(lambda p: p["events"][0].__setitem__("sig", "x"))


def test_unconfirmed_signs_nothing(signing, rich):
    link = signing.link()
    got = prepare(link, "log", {"ref": "DEMO-0001", "message": "x"})
    status, shown = signing.sign(got["request"]["code"], confirm=lambda text, word: False)
    assert status == 1 and not signing.backend.signed
    assert pending(link, request_id=got["request"]["id"])[0]["state"] == "bound"


def test_signer_output_is_dev_only_and_escapes_ticket_text(signing, rich):
    link = signing.link()
    got = prepare(link, "log", {"ref": "DEMO-0001", "message": "line one\nline two"})
    status, shown = signing.sign(got["request"]["code"])
    text = shown[0][0]
    assert status == 0 and "| line one" in text and "| line two" in text and "[from your own registry]" in text
    json.dumps(got)


def test_the_signer_checks_before_against_base_rev_and_needs_orch_dev(signing, rich, monkeypatch):
    import copy

    link = signing.link()
    got = prepare(link, "set", {"ref": "DEMO-0001", "pairs": ["labels=zz"]})
    device = signing.p1.owner.device
    signer = {"device": device, "auth": "passphrase"}
    bound = link.call("human.get", {"request_id": got["request"]["id"], "signer": signer})
    args = {"workspace_id": signing.ws, "person": signing.stub.person, "device": device, "auth": "passphrase"}
    stub_signer.check(bound, **args)
    wrong = copy.deepcopy(bound)
    wrong["material"]["before"]["ticket.labels"] = ["not", "the", "labels"]
    wrong["material"]["review"] = review.reviews_of(wrong["events"], wrong["material"])
    wrong["review_digest"] = review.digest_of(wrong["material"])
    wrong["fingerprint"] = review.fingerprint(wrong["review_digest"], wrong["events"])
    with pytest.raises(stub_signer.Refused, match="base_rev"):
        stub_signer.check(wrong, **args)
    monkeypatch.delenv("ORCH_DEV", raising=False)
    with pytest.raises(RuntimeError, match="ORCH_DEV"):
        stub_signer.main("7F3K-Q2MA")
