"""B10 Part B (phase 1): approve requirements and plan on the phone. The hint ops (column P), ``phone.requests`` and
``gate.input`` (column R), the ``approve`` / ``changes`` relay ops through the model, the host's refusals (the gate
kind, a stale generation, a revoked device at append), the push trigger and its payload bytes.

The hint carries no authority: every decision below is an ordinary device-signed event judged by the model."""

# ruff: noqa: F811 - pytest fixtures are imported and then used as arguments
from __future__ import annotations

import pytest

from orch import canon
from orch.host import phonehints
from orch.relay.framing import Refusal
from tests.host.human_helpers import (  # noqa: F401 - fixtures  # noqa: F401 - fixtures
    Human,
    err,
    human,
    lab,
    ok,
    served,
)
from tests.relay.test_relay_entry import _code, _entry, _event, _ident, _on_worker

UID_RE = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"


def _ready(human: Human, key: str, *, text: str = "") -> None:
    for sid in ("context", "requirements", "out_of_scope"):
        human.run("section.set", {"ref": key, "section": sid, "message": f"the {sid}{text}"})
    human.run("ac.add", {"ref": key, "text": "it works"})


def _gate(served, entry, owner, ref, gate="requirements") -> dict:
    """``gate.input`` as the phone (column R) reads it."""
    return _on_worker(served, lambda: entry.read(_ident(served, owner), "gate.input", {"ref": ref, "gate": gate},
                                                 lambda: True))["data"]  # fmt: skip


def _decision(served, owner, uid, g, *, typ="gate.approved", gate="requirements", auth="secure-enclave-unlocked", **kw):
    """The event the phone builds itself from ``gate.input`` and ``ticket.get``'s head, signed by the device."""
    fields = {"gate": gate, "gate_gen": g["gen"], "hash": g["hash"], "policy_hash": g["input"]["policy_hash"]}
    if typ == "gate.changes_requested":
        fields["text"] = kw.pop("text", "Please add the rollback case.")
    return _event(served, owner, uid, typ, auth=auth, **fields, **kw)


def _send(served, entry, owner, uid, ev, *, op=None, recheck=lambda: True):
    op = op or ("approve" if ev["type"] == "gate.approved" else "changes")
    return _on_worker(served, lambda: entry.submit(_ident(served, owner), ev["id"], op, uid, [ev], recheck))


def _request(human, key, gen, gate="requirements", **kw):
    return human.c.req("phone.request", {"ref": key, "gate": gate, "gate_gen": gen}, idem=True, **kw)


# --- the hint ops (column P) ----------------------------------------------------------------------------------------


def test_a_phone_request_is_a_hint_with_no_authority_and_one_per_gate(human, served):  # noqa: F811
    uid, key = human.ticket()
    _ready(human, key)
    entry = _entry(served)
    owner = served.lab.ws.owner
    g = _gate(served, entry, owner, key)
    got = ok(_request(human, key, g["gen"]))["data"]
    assert got["state"] == "open" and got["duplicate"] is False and got["uid"] == uid and got["ref"] == key
    assert got["gate"] == "requirements" and got["gate_gen"] == g["gen"] and got["id"].startswith("ph_")
    assert got["phones"] == 0
    again = ok(_request(human, key, g["gen"]))["data"]
    assert again["id"] == got["id"] and again["duplicate"] is True
    # no event: the log has not changed by a hint
    assert [e["type"] for e in served.lab.ws.events(key)][-1] != "gate.approved"
    # the hint file holds the identity of the request and nothing else
    (h,) = served.service.phone_hints.all()
    assert set(h) == {"id", "n", "person", "uid", "gate", "gate_gen", "base_seq", "created", "expires", "cancelled"}
    assert h["expires"] - h["created"] == phonehints.TTL_SECONDS == 600


def test_a_hint_for_another_generation_replaces_the_open_one(human, served):  # noqa: F811
    uid, key = human.ticket()
    _ready(human, key)
    entry = _entry(served)
    first = ok(_request(human, key, _gate(served, entry, served.lab.ws.owner, key)["gen"]))["data"]
    human.run("section.set", {"ref": key, "section": "context", "message": "changed context"})
    g2 = _gate(served, entry, served.lab.ws.owner, key)
    second = ok(_request(human, key, g2["gen"]))["data"]
    assert second["id"] != first["id"] and second["duplicate"] is False
    states = {h["id"]: h["state"] for h in ok(human.c.req("phone.status", {}))["data"]["hints"]}
    assert states[first["id"]] == "stale" and states[second["id"]] == "open"  # only one open hint per gate


