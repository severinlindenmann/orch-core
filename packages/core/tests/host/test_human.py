"""H5a, the human contract through a real host (§0.8.1): prepare, get, submit, cancel; the request states; replay
precedence; grants; multi-event requests and their roll-forward; the CLI composite with a real passphrase prompt."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
from pathlib import Path

import pytest

import orch.ops as ops
from orch import canon
from orch.custody.describe import describe_payload
from orch.host import review, signer
from orch.host.control_client import new_idem
from orch.ops import plans
from orch.ops.base import Context, Result
from tests.host import human_helpers
from tests.host.dispatch_helpers import UID
from tests.host.human_helpers import Human, err, ok

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

INVARIANT = ("op", "workspace_id", "log", "material", "review_digest", "fingerprint", "origin")


def tick(served, seconds: int) -> None:
    served.lab.ws.clock[0] += seconds


def requests_dir(served) -> Path:
    return served.lab.state / "hosts" / served.wid / "requests"


def record(served, request_id: str) -> dict:
    return json.loads((requests_dir(served) / request_id).read_text())


def outcome(served, idem: str) -> dict:
    o = served.service.outcomes
    return o.read(o.key(served.wid, f"uid:{UID}", idem))


def ready(human: Human, key: str) -> None:
    """Fill what the requirements gate needs, as person edits through the contract."""
    for sid in ("context", "requirements", "out_of_scope"):
        human.run("section.set", {"ref": key, "section": sid, "message": f"the {sid}"})
    human.run("ac.add", {"ref": key, "text": "it works"})


def logged(served, key: str) -> list[dict]:
    return served.lab.ws.events(key)


class TwoNotes:
    """``log`` rebound to plan two ``log.added`` events: a multi-event person request."""

    def __enter__(self):
        self.original = ops.get("log").handler

        def handler(ctx: Context, args: dict) -> Result:
            def build(c, p, a):
                p.add({"type": "log.added", "text": a["text"] + " (1)"})
                p.add({"type": "log.added", "text": a["text"] + " (2)"})
                return plans.Out({}, ["orch show"])

            return plans.run(ctx, "log", args, build)

        ops.bind("log", handler)
        return self

    def __exit__(self, *exc):
        ops.bind("log", self.original)


# --- the contract shape ---------------------------------------------------------------------------------------------


def test_prepare_answers_the_prepared_contract(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "hello"})
    assert set(p) == {"request", *INVARIANT, "events"}
    assert re.fullmatch(r"hr_[0-7][0-9A-HJKMNP-TV-Z]{25}", p["request"]["id"])
    assert "code" not in p["request"] and p["request"]["state"] == "bound"
    assert re.fullmatch(r"[0-9a-f]{12}", p["fingerprint"])
    assert p["review_digest"] == review.review_digest(p["material"])
    assert p["origin"]["uid"] == UID and p["origin"]["pid"] == os.getpid() and p["origin"]["client"] == "test"
    (ev,) = p["events"]
    assert ev["actor"] == {"kind": "person", "id": human.person.ref, "device": human.person.device}
    assert ev["auth"] == "passphrase" and "sig" not in ev and "seq" not in ev
    assert ev["based_on"] == served.service.store.log_head(served.lab.ws.uid(key))
    assert p["material"] == {
        "kind": "payload",
        "ticket": {"key": key, "uid": p["log"]},
        "review": [review.of(ev, p["material"])],
    }


def test_dry_run_stores_nothing_and_has_no_request(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "hello"}, dry_run=True)
    assert p["request"] is None and p["events"][0]["text"] == "hello"
    assert not requests_dir(served).exists() or os.listdir(requests_dir(served)) == []
    assert err(human.c.req("human.get", {"request_id": "hr_" + "0" * 26}, dry_run=True)) == "invalid.input"


def test_a_dry_run_without_signer_or_handoff_persists_nothing(human, served):
    _, key = human.ticket()
    before = list(logged(served, key))
    pending_before = ok(human.c.req("human.pending", {}))["data"]
    body = {"op": "log", "args": {"ref": key, "text": "only a look"}}
    p = ok(human.c.req("human.prepare", body, dry_run=True))["data"]
    assert p["request"] is None and p["events"][0]["text"] == "only a look"
    assert not requests_dir(served).exists() or os.listdir(requests_dir(served)) == []  # no request record
    assert logged(served, key) == before  # no event
    assert ok(human.c.req("human.pending", {}))["data"] == pending_before  # the request store is unchanged
    # the same call for real still needs a signer (handoff:true is only the hub's add-on grant); never both
    assert err(human.c.req("human.prepare", body)) == "invalid.input"
    assert err(human.c.req("human.prepare", {**body, "handoff": True})) == "invalid.input"
    assert ok(human.c.req("human.prepare", {**body, "handoff": True}, dry_run=True))["data"]["request"] is None
    both = {**body, "signer": human.signer, "handoff": True}
    assert err(human.c.req("human.prepare", both, dry_run=True)) == "invalid.input"
    assert not requests_dir(served).exists() or os.listdir(requests_dir(served)) == []


def test_only_person_ops_are_prepared(human, served):
    _, key = human.ticket()
    for op in ("claim", "task.done", "show", "import.v1", "nonsense"):
        assert err(human.prepare_res(op, {})) == "op.not_allowed", op
    assert err(human.c.req("human.prepare", {"op": "log", "args": {"ref": key, "text": "x"}})) == "invalid.input"
    both = {"op": "log", "args": {"ref": key, "text": "x"}, "signer": human.signer, "handoff": True}
    assert err(human.c.req("human.prepare", both)) == "invalid.input"


def test_a_hand_off_is_stored_only_for_the_hubs_add_on_grant(human, served):
    """FB15: ``handoff: true`` (no signer) stores an unbound request for ``addon.grant`` only, marked for the hub;
    every other op is refused and stores nothing (it is prepared with a signer); a hand-off never takes a signer."""
    _, key = human.ticket()
    for op, args in (
        ("log", {"ref": key, "text": "x"}),
        ("verdict", {"ref": key, "outcome": "pass"}),
        ("member.remove", {"person": human.person.ref}),
        ("settings.set", {"set": {"grant_hours": 4}}),
        ("grant", {"ref": key}),
        ("addon.disable", {"name": "wiki"}),
    ):
        res = human.prepare_res(op, args, handoff=True)
        assert err(res) == "invalid.input" and "add-on grant only" in res["error"]["message"], op
    assert not requests_dir(served).exists() or os.listdir(requests_dir(served)) == []
    grant = {"op": "addon.grant", "args": {"name": "wiki"}, "handoff": True}
    assert err(human.c.req("human.prepare", {**grant, "signer": human.signer})) == "invalid.input"
    handed = human.prepare("addon.grant", {"name": "wiki"}, handoff=True)
    assert handed["request"]["state"] == "prepared"
    assert record(served, handed["request"]["id"])["hub_handoff"] is True


def test_an_agent_and_a_grant_get_human_only(human, served):
    _, key = human.ticket()
    res = human.c.req("human.prepare", {"op": "log", "args": {"ref": key, "text": "x"}}, grant=served.grant)
    assert err(res) == "human_only"


# --- the state table ------------------------------------------------------------------------------------------------


def test_every_state_transition_of_the_table(human, served):
    _, key = human.ticket()
    # prepare with signer -> bound; with handoff (the hub's add-on grant, the one unbound prepare) -> prepared
    bound = human.prepare("log", {"ref": key, "text": "a"})
    assert bound["request"]["state"] == "bound"
    handed = human.prepare("addon.grant", {"name": "wiki"}, handoff=True)
    assert handed["request"]["state"] == "prepared"
    assert handed["events"][0]["actor"]["device"] is None and handed["events"][0]["auth"] is None
    # submit on an unbound request: request_state
    hid = handed["request"]["id"]
    fake = [{**handed["events"][0], "sig": "x"}]
    assert err(human.submit_res(hid, fake)) == "human.request_state"
    # prepared -> bound (get with signer); bound -> bound (the same signer again)
    got = ok(human.get(hid, signer=human.signer))["data"]
    assert got["request"]["state"] == "bound"
    assert ok(human.get(hid, signer=human.signer))["data"]["request"]["state"] == "bound"
    # another device on a bound request: request_state
    other = {"device": "d_" + "0" * 32, "auth": "passphrase"}
    assert err(human.get(hid, signer=other)) == "human.request_state"
    # bound -> submitted
    signed = human.sign(got)
    ok(human.submit_res(hid, signed.events))
    assert ok(human.get(hid))["data"]["request"]["state"] == "submitted"
    assert err(human.get(hid, signer=human.signer)) == "human.request_state"
    assert ok(human.c.req("human.cancel", {"request_id": hid}))["data"] == {"state": "submitted"}
    # bound -> cancelled; prepared -> cancelled
    bid = bound["request"]["id"]
    assert ok(human.c.req("human.cancel", {"request_id": bid}))["data"] == {"state": "cancelled"}
    assert ok(human.get(bid))["data"]["request"]["state"] == "cancelled"
    assert err(human.get(bid, signer=human.signer)) == "human.request_state"
    p2 = human.prepare_unbound("log", {"ref": key, "text": "c"})
    assert ok(human.c.req("human.cancel", {"request_id": p2["request"]["id"]}))["data"] == {"state": "cancelled"}
    signed_bound = human.sign(bound)
    assert err(human.submit_res(bid, signed_bound.events)) == "human.request_state"
    # prepared and bound -> expired after 10 minutes
    p3 = human.prepare_unbound("log", {"ref": key, "text": "d"})
    p4 = human.prepare("log", {"ref": key, "text": "e"})
    tick(served, 601)
    for rid in (p3["request"]["id"], p4["request"]["id"]):
        assert ok(human.get(rid))["data"]["request"]["state"] == "expired"  # get without signer: any state
        assert err(human.get(rid, signer=human.signer)) == "human.request_expired"
    assert err(human.submit_res(p4["request"]["id"], human.sign(p4).events)) == "human.request_expired"


def test_a_request_found_expired_at_append_is_refused_under_the_lock(human, served, monkeypatch):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "late"})
    signed = human.sign(p)
    requests = served.service.requests
    real = requests.load

    def load_then_age(rid, principal):  # the clock passes 10 minutes between the checks and the append
        out = real(rid, principal)
        tick(served, 601)
        return out

    monkeypatch.setattr(requests, "load", load_then_age)
    assert err(human.submit_res(p["request"]["id"], signed.events)) == "human.request_expired"
    assert len(logged(served, key)) == 1


def test_get_returns_every_invariant_byte_and_binding_changes_only_device_and_auth(human, served):
    _, key = human.ticket()
    p = human.prepare("addon.grant", {"name": "wiki"}, handoff=True)  # the one request prepared unbound
    rid = p["request"]["id"]
    plain = ok(human.get(rid))["data"]
    assert canon.dumps(plain) == canon.dumps(p)  # get without signer: byte identical
    bound = ok(human.get(rid, signer=human.signer))["data"]
    for k in INVARIANT:
        assert canon.dumps(bound[k]) == canon.dumps(p[k]), k
    for before, after in zip(p["events"], bound["events"], strict=True):
        assert before["actor"]["device"] is None and before["auth"] is None
        assert after["actor"]["device"] == human.person.device and after["auth"] == "passphrase"
        strip = lambda e: {**{k: v for k, v in e.items() if k != "auth"}, "actor": {**e["actor"], "device": None}}  # noqa: E731
        assert canon.dumps(strip(after)) == canon.dumps(strip(before))
    assert bound["request"]["state"] == "bound" and p["request"]["state"] == "prepared"


# --- submit: mismatch, single use, replay precedence ----------------------------------------------------------------


def test_a_submitted_event_that_differs_is_a_mismatch(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "exact"})
    rid = p["request"]["id"]
    for change in (
        lambda e: e.update(text="other"),
        lambda e: e.update(extra=1),
        lambda e: e.update(id=e["id"][:-1] + ("0" if e["id"][-1] != "0" else "1")),
        lambda e: e["actor"].update(device="d_" + "1" * 32),
        lambda e: e.update(secret_hash="sha256:" + "1" * 64),  # set aside only on grant.issued
    ):
        events = [copy.deepcopy(p["events"][0])]
        change(events[0])
        events[0]["sig"] = "AAAA"
        res = human.submit_res(rid, events)
        assert err(res) == "human.request_mismatch", (events, res)
    assert err(human.submit_res(rid, [*human.sign(p).events, *human.sign(p).events])) == "human.request_mismatch"
    assert len(logged(served, key)) == 1
    ok(human.submit_res(rid, human.sign(p).events))  # still bound: the refusals changed nothing


def test_a_bad_signature_is_refused_and_appends_nothing(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "sig"})
    events = human.sign(p).events
    events[0]["sig"] = human.sign(human.prepare("log", {"ref": key, "text": "other"})).events[0]["sig"]
    assert err(human.submit_res(p["request"]["id"], events)) == "sig.invalid"
    assert len(logged(served, key)) == 1


def test_single_use_identical_events_replay_the_stored_result_and_others_get_request_state(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "once"})
    rid = p["request"]["id"]
    signed = human.sign(p)
    first_idem = new_idem()
    first = ok(human.submit_res(rid, signed.events, idem=first_idem))
    assert first["duplicate"] is False and first["key"] == key
    fresh = new_idem()
    again = ok(human.submit_res(rid, signed.events, idem=fresh))  # the same signed events, a fresh idem
    assert again == {**first, "duplicate": True}
    # the fresh idem got its own record with the same outcome state, so a retry of it replays at step 2
    assert outcome(served, fresh)["step"] == "committed"
    assert ok(human.submit_res(rid, signed.events, idem=fresh)) == {**first, "duplicate": True}
    assert ok(human.c.req("outcome.get", {"idem": fresh}))["data"]["state"] == "committed"
    resigned = human.sign(p)  # ECDSA: other signature bytes over the same event
    assert err(human.submit_res(rid, resigned.events)) == "human.request_state"
    assert len(logged(served, key)) == 2  # one append, ever


def test_submit_replay_precedence_against_missing_conflicting_expired_and_fresh_idems(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "idem"})
    rid = p["request"]["id"]
    signed = human.sign(p)
    assert err(human.c.req("human.submit", {"request_id": rid, "events": signed.events})) == "idem.required"
    assert err(human.c.req("human.submit", {"request_id": rid, "events": signed.events}, idem=True, dry_run=True)) == (
        "invalid.input"
    )
    idem = new_idem()
    ok(human.submit_res(rid, signed.events, idem=idem))
    # the same idem with another binding: idem.conflict (before any request-level rule)
    other = human.prepare("log", {"ref": key, "text": "other"})
    assert err(human.submit_res(other["request"]["id"], human.sign(other).events, idem=idem)) == "idem.conflict"
    # a refusal under a fresh idem is stored too (failed): its retry replays it
    bad = new_idem()
    assert err(human.submit_res("hr_" + "0" * 26, signed.events, idem=bad)) == "not_found"
    assert outcome(served, bad)["step"] == "failed"
    assert err(human.submit_res("hr_" + "0" * 26, signed.events, idem=bad)) == "not_found"
    # a tombstone: idem.expired, even though the request would now answer its stored result
    tick(served, 24 * 3600 + 5)
    served.service.submit(served.service.outcomes.sweep).result(10)
    assert outcome(served, idem)["step"] == "tombstone"
    assert err(human.submit_res(rid, signed.events, idem=idem)) == "idem.expired"
    assert len(logged(served, key)) == 2


def test_submit_from_another_workspaces_socket_is_not_found(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "elsewhere"})
    rid = p["request"]["id"]
    signed = human.sign(p)
    elsewhere = served.lab.state / "hosts" / ("f" * 32) / "requests"
    elsewhere.mkdir(parents=True, mode=0o700)
    shutil.move(requests_dir(served) / rid, elsewhere / rid)  # prepared on another workspace's socket
    for res in (human.submit_res(rid, signed.events), human.get(rid), human.c.req("human.cancel", {"request_id": rid})):
        assert err(res) == "not_found"
    assert len(logged(served, key)) == 1


def test_stale_between_prepare_and_submit(human, served, lab):
    _, key = human.ticket()
    ready(human, key)
    approve = human.prepare("approve", {"ref": key, "gate": "requirements"})
    assert approve["material"]["kind"] == "gate"
    note = human.prepare("log", {"ref": key, "text": "roster"})
    human.run("section.set", {"ref": key, "section": "requirements", "message": "changed"})  # the gate moved
    assert err(human.submit_res(approve["request"]["id"], human.sign(approve).events)) == "gate.stale"
    lab.add_member()  # the roster moved (a foreign append the store refreshes)
    assert err(human.submit_res(note["request"]["id"], human.sign(note).events)) == "members.stale"


def test_a_decision_and_an_answer_round_trip_with_their_material(human, served):
    _, key = human.ticket()
    ready(human, key)
    prepared, res, _ = human.run("approve", {"ref": key, "gate": "requirements"})
    m = prepared["material"]
    assert m["gate"] == "requirements" and m["ticket"]["key"] == key
    assert prepared["events"][0]["hash"] == canon.gate_hash(m["input"])
    assert m["review"][0]["derived"] == {"ticket_key": key}
    assert res["data"] == {"gate": "requirements"} and logged(served, key)[-1]["type"] == "gate.approved"
    human.agent("ask", {"ref": key, "text": "which one?", "options": ["a", "b"]})
    prepared, res, _ = human.run("answer", {"ref": key, "question": "Q1", "option": "a"})
    assert prepared["material"]["kind"] == "question" and prepared["material"]["question"]["text"] == "which one?"
    assert logged(served, key)[-1]["type"] == "question.answered"


# --- grants (F1) ----------------------------------------------------------------------------------------------------


def test_the_grant_secret_is_drawn_by_the_signer_and_never_reaches_the_host(human, served):
    p = human.prepare("grant", {"hours": 2, "scope": "workable"})
    (ev,) = p["events"]
    assert ev["type"] == "grant.issued" and ev["secret_hash"] is None
    assert p["material"]["review"][0]["payload"]["secret_hash"] is None
    signed = human.sign(p)
    assert signed.secret is not None and signed.events[0]["secret_hash"] == canon.grant_secret_hash(
        __import__("orch.identity", fromlist=["parse_grant"]).parse_grant(signed.secret).secret
    )
    res = ok(human.submit_res(p["request"]["id"], signed.events))
    local = res["data"].pop("until_local")  # the same moment in this computer's time (the text line shows it)
    assert local and not local.endswith("Z")
    assert res["data"] == {"grant": ev["grant"], "until": ev["expires_at"], "scope": "workable"}
    secret = signed.secret.partition(".")[2]
    for path in served.lab.state.rglob("*"):  # the host's state: requests, outcomes, keys, logs of the host
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path
    for path in Path(served.lab.root).rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path
    assert secret not in json.dumps(ok(human.get(p["request"]["id"])))
    # the grant works with the secret the signer drew
    assert ok(human.c.req("whoami", {}, grant=signed.secret))["data"]["grant"]["id"] == ev["grant"]


def test_a_grant_prepared_with_a_secret_hash_is_refused_by_the_signer(human):
    p = human.prepare("grant", {"hours": 1})
    p["events"][0]["secret_hash"] = "sha256:" + "a" * 64
    with pytest.raises(signer.SignerRefused, match="secret_hash null"):
        human.sign(p)
    assert human.backend.payloads == []
    assert err(human.prepare_res("grant", {"hours": 1, "secret_hash": "sha256:" + "a" * 64})) == "invalid.input"


def test_a_grant_submit_needs_a_well_formed_new_secret_hash(human, served):
    p = human.prepare("grant", {"hours": 1})
    events = human.sign(p).events
    for bad in (None, "nope", served.service.store.grant_secret_hash(served.lab.ws.grant_id)):
        e = [{**events[0], "secret_hash": bad}]
        assert err(human.submit_res(p["request"]["id"], e)) == "human.request_mismatch"


# --- multi-event requests -------------------------------------------------------------------------------------------


def test_a_multi_event_request_is_all_or_nothing(human, served):
    _, key = human.ticket()
    with TwoNotes():
        p = human.prepare("log", {"ref": key, "text": "pair"})
    assert [e["type"] for e in p["events"]] == ["log.added", "log.added"]
    assert len({e["based_on"] for e in p["events"]}) == 1
    events = human.sign(p).events
    events[1]["sig"] = events[0]["sig"]  # the second signature is wrong: nothing lands, not even the first
    assert err(human.submit_res(p["request"]["id"], events)) == "sig.invalid"
    assert len(logged(served, key)) == 1
    res = ok(human.submit_res(p["request"]["id"], human.sign(p).events))
    assert [e["text"] for e in logged(served, key)[1:]] == ["pair (1)", "pair (2)"] and res["seq"] == 3


class Crash(BaseException):
    pass


def test_a_crash_between_two_appends_is_rolled_forward_at_start_and_ends_committed(lab, monkeypatch):
    from tests.host.dispatch_helpers import Served

    s = Served(lab)
    try:
        h = Human(s)
        _, key = h.ticket()
        with TwoNotes():
            p = h.prepare("log", {"ref": key, "text": "crash"})
        signed = h.sign(p)
        store = s.service.store
        real = store.append
        calls = []

        def crash_on_second(event, **kw):
            calls.append(event.get("id"))
            if len(calls) == 2:
                raise Crash()  # the host dies between the two appends
            return real(event, **kw)

        monkeypatch.setattr(store, "append", crash_on_second)
        idem = new_idem()
        res = h.submit_res(p["request"]["id"], signed.events, idem=idem)
        assert res["error"]["code"] == "internal"  # the request dies with the host
        rid = p["request"]["id"]
        wid = s.wid
    finally:
        s.close()
    assert len(lab.ws.events(key)) == 2  # only the first landed
    assert record(s, rid)["outcome"] is None and record(s, rid)["submit"] is not None
    with lab.host(clock=lambda: lab.ws.clock[0]) as host:
        service = host.host.services[wid]
        after = json.loads((lab.state / "hosts" / wid / "requests" / rid).read_text())
        assert after["state"] == "submitted" and after["outcome"]["state"] == "committed"
        assert [e["text"] for e in lab.ws.events(key)[1:]] == ["crash (1)", "crash (2)"]
        o = service.outcomes.read(service.outcomes.key(wid, f"uid:{UID}", idem))
        assert o["step"] == "committed" and o["result"]["seq"] == 3
        from tests.host.outcome_helpers import WsClient

        ws = WsClient(lab.run / wid / "host.sock", wid, UID)
        try:
            assert ok(ws.call("outcome.get", {"idem": idem}))["data"]["state"] == "committed"
            again = ok(ws.call("human.submit", {"request_id": rid, "events": signed.events}, "r1", opts={"idem": idem}))
            assert again["duplicate"] is True and again["seq"] == 3
        finally:
            ws.close()


# --- the CLI composite with a real passphrase prompt ----------------------------------------------------------------


def test_the_composite_prompt_fields_equal_describe_payload_of_the_signed_bytes(lab, tmp_path):
    from orch.custody.passphrase import KdfParams, PassphraseBackend
    from orch.identity import certs
    from tests.host.dispatch_helpers import Served
    from tests.identity.helpers import Person

    asked = []
    words = "orbit candle meadow zipper falcon"
    backend = PassphraseBackend(
        tmp_path / "dk", _passphrase_provider=lambda r: asked.append(r) or words, _kdf=KdfParams(2**15, 8, 1)
    )
    pub = backend.create("dk", passphrase=words)
    p = Person()
    p.sig_pub = pub
    device = certs.device_ref(pub)
    cert = certs.make_device_cert(
        p.pk_pub,
        p.pk_sign,
        dk_sig_pub=pub,
        dk_kx_pub=p.kx_pub,
        label_sealed=b"x",
        created_ms=lab.ws.clock[0] * 1000,
        expires_ms=None,
        scopes_max=["look", "decide", "operate", "type"],
    )
    store = lab.ws.other()
    lab.ws.store = store
    try:
        from orch import crypto

        store.append(
            lab.ws.person_event(
                lab.ws.owner,
                "workspace",
                "member.added",
                person=p.ref,
                name="quinn",
                role="maintainer",
                pk_pub=crypto.b64u(p.pk_pub),
                device_cert=cert,
            ),
            log="workspace",
        )
    finally:
        store.close()
        lab.ws.store = None
    lab.peers = lambda person=None, uid=UID: {str(uid): {"class": "person", "person": p.ref}}
    s = Served(lab)
    try:
        c = s.client()
        _, key = s.ticket()

        def call(op, args, files=None, idem=None):
            return ok(c.req(op, args, files=files, idem=idem))

        shown = []
        res = signer.run(
            call,
            "log",
            {"ref": key, "text": "via the composite"},
            backend=backend,
            key_id="dk",
            workspace_id=s.wid,
            person=p.ref,
            device=device,
            workspace=lambda w: ("Acme", "DEMO") if w == s.wid else None,
            confirm=lambda text: shown.append(text) or True,
            show_secret=lambda t: None,
            idem=new_idem(),
        )
        assert res["key"] == key
        (text,) = shown
        assert (
            "in workspace Acme (DEMO)" in text  # the plain view first ...
            and "self-declared" not in text
            and "workspace: Acme (DEMO)" in text.details  # ... the full review behind `d`
            and "self-declared, unverified" in text.details
        )
        event = s.lab.ws.events(key)[-1]
        assert event["text"] == "via the composite"
        signing = canon.person_signing_bytes(s.wid, s.lab.ws.uid(key), event)
        (req,) = asked
        assert req.fields == tuple(describe_payload(signing))
        assert ("text", "via the composite") in req.fields
    finally:
        s.close()


def test_the_composite_cancels_and_signs_nothing_when_the_person_does_not_confirm(human, served):
    _, key = human.ticket()

    def call(op, args, files=None, idem=None):
        return ok(human.c.req(op, args, files=files, idem=idem))

    with pytest.raises(__import__("orch.ops.errors", fromlist=["OrchError"]).OrchError, match="nothing was signed"):
        signer.run(
            call,
            "log",
            {"ref": key, "text": "no"},
            backend=human.backend,
            key_id="dk",
            workspace_id=served.wid,
            person=human.person.ref,
            device=human.person.device,
            workspace=lambda w: ("Acme", "DEMO"),
            confirm=lambda text: False,
            show_secret=lambda t: None,
            idem=new_idem(),
        )
    assert human.backend.payloads == []
    (rid,) = os.listdir(requests_dir(served))
    assert record(served, rid)["state"] == "cancelled"


def test_the_composite_shows_the_grant_secret_only_after_the_commit(human, served):
    shown = []

    def call(op, args, files=None, idem=None):
        if op == "human.submit":
            assert shown == []  # nothing shown before the host confirmed the commit
        return ok(human.c.req(op, args, files=files, idem=idem))

    res = signer.run(
        call,
        "grant",
        {"hours": 1},
        backend=human.backend,
        key_id="dk",
        workspace_id=served.wid,
        person=human.person.ref,
        device=human.person.device,
        workspace=lambda w: ("Acme", "DEMO"),
        confirm=lambda text: True,
        show_secret=shown.append,
        idem=new_idem(),
    )
    (text,) = shown
    assert text.startswith("\nORCH_GRANT=" + res["data"]["grant"] + ".")


def test_a_persons_new_creates_the_ticket_with_the_prepared_key_and_a_taken_key_is_refused(human, served):
    first = human.prepare("new", {"title": "Mine", "type": "bug"})
    second = human.prepare("new", {"title": "Also mine"})
    (ev,) = first["events"]
    assert ev["type"] == "ticket.created" and ev["owner"] == human.person.ref and ev["based_on"] is None
    assert first["material"]["ticket"] == {"key": ev["key"], "uid": first["log"]}
    assert second["events"][0]["key"] == ev["key"]  # the key the store would allocate now, for both
    res = ok(human.submit_res(first["request"]["id"], human.sign(first).events))
    assert res["key"] == ev["key"] and res["data"] == {"title": "Mine", "ticket_type": "bug"}
    created = served.lab.ws.events(ev["key"])[0]
    assert created["actor"]["kind"] == "person" and created["title"] == "Mine"
    assert err(human.submit_res(second["request"]["id"], human.sign(second).events)) == "ticket.exists"
    assert err(human.prepare_res("new", {"title": "x", "priority": "high"})) == "invalid.input"


def test_every_person_version_of_an_agent_op_round_trips(human, served):
    import base64

    _, key = human.ticket()
    b64 = base64.b64encode(b"evidence bytes\n").decode()
    b64b = base64.b64encode(b"replaced bytes\n").decode()
    steps = [
        ("set", {"ref": key, "pairs": ["title=Renamed", "priority=high"]}, None, "ticket.updated"),
        ("section.set", {"ref": key, "section": "summary", "message": "why"}, None, "ticket.updated"),
        ("ac.add", {"ref": key, "text": "it works"}, None, "ticket.updated"),
        ("ac.edit", {"ref": key, "ac": "AC1", "text": "it really works"}, None, "ticket.updated"),
        ("log", {"ref": key, "text": "a note"}, None, "log.added"),
        ("ask", {"ref": key, "text": "ok?", "options": ["y", "n"]}, None, "question.asked"),
        ("artifact.add", {"ref": key, "path": "proof.txt"}, {"path": {"b64": b64}}, "artifact.added"),
        (
            "artifact.replace",
            {"ref": key, "name": "proof.txt", "path": "p2.txt"},
            {"path": {"b64": b64b}},
            "artifact.replaced",
        ),
    ]
    for op, args, files, typ in steps:
        prepared = ok(human.c.req("human.prepare", {"op": op, "args": args, "signer": human.signer}, files=files))
        prepared = prepared["data"]
        human.check(prepared)
        ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
        last = logged(served, key)[-1]
        assert last["type"] == typ and last["actor"]["kind"] == "person", op
    view = served.lab.ws.ticket_json(key)
    assert view["title"] == "Renamed" and view["acceptance"][0]["text"] == "it really works"
    # an agent op whose events a person may not append (task.*) is not a person op
    assert err(human.prepare_res("task.start", {"task": f"{key}/T1"})) == "op.not_allowed"


def test_a_dry_run_prepare_of_addon_grant_installs_nothing(human, served):
    """A built-in add-on that was never installed is put in place by a real grant only: a dry-run prepare through the
    host (ctx.dry_run is False there, the flag rides on the capture) leaves no package."""
    from pathlib import Path

    package = Path(served.service.store.layout.store) / "addons" / "terminals"
    assert not package.exists()
    assert err(human.prepare_res("addon.grant", {"name": "terminals"}, dry_run=True)) == "not_found"
    assert not package.exists()
