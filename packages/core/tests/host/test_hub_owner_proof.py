"""#1198 C9: the host enforces the hub owner proof (hub contract 9.6) on every path: at binding (``human.get``), at
submit (under the store lock), in the model (every ``hub.joined`` / ``hub.left`` that is appended or replayed), at the
signer (the Terminal path asks for the backup key), and in the review (the proof line, not the proof). The reader
gate (``hub_too_old``) is here too."""

from __future__ import annotations

import copy

import pytest

from orch import canon
from orch.host import convenience, hubops, hubproof, review, signer
from orch.host.control_client import new_idem
from orch.hubclient import wire
from orch.identity import sign_person_event
from orch.store.render import thaw
from tests.host import human_helpers
from tests.host.human_helpers import err
from tests.hub.vec import V

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

CARD = V["objects"]["card"]


@pytest.fixture(autouse=True)
def dev_workspace(monkeypatch):
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)


def join_args(**over):
    return {"hub_card": copy.deepcopy(CARD), "mode": "selective", **over}


class RawBackend:
    """The owner's device key without the prompt's description (which would refuse a null proof): a hostile or old
    signer that signs whatever it is given."""

    auth = "passphrase"
    name = "raw"
    person_capable = True

    def __init__(self, person):
        self.person = person

    def sign(self, key_id, payload, *, action):
        return self.person.dk_sign(payload)


def submit_with(human, prepared, proof):
    """Sign the prepared hub event with ``proof`` as it is (no signer check) and submit it."""
    events = copy.deepcopy(prepared["events"])
    for ev in events:
        ev["owner_proof"] = proof
        ev["sig"] = sign_person_event(RawBackend(human.person), "dk", human.served.wid, prepared["log"], ev, action="t")
    return human.submit_res(prepared["request"]["id"], events)


def reason(res):
    assert err(res) == "hub.proof_invalid", res
    return res["error"]["detail"]["reason"]


# --- the host at submit ---------------------------------------------------------------------------


def test_a_hub_event_without_a_proof_is_refused_at_submit(human):
    prepared = human.prepare("hub.join", join_args())
    assert reason(submit_with(human, prepared, None)) == "proof_missing"


def test_a_valid_proof_lands_and_the_event_carries_it(human, served):
    prepared = human.prepare("hub.join", join_args())
    assert prepared["events"][0]["owner_proof"] is None  # prepared with the member, null
    proof = human.owner_proof(prepared)
    assert "data" in human_helpers.ok(submit_with(human, prepared, proof))
    store = served.lab.ws.other()
    try:
        assert canon.dumps(thaw(store.state.workspace.hub["owner_proof"])) == canon.dumps(proof)  # hub.connect sends it
        (event,) = [e for e, _ in store.events_after("workspace", 0, 99) if e["type"] == "hub.joined"]
        assert canon.dumps(thaw(event["owner_proof"])) == canon.dumps(proof)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"sign": "device"}, "proof_invalid"),  # the owner's device key is not the person key
        ({"request_id": "hr_01J9ZP00000000000000000RQ9"}, "proof_invalid"),  # a proof of another request
        ({"workspace_id": "0" * 32}, "proof_invalid"),
        ({"op": "hub.mode"}, "proof_invalid"),  # the proof of another op
        ({"issued_ms": -1}, "proof_expired"),  # replaced below with the lab clock
        ({"dev": False}, "dev_mismatch"),  # a real proof at a dev host
    ],
)
def test_a_wrong_proof_is_refused_at_submit(human, over, code):
    prepared = human.prepare("hub.join", join_args())
    now = human.served.lab.ws.clock[0] * 1000
    over = dict(over)
    if over.get("sign") == "device":
        over["sign"] = human.person.dk_sign
    if over.get("issued_ms") == -1:
        over.update(issued_ms=now - 700_000, expires_ms=now - 100_000)
    if over.get("op") == "hub.mode":  # a mode-switch proof needs a full payload; reuse the join's with mode full
        full = {**hubproof.payload_of(prepared["events"][0]), "mode": "full"}
        over["payload"] = full
    proof = human.owner_proof(prepared, **over)
    assert reason(submit_with(human, prepared, proof)) == code


def test_a_proof_over_another_payload_is_refused(human):
    prepared = human.prepare("hub.join", join_args())
    other = {**hubproof.payload_of(prepared["events"][0]), "mode": "full"}
    proof = human.owner_proof(prepared, payload=other)
    assert reason(submit_with(human, prepared, proof)) == "proof_invalid"


def test_the_proof_is_never_taken_from_another_person(human, served):
    prepared = human.prepare("hub.join", join_args())
    stranger = wire.build_owner_proof(
        workspace_id=served.wid, pk_pub=human.person.pk_pub, op="hub.join",
        payload=hubproof.payload_of(prepared["events"][0]), request_id=prepared["request"]["id"],
        issued_ms=served.lab.ws.clock[0] * 1000, expires_ms=served.lab.ws.clock[0] * 1000 + 600_000, dev=True,
        sign=lambda msg: b"\0" * 64,
    )  # fmt: skip
    assert reason(submit_with(human, prepared, stranger)) == "proof_invalid"  # not signed by the roster's PK