def test_the_request_checks_gate_generation_kind_and_sign_methods(human, served):  # noqa: F811
    uid, key = human.ticket()
    _ready(human, key)
    g = _gate(served, _entry(served), served.lab.ws.owner, key)
    assert err(_request(human, key, g["gen"] + 5)) == "gate.stale"
    assert err(_request(human, key, g["gen"], gate="code")) == "invalid.input"
    assert err(_request(human, key, g["gen"], gate="verify")) == "invalid.input"
    assert err(human.c.req("phone.request", {"ref": key, "gate": "plan"}, idem=True)) == "invalid.input"
    assert (
        err(human.c.req("phone.request", {"ref": "DEMO-999", "gate": "plan", "gate_gen": 0}, idem=True)) == "not_found"
    )
    assert err(human.c.req("phone.request", {"ref": key, "gate": "plan", "gate_gen": 0})) == "idem.required"
    assert served.service.phone_hints.all() == []


def test_agents_never_reach_the_hint_ops_or_the_phone_reads(human, served):  # noqa: F811
    """Column G (a grant, ORCH_GRANT) and N get ``op.not_allowed``: hints are a person's own."""
    uid, key = human.ticket()
    _ready(human, key)
    g = _gate(served, _entry(served), served.lab.ws.owner, key)
    c = served.client()
    args = {"ref": key, "gate": "requirements", "gate_gen": g["gen"]}
    assert err(c.req("phone.request", args, grant=served.grant, session="s", idem=True)) == "op.not_allowed"
    ok(_request(human, key, g["gen"]))
    for op, a in (("phone.requests", {}), ("phone.status", {})):
        assert err(c.req(op, a, grant=served.grant, session="s")) == "op.not_allowed"
    hid = served.service.phone_hints.all()[0]["id"]
    assert err(c.req("phone.cancel", {"id": hid}, grant=served.grant, session="s", idem=True)) == "op.not_allowed"
    assert [h["state"] for h in ok(human.c.req("phone.status", {}))["data"]["hints"]] == ["open"]


def test_at_most_five_open_hints_per_person(human, served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    keys = []
    for i in range(6):
        uid, key = human.ticket(f"t{i}")
        _ready(human, key)
        keys.append(key)
    for key in keys[:5]:
        ok(_request(human, key, _gate(served, entry, owner, key)["gen"]))
    assert err(_request(human, keys[5], _gate(served, entry, owner, keys[5])["gen"])) == "rate.limited"
    # a cancelled hint frees its place
    hid = served.service.phone_hints.all()[0]["id"]
    assert ok(human.c.req("phone.cancel", {"id": hid}, idem=True))["data"] == {"id": hid, "state": "cancelled"}
    ok(_request(human, keys[5], _gate(served, entry, owner, keys[5])["gen"]))


def test_cancel_and_expiry_end_a_hint_and_hide_it_from_the_phone(human, served, monkeypatch):  # noqa: F811
    uid, key = human.ticket()
    _ready(human, key)
    entry = _entry(served)
    owner = served.lab.ws.owner
    hid = ok(_request(human, key, _gate(served, entry, owner, key)["gen"]))["data"]["id"]

    def listed():
        return _on_worker(served, lambda: entry.read(_ident(served, owner), "phone.requests", {}, lambda: True))["data"]

    assert [r["id"] for r in listed()["requests"]] == [hid]
    ok(human.c.req("phone.cancel", {"id": hid}, idem=True))
    assert listed()["requests"] == []
    assert err(human.c.req("phone.cancel", {"id": "ph_" + "0" * 26}, idem=True)) == "not_found"
    # a new request, then time passes
    hid2 = ok(_request(human, key, _gate(served, entry, owner, key)["gen"]))["data"]["id"]
    assert [r["id"] for r in listed()["requests"]] == [hid2]
    real = served.service.clock
    monkeypatch.setattr(served.service, "clock", lambda: real() + 601)
    assert listed()["requests"] == []
    st = {h["id"]: h["state"] for h in ok(human.c.req("phone.status", {}))["data"]["hints"]}
    assert st == {hid: "cancelled", hid2: "expired"}


# --- phone.requests (column R) ----------------------------------------------------------------------------------------


def test_phone_requests_serve_the_viewers_own_open_hints_and_no_authority(human, served):  # noqa: F811
    uid, key = human.ticket("Make it faster")
    _ready(human, key)
    entry = _entry(served)
    owner = served.lab.ws.owner
    g = _gate(served, entry, owner, key)
    hid = ok(_request(human, key, g["gen"]))["data"]["id"]
    got = _on_worker(served, lambda: entry.read(_ident(served, owner), "phone.requests", {}, lambda: True))["data"]
    (r,) = got["requests"]
    assert set(r) == {"id", "uid", "ref", "title", "gate", "gate_gen", "created_at", "expires_at"}
    assert (r["id"], r["uid"], r["ref"], r["title"], r["gate"], r["gate_gen"]) == (
        hid, uid, key, "Make it faster", "requirements", g["gen"],
    )  # fmt: skip
    assert set(got) == {"requests", "now"}
    # another person sees none of it
    other = served.lab.add_member(scopes=("look", "decide"))
    none = _on_worker(served, lambda: entry.read(_ident(served, other), "phone.requests", {}, lambda: True))["data"]
    assert none["requests"] == []
    # a hint hidden by visibility is not served
    served.restrict(uid, [other.ref])
    hidden = _on_worker(served, lambda: entry.read(_ident(served, owner), "phone.requests", {}, lambda: True))["data"]
    assert hidden["requests"] == []


def test_phone_count_is_the_persons_current_phone_profile_devices(served):  # noqa: F811
    p = served.lab.add_member(scopes=("look", "decide"))
    store = served.service.store

    def count(person):
        with store.locked():
            store.refresh()
            return phonehints._phones(store.state, person)  # noqa: SLF001

    assert _on_worker(served, lambda: count(p.ref)) == 1
    assert _on_worker(served, lambda: count(served.lab.ws.owner.ref)) == 0  # a laptop (full scopes) is no phone


# --- gate.input: the gate hash covers the ticket, the phone can recompute it ----------------------------------------


def test_gate_input_hashes_to_the_hash_and_the_hash_covers_the_ticket_identity(human, served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    uid1, key1 = human.ticket("one")
    uid2, key2 = human.ticket("two")
    _ready(human, key1)
    _ready(human, key2)
    g1, g2 = _gate(served, entry, owner, key1), _gate(served, entry, owner, key2)
    for g, uid in ((g1, uid1), (g2, uid2)):
        assert g["ref"] == uid and g["input"]["uid"] == uid and g["input"]["gate"] == "requirements"
        assert g["input"]["workspace_id"] == served.wid
        assert canon.gate_hash(g["input"]) == g["hash"]  # what the phone recomputes
    # identical content, different ticket: a different hash (the uid is inside G), so a signature cannot be moved
    assert g1["input"]["sections"] == g2["input"]["sections"] and g1["hash"] != g2["hash"]
    assert g1["input"]["policy_hash"] == g1["policy_hash"]


def test_gate_input_refuses_a_gate_that_does_not_apply_and_a_hidden_ticket(human, served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    uid, key = human.ticket()
    assert _code(lambda: _gate(served, entry, owner, "DEMO-404")) == "not_found"
    served.restrict(uid, [served.lab.add_member().ref])
    assert _code(lambda: _gate(served, entry, owner, key)) == "not_found"


# --- approve / changes through the model ----------------------------------------------------------------------------


def test_approve_on_the_phone_lands_and_the_hint_is_done(human, served):  # noqa: F811
    uid, key = human.ticket()
    _ready(human, key)
    entry = _entry(served)
    owner = served.lab.ws.owner
    g = _gate(served, entry, owner, key)
    hid = ok(_request(human, key, g["gen"]))["data"]["id"]
    ev = _decision(served, owner, uid, g)
    res = _send(served, entry, owner, uid, ev)
    assert res["data"]["log"] == uid and res["duplicate"] is False
    logged = [e for e in served.lab.ws.events(key) if e["type"] == "gate.approved"]
    assert len(logged) == 1 and logged[0]["auth"] == "secure-enclave-unlocked" and logged[0]["gate_gen"] == g["gen"]
    (row,) = ok(human.c.req("phone.status", {"id": hid}))["data"]["hints"]
    assert row["state"] == "done" and row["outcome"]["kind"] == "approved" and row["outcome"]["event_id"] == ev["id"]
    assert row["outcome"]["seq"] == logged[0]["seq"]
    # resent: the same outcome, a duplicate
    again = _send(served, entry, owner, uid, ev)
    assert again["duplicate"] is True
    # and the phone no longer lists it
    assert (
        _on_worker(served, lambda: entry.read(_ident(served, owner), "phone.requests", {}, lambda: True))["data"][
            "requests"
        ]
        == []
    )


def test_changes_requested_on_the_phone_lands_with_the_text(human, served):  # noqa: F811
    uid, key = human.ticket()
    _ready(human, key)
    entry = _entry(served)
    owner = served.lab.ws.owner
    g = _gate(served, entry, owner, key)
    hid = ok(_request(human, key, g["gate"] and g["gen"]))["data"]["id"]
    ev = _decision(served, owner, uid, g, typ="gate.changes_requested", text="Please add the rollback case.")
    _send(served, entry, owner, uid, ev)
    (e,) = [e for e in served.lab.ws.events(key) if e["type"] == "gate.changes_requested"]
    assert e["text"] == "Please add the rollback case." and e["gate"] == "requirements"
    (row,) = ok(human.c.req("phone.status", {"id": hid}))["data"]["hints"]
    assert row["state"] == "done" and row["outcome"]["kind"] == "changes_requested"


def test_a_signature_already_in_flight_still_lands_after_cancel(human, served):  # noqa: F811
    uid, key = human.ticket()
    _ready(human, key)
    entry = _entry(served)
    owner = served.lab.ws.owner
    g = _gate(served, entry, owner, key)
    hid = ok(_request(human, key, g["gen"]))["data"]["id"]
    ev = _decision(served, owner, uid, g)
    ok(human.c.req("phone.cancel", {"id": hid}, idem=True))
    _send(served, entry, owner, uid, ev)  # the hint was never needed
    (row,) = ok(human.c.req("phone.status", {"id": hid}))["data"]["hints"]
    assert row["state"] == "done"


def test_a_decision_needs_no_hint_and_a_hint_grants_nothing(human, served):  # noqa: F811
    """No hint: the event is judged as any other. A hint for another person or gen changes nothing about the model."""
    uid, key = human.ticket()
    _ready(human, key)
    entry = _entry(served)
    owner = served.lab.ws.owner
    g = _gate(served, entry, owner, key)
    bad = _decision(served, owner, uid, {**g, "hash": "sha256:" + "0" * 64})
    ok(_request(human, key, g["gen"]))
    assert _code(lambda: _send(served, entry, owner, uid, bad)) == "gate.stale"
    assert [e for e in served.lab.ws.events(key) if e["type"] == "gate.approved"] == []


def test_phase_1_refuses_code_and_verify_decisions_before_the_store(human, served):  # noqa: F811
    uid, key = human.ticket()
    entry = _entry(served)
    owner = served.lab.ws.owner
    g = {"gen": 0, "hash": "sha256:" + "1" * 64, "input": {"policy_hash": "sha256:" + "2" * 64}}
    for typ in ("gate.approved", "gate.changes_requested"):
        ev = _decision(served, owner, uid, g, typ=typ, gate="code")
        assert _code(lambda ev=ev: _send(served, entry, owner, uid, ev)) == "op.not_allowed"
    # the lock-free check (the bridge's) is the same rule
    from orch.relay import submit as rs

    with pytest.raises(Refusal) as e:
        rs.check_phase1({"type": "gate.approved", "gate": "code"})
    assert e.value.code == "op.not_allowed"
    rs.check_phase1({"type": "gate.approved", "gate": "plan"})
    rs.check_phase1({"type": "question.answered"})


def test_a_convenience_signed_gate_decision_is_never_admitted(human, served):  # noqa: F811
    from orch.relay import submit as rs
    from orch.relay.framing import Identity

    uid, key = human.ticket()
    owner = served.lab.ws.owner
    g = {"gen": 0, "hash": "sha256:" + "1" * 64, "input": {"policy_hash": "sha256:" + "2" * 64}}
    ev = _decision(served, owner, uid, g, auth="convenience")
    ident = Identity(served.wid, owner.device, owner.ref, "sha256:" + "1" * 64, 2, "sha256:" + "4" * 64)
    with pytest.raises(Refusal) as e:
        rs.check_events("approve", {"log": uid, "events": [ev]}, ident, owner.sig_pub, idem=ev["id"])
    assert e.value.code == "op.not_allowed"


def test_the_event_fields_the_phone_builds_are_exactly_the_contract(human, served):  # noqa: F811
    from orch.relay import submit as rs
    from orch.relay.framing import Identity

    uid, key = human.ticket()
    _ready(human, key)
    owner = served.lab.ws.owner
    g = _gate(served, _entry(served), owner, key)
    ev = _decision(served, owner, uid, g)
    assert set(ev) == {
        "v", "id", "type", "actor", "auth", "hash_v", "roster_v", "based_on", "gate", "gate_gen", "hash", "policy_hash",
        "sig",
    }  # fmt: skip
    ident = Identity(served.wid, owner.device, owner.ref, "sha256:" + "1" * 64, 2, "sha256:" + "4" * 64)
    log, events = rs.check_events("approve", {"log": uid, "events": [ev]}, ident, owner.sig_pub, idem=ev["id"])
    assert log == uid and events == [ev]
    with pytest.raises(Refusal) as e:  # a source_sha (host-stamped, phase 2) is not part of the phone's event
        rs.check_events("approve", {"log": uid, "events": [{**ev, "source_sha": []}]}, ident, owner.sig_pub,
                        idem=ev["id"])  # fmt: skip
    assert e.value.code == "validation.event"
    with pytest.raises(Refusal) as e:  # the op names the type: approve with a changes event is forbidden_scope
        ch = _decision(served, owner, uid, g, typ="gate.changes_requested")
        rs.check_events("approve", {"log": uid, "events": [ch]}, ident, owner.sig_pub, idem=ch["id"])
    assert e.value.code == "forbidden_scope"


# --- stale generation, revoked device ---------------------------------------------------------------------------------


def test_an_approval_held_at_g_is_refused_after_the_gate_moved_even_if_the_hash_reverts(human, served):  # noqa: F811
    uid, key = human.ticket()
    _ready(human, key)
    entry = _entry(served)
    owner = served.lab.ws.owner
    g = _gate(served, entry, owner, key)
    held = _decision(served, owner, uid, g)  # signed at gen g, held back
    human.run("section.set", {"ref": key, "section": "context", "message": "something else"})
    moved = _gate(served, entry, owner, key)
    assert moved["gen"] > g["gen"] and moved["hash"] != g["hash"]
    human.run("section.set", {"ref": key, "section": "context", "message": "the context"})  # content back
    back = _gate(served, entry, owner, key)
    assert back["hash"] == g["hash"] and back["gen"] > g["gen"]  # the hash reverted, the generation did not
    assert _code(lambda: _send(served, entry, owner, uid, held)) == "gate.stale"
    assert [e for e in served.lab.ws.events(key) if e["type"] == "gate.approved"] == []
    # and the hint made at g is stale, never done
    # (a fresh signature at the current generation lands)
    fresh = _decision(served, owner, uid, back)
    _send(served, entry, owner, uid, fresh)
    assert len([e for e in served.lab.ws.events(key) if e["type"] == "gate.approved"]) == 1


def test_gate_stale_is_a_review_again_class_for_the_phone():
    from orch.relay.framing import refusal_meta, retry_class

    assert retry_class("gate.stale") == "C-review"
    assert refusal_meta("gate.stale") == {"refusal": "gate.stale", "retryable": False}
    assert retry_class("device.invalid") == "D" and retry_class("op.not_allowed") == "D"


def test_a_stale_hint_is_stale_not_done(human, served):  # noqa: F811
    uid, key = human.ticket()
    _ready(human, key)
    entry = _entry(served)
    owner = served.lab.ws.owner
    hid = ok(_request(human, key, _gate(served, entry, owner, key)["gen"]))["data"]["id"]
    human.run("section.set", {"ref": key, "section": "context", "message": "moved"})
    (row,) = ok(human.c.req("phone.status", {"id": hid}))["data"]["hints"]
    assert row["state"] == "stale" and "outcome" not in row
    assert (
        _on_worker(served, lambda: entry.read(_ident(served, owner), "phone.requests", {}, lambda: True))["data"][
            "requests"
        ]
        == []
    )


def test_the_model_judges_the_device_at_append_signed_before_revoke_submitted_after(human, served):  # noqa: F811
    """The bridge's checks (steps 1 to 3) ran when the request arrived; the signature is older than the revoke. The
    model still judges the signing device at the append: ``device.invalid``, nothing lands."""
    uid, key = human.ticket()
    _ready(human, key)
    entry = _entry(served)
    p = served.lab.add_member(scopes=("look", "decide"))
    g = _gate(served, entry, p, key)
    ev = _decision(served, p, uid, g)
    served.lab.revoke_device(p)
    code = _code(lambda: _send(served, entry, p, uid, ev, recheck=lambda: True))
    assert code == "device.invalid"
    assert [e for e in served.lab.ws.events(key) if e["type"] == "gate.approved"] == []
    # control: before the revoke the same shape of event reaches the gate rules (a member is not an approver here
    # or is; either way it is not device.invalid)
    q = served.lab.add_member(scopes=("look", "decide"))
    ev2 = _decision(served, q, uid, g)
    try:
        _send(served, entry, q, uid, ev2)
    except Refusal as r:
        assert r.code != "device.invalid"


# --- push -------------------------------------------------------------------------------------------------------------


def test_the_hint_push_is_a_generic_wake_with_exact_payload_bytes(human, served, tmp_path):  # noqa: F811
    from orch import crypto
    from orch.crypto import push as cpush
    from orch.relay.outbound import OutboundStore
    from orch.relay.push import HINT_KIND, HINT_LABEL, HINT_LOG, Planner
    from orch.wsproto.state import StateDir

    assert HINT_KIND in cpush.PUSH_KINDS and HINT_KIND in cpush.HOST_PUSH_KINDS
    uid, key = human.ticket("Secret title")
    _ready(human, key)
    entry = _entry(served)
    owner = served.lab.ws.owner
    hid = ok(_request(human, key, _gate(served, entry, owner, key)["gen"]))["data"]["id"]
    ob = OutboundStore(StateDir(tmp_path / "ob"), instance_id="i" * 32, now_ms=1_000)
    planner = Planner(served.service.store, ob, hints=lambda n: phonehints.open_after(served.service, n))
    assert _on_worker(served, lambda: planner.run(2_000)) >= 1
    (it,) = [i for i in ob.open_intents(HINT_LOG)]
    assert (it.kind, it.id, it.label, it.priority) == ("ticket.update", hid, "Needs your approval", "high")
    assert it.narrow == frozenset({owner.ref}) and it.ticket == uid
    # nothing of the ticket is in the intent
    assert "Secret title" not in repr(it) and key not in repr(it)
    # the payload that is sealed: exactly {kind, id, label (padded to 80), ts_ms}
    payload = {"kind": it.kind, "id": it.id, "label": cpush.pad_label(it.label), "ts_ms": 12345}
    assert set(payload) == {"kind", "id", "label", "ts_ms"}
    assert len(payload["label"]) == cpush.MAX_LABEL and payload["label"].rstrip(" ") == HINT_LABEL
    assert canon.dumps(payload) == (
        b'{"id":"' + hid.encode() + b'","kind":"ticket.update","label":"' + HINT_LABEL.encode()
        + b" " * (80 - len(HINT_LABEL)) + b'","ts_ms":12345}'
    )  # fmt: skip
    assert cpush._check_payload(payload, cpush.HOST_PUSH_KINDS, padded=True) == payload  # noqa: SLF001
    # replanning finds the cursor: no second intent for the same hint
    assert _on_worker(served, lambda: planner.run(3_000)) == 0
    del crypto


def test_a_cancelled_hint_is_not_pushed(human, served, tmp_path):  # noqa: F811
    from orch.relay.outbound import OutboundStore
    from orch.relay.push import HINT_LOG, Planner
    from orch.wsproto.state import StateDir

    uid, key = human.ticket()
    _ready(human, key)
    entry = _entry(served)
    hid = ok(_request(human, key, _gate(served, entry, served.lab.ws.owner, key)["gen"]))["data"]["id"]
    ok(human.c.req("phone.cancel", {"id": hid}, idem=True))
    ob = OutboundStore(StateDir(tmp_path / "ob"), instance_id="i" * 32, now_ms=1_000)
    planner = Planner(served.service.store, ob, hints=lambda n: phonehints.open_after(served.service, n))
    assert _on_worker(served, lambda: planner.run(2_000)) == 0 and ob.open_intents(HINT_LOG) == []


# --- the hint file fails closed ---------------------------------------------------------------------------------------


def test_an_unparsable_hint_file_is_an_error_never_an_empty_list(human, served):  # noqa: F811
    uid, key = human.ticket()
    _ready(human, key)
    served.service.phone_hints.path.parent.mkdir(parents=True, exist_ok=True)
    served.service.phone_hints.path.write_text("{not json")
    served.service.phone_hints.path.chmod(0o600)
    res = human.c.req("phone.status", {})
    assert res["ok"] is False
    # a structurally wrong file too
    served.service.phone_hints.path.write_text('{"v":1,"n":0,"hints":[{"id":"x"}]}')
    served.service.phone_hints.path.chmod(0o600)
    assert human.c.req("phone.status", {})["ok"] is False