def test_a_refused_submit_appends_nothing(human, served):
    prepared = human.prepare("hub.join", join_args())
    submit_with(human, prepared, None)
    store = served.lab.ws.other()
    try:
        assert dict(store.state.workspace.hub) == {}
    finally:
        store.close()


def test_leave_needs_the_proof_too(human):
    human.run("hub.join", join_args())
    prepared = human.prepare("hub.leave", {})
    assert reason(submit_with(human, prepared, None)) == "proof_missing"
    assert "data" in human_helpers.ok(submit_with(human, prepared, human.owner_proof(prepared)))


# --- binding --------------------------------------------------------------------------------------


def convenience_signer(human):
    return {**human.signer, "auth": "convenience"}


def test_binding_without_a_proof_works_for_the_terminal_signer_and_not_for_the_one_click_key(human):
    prepared = human.prepare("hub.join", join_args())
    rid = prepared["request"]["id"]
    assert human_helpers.ok(human.get(rid, signer=human.signer))["data"]["request"]["id"] == rid
    other = human.prepare("hub.join", join_args())
    res = human.get(other["request"]["id"], signer=convenience_signer(human))
    assert reason(res) == "proof_missing"


def test_binding_checks_a_proof_that_is_sent(human):
    prepared = human.prepare("hub.join", join_args())
    rid = prepared["request"]["id"]
    bad = human.owner_proof(prepared, request_id="hr_01J9ZP00000000000000000RQ9")
    res = human.c.req("human.get", {"request_id": rid, "signer": human.signer, "owner_proof": bad})
    assert reason(res) == "proof_invalid"
    good = human.owner_proof(prepared)
    res = human.c.req("human.get", {"request_id": rid, "signer": human.signer, "owner_proof": good})
    assert res["ok"] is True
    # a One-click key with a good proof gets past the proof; the One-click rules (switched off here) are next
    other = human.prepare("hub.join", join_args())
    res = human.c.req(
        "human.get",
        {
            "request_id": other["request"]["id"],
            "signer": convenience_signer(human),
            "owner_proof": human.owner_proof(other),
        },
    )
    assert err(res) == "sign.convenience_unavailable"


# --- the reader gate ------------------------------------------------------------------------------


@pytest.mark.parametrize("op", ["hub.join", "hub.mode"])
def test_a_hub_below_the_required_reader_level_is_refused(human, monkeypatch, op):
    if op == "hub.mode":
        human.run("hub.join", join_args())
        args = {"hub_card": copy.deepcopy(CARD), "mode": "full"}
    else:
        args = join_args()
    monkeypatch.setattr(convenience, "_required", lambda service, *, history: CARD["o"]["reader"] + 1)
    res = human.prepare_res(op, args)
    assert err(res) == "sign.convenience_readers"
    assert res["error"]["detail"]["reason"] == "hub_too_old"


def test_a_hub_at_the_required_level_is_fine(human, monkeypatch):
    monkeypatch.setattr(convenience, "_required", lambda service, *, history: CARD["o"]["reader"])
    human.prepare("hub.join", join_args())


# --- the review -----------------------------------------------------------------------------------


def test_the_review_shows_a_proof_line_and_the_fingerprint_does_not_cover_the_proof(human):
    prepared = human.prepare("hub.join", join_args())
    block = prepared["material"]["review"][0]
    assert "owner_proof" not in block["payload"] and block["derived"] == {"owner_proof": review.OWNER_PROOF_LINE}
    text = signer.render(prepared, lambda wid: ("Demo", "DEMO"))
    assert "owner proof: signed with your backup key" in text
    before = review.fingerprint(prepared["review_digest"], prepared["events"], "hub.join")
    signed = human.sign(prepared)
    assert signed.events[0]["owner_proof"] is not None
    assert review.fingerprint(prepared["review_digest"], signed.events, "hub.join") == before


# --- the signer: the Terminal (backup key) path ---------------------------------------------------


class FakeRecoveryCode:
    """Stands for ``RecoveryCodeSource`` over the Lab owner's person key (the real one types the code on /dev/tty)."""

    def __init__(self, owner):
        self.owner, self.seen = owner, []
        self.pk_pub = owner.pk_pub

    def __call__(self, **kw):
        self.seen.append(kw)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def signer(self, label):
        assert label == "sig_hub_owner_proof"
        return self.owner.pk_sign


def test_the_terminal_makes_the_proof_with_the_backup_key_only_for_this_label(human, served):
    prepared = human.prepare("hub.join", join_args())
    fake = FakeRecoveryCode(human.person)
    now = served.lab.ws.clock[0] * 1000
    proof = signer.terminal_owner_proof(
        prepared, prepared["request"]["id"], workspace_id=served.wid, person=human.person.ref,
        now_ms=lambda: now, pk_factory=fake,
    )  # fmt: skip
    assert fake.seen[0]["labels"] == ("sig_hub_owner_proof",)  # least privilege: no certificate, no revocation
    assert fake.seen[0]["person_id"] == bytes.fromhex(human.person.ref[2:])
    o = proof["o"]
    assert (o["op"], o["request_id"], o["issued_ms"], o["expires_ms"]) == (
        "hub.join", prepared["request"]["id"], now, now + 600_000,
    )  # fmt: skip
    assert o["payload"] == hubproof.payload_of(prepared["events"][0])
    assert human_helpers.ok(submit_with(human, prepared, proof))["data"]


def test_the_terminal_refuses_a_prepared_request_without_the_dev_marker(human, served):
    prepared = human.prepare("hub.join", join_args())
    prepared.pop("owner_proof")
    with pytest.raises(signer.SignerRefused):
        signer.terminal_owner_proof(
            prepared, prepared["request"]["id"], workspace_id=served.wid, person=human.person.ref,
            pk_factory=FakeRecoveryCode(human.person),
        )  # fmt: skip


def test_sign_refuses_a_hub_event_without_the_proof_and_a_proof_on_a_prepared_event(human):
    prepared = human.prepare("hub.join", join_args())
    with pytest.raises(signer.SignerRefused):
        human.sign(prepared, proof=None)
    prepared["events"][0]["owner_proof"] = human.owner_proof(prepared)
    with pytest.raises(signer.SignerRefused):
        signer.check(
            prepared, workspace_id=human.served.wid, person=human.person.ref, device=human.person.device,
            auth="passphrase",
        )  # fmt: skip


def test_the_composite_asks_for_the_backup_key_after_the_confirmation(human, served):
    prepared = human.prepare("hub.join", join_args())
    calls = []

    def call(op, args, **kw):
        calls.append(op)
        return human_helpers.ok(human.c.req(op, args, **kw))

    order = []
    result = signer.finish(
        call, prepared, prepared["request"]["id"], backend=human.backend, key_id="dk", workspace_id=served.wid,
        person=human.person.ref, device=human.person.device, workspace=lambda wid: ("Demo", "DEMO"),
        confirm=lambda text, word: order.append("confirm") or True, show_secret=lambda s: None, idem=new_idem(),
        owner_proof=lambda p, rid: order.append("proof") or human.owner_proof(p),
    )  # fmt: skip
    assert order == ["confirm", "proof"] and calls[-1] == "human.submit"
    assert result["data"]["hub_id"]


# --- the model through the real engine and the crypto verifier (no fake verifier) -------------------------


def append_raw(served, typ, **fields):
    """Append a hub event the owner's device signed, straight to the log: the host's own checks are bypassed, so what
    refuses it is the model (``Store.append`` -> ``admit`` with the real ``CryptoVerifier``)."""

    def run(store):
        served.lab.ws.tick()
        return store.append(
            served.lab.ws.person_event(served.lab.ws.owner, "workspace", typ, **fields), log="workspace"
        )

    return served.append(run)


def test_a_proof_replayed_into_a_later_event_is_stale_binding(human, served):
    from orch.store import StoreError

    human.run("hub.join", join_args())
    hub = served.service.store.state.workspace.hub
    proof = thaw(hub["owner_proof"])
    joined = {k: hub[k] for k in ("hub_id", "hk_root_pub", "mode", "binding_seq")} | {"aliases": []}
    with pytest.raises(StoreError) as e:  # the same join again while bound: the proof is genuine, its place is not
        append_raw(served, "hub.joined", **joined, owner_proof=proof)
    assert "stale_binding" in str(e.value)
    human.run("hub.leave", {})
    with pytest.raises(StoreError) as e:  # after the leave, the old join (binding 1) cannot come back either
        append_raw(served, "hub.joined", **joined, owner_proof=proof)
    assert "stale_binding" in str(e.value)


def test_an_event_appended_after_the_proof_ran_out_is_proof_expired(human, served):
    from orch.store import StoreError

    human.run("hub.join", join_args())
    prepared = human.prepare("hub.leave", {})
    now = int(served.lab.ws.clock[0] * 1000)
    short = human.owner_proof(prepared, issued_ms=now, expires_ms=now + 1_000)
    body = hubproof.payload_of(prepared["events"][0])
    served.lab.ws.clock[0] += 700  # the line's own time is past the proof's life
    with pytest.raises(StoreError) as e:
        append_raw(served, "hub.left", **body, owner_proof=short)
    assert "proof_expired" in str(e.value)
    served.lab.ws.clock[0] -= 700
    ok_ = append_raw(served, "hub.left", **body, owner_proof=human.owner_proof(prepared))
    assert ok_ is not None  # the same event with a live proof lands
