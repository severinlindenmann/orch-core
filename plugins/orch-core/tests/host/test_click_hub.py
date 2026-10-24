"""#1198 C10: a hub step (join, mode, leave) by click, with the person's backup key typed once in the page and sealed to
the signer (purpose ``hub.backup_key``). The signer opens it, derives the person key ``PK``, checks it is the acting
person's, signs ONLY the owner proof and wipes both; the host checks the proof at the bind (``human.get``) and again at
the submit. Adversarial cases: another person's key, an envelope for another request or review, a missing seal, a
tampered proof, a dev/real mismatch, key material in logs and state."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from orch.click import sealed
from orch.click import view as views
from orch.click.protocol import ClickError
from orch.custody import personkey
from orch.host import hubops
from orch.identity import recovery as rec
from tests.hub.vec import V

from tests.host import human_helpers
from tests.host.test_click import Click, pending, refused

lab, served = human_helpers.lab, human_helpers.served

FAST = 2**10
CODE = rec.code_from_entropy(bytes(range(32)))
OTHER = rec.code_from_entropy(bytes(range(1, 33)))
CARD = V["objects"]["card"]


@pytest.fixture(autouse=True)
def fast_world(monkeypatch):
    """The owner's person key is the one of ``CODE`` (derived at a low scrypt N), the workspace is a dev workspace."""
    from orch import crypto
    from tests.identity import helpers
    from tests.store import helpers as store_helpers

    monkeypatch.setattr(personkey, "_derive_scalar", lambda text: rec._scalar(text, FAST))
    original = helpers.Person
    made = []

    class CodePerson(original):
        def __init__(self):
            super().__init__()
            if not made:  # the first person of a lab is its owner
                made.append(self)
                self.pk = rec._key_at(CODE, FAST)
                self.pk_pub = crypto.public_bytes(self.pk)

    monkeypatch.setattr(helpers, "Person", CodePerson)
    monkeypatch.setattr(store_helpers, "Person", CodePerson)
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)


@pytest.fixture
def click(served):
    c = Click(served)
    yield c
    c.close()


def join_args(**over):
    return {"hub_card": copy.deepcopy(CARD), "hub_id": CARD["o"]["hub_id"], "mode": "selective", **over}


def seal_for(view, served, rid, code=CODE, *, purpose="hub.backup_key", digest=None, wid=None):
    ctx = sealed.Context(purpose, wid or served.wid, rid, digest or view["view_digest"])
    return {"hub.backup_key": sealed.seal(view["view"]["seal"]["pk"], code, ctx)}


def flow(click, op="hub.join", args=None):
    p = click.prepare(op, join_args() if args is None else args)
    v = click.view(p["request_id"])
    click.mono.t += 1.5
    return p, v


def hub_state(served):
    served.service.store.refresh()
    return dict(served.service.store.state.workspace.hub)


def test_a_hub_view_carries_the_seal_and_a_plain_line_and_the_key_is_per_request(click, served):
    p, v = flow(click)
    view = v["view"]
    assert p["confirm"] == "click" and view["confirm"] == {"kind": "click"}
    seal = view["seal"]
    assert seal["purposes"] == ["hub.backup_key"] and seal["v"] == 1 and seal["alg"] == sealed.ALG
    assert view["backup_key_line"] in view["lines"] and "pk" in seal
    again = click.view(p["request_id"])
    assert again["view_digest"] == v["view_digest"]  # the same key for every rebuild: the digest holds
    p2, v2 = flow(click)
    assert v2["view"]["seal"]["pk"] != seal["pk"]  # one key per approval


def test_join_and_leave_by_click_with_the_backup_key(click, served):
    p, v = flow(click)
    res = click.sign(p["request_id"], v["view_digest"], sealed=seal_for(v, served, p["request_id"]))
    assert res["result"] and click.signatures == 1
    assert hub_state(served)["owner_proof"]["o"]["op"] == "hub.join" and hub_state(served)["status"] != "left"
    assert click.signer.approvals[(served.wid, p["request_id"])].seal.closed  # the key for the secret is dropped
    p2, v2 = flow(click, "hub.leave", {})
    click.sign(p2["request_id"], v2["view_digest"], sealed=seal_for(v2, served, p2["request_id"]))
    assert click.signatures == 2
    assert hub_state(served)["status"] == "left"


def test_another_persons_backup_key_signs_nothing_and_the_right_one_still_works(click, served):
    p, v = flow(click)
    err = refused(
        lambda: click.sign(p["request_id"], v["view_digest"], sealed=seal_for(v, served, p["request_id"], OTHER)),
        "human.click_refused",
        "check_failed",
    )
    assert "not yours" in err.message and OTHER not in str(err) and CODE not in str(err)
    assert click.signatures == 0 and hub_state(served) == {}
    click.sign(p["request_id"], v["view_digest"], sealed=seal_for(v, served, p["request_id"]))  # a retry, same review
    assert click.signatures == 1


def test_a_mistyped_backup_key_is_refused_with_plain_words(click, served):
    p, v = flow(click)
    words = CODE.split()
    bad = " ".join(words[:-1] + ["zoo" if words[-1] != "zoo" else "abandon"])
    e = refused(
        lambda: click.sign(p["request_id"], v["view_digest"], sealed=seal_for(v, served, p["request_id"], bad)),
        "human.click_refused",
        "check_failed",
    )
    assert "not a backup key" in e.message and click.signatures == 0


@pytest.mark.parametrize("what", ["request", "workspace", "digest", "purpose"])
def test_an_envelope_made_for_another_request_workspace_or_review_is_refused(click, served, what):
    p, v = flow(click)
    other_rid = "hr_" + "0" * 26
    kw = {
        "request": {"rid": other_rid},
        "workspace": {"wid": "0" * 32},
        "digest": {"digest": "sha256:" + "0" * 64},
        "purpose": {"purpose": "custody.passphrase"},
    }[what]
    kw = dict(kw)
    rid = kw.pop("rid", p["request_id"])
    env = seal_for(v, served, rid, **kw)
    refused(
        lambda: click.sign(p["request_id"], v["view_digest"], sealed=env), "human.click_refused", "check_failed"
    )
    assert click.signatures == 0 and hub_state(served) == {}
    click.sign(p["request_id"], v["view_digest"], sealed=seal_for(v, served, p["request_id"]))  # nothing was burnt
    assert click.signatures == 1


def test_an_envelope_sealed_to_another_signer_key_is_refused(click, served):
    p, v = flow(click)
    p2, v2 = flow(click)
    env = seal_for(v2, served, p["request_id"])  # sealed to the key of request 2, for request 1's context
    refused(lambda: click.sign(p["request_id"], v["view_digest"], sealed=env), "human.click_refused", "check_failed")
    assert click.signatures == 0


def test_the_same_envelope_opens_once(click, served):
    p, v = flow(click)
    env = seal_for(v, served, p["request_id"], OTHER)
    refused(lambda: click.sign(p["request_id"], v["view_digest"], sealed=env), "human.click_refused", "check_failed")
    refused(lambda: click.sign(p["request_id"], v["view_digest"], sealed=env), "human.click_refused", "check_failed")
    assert click.signatures == 0


def test_a_missing_or_wrong_sealed_field_is_refused_before_anything_is_opened(click, served):
    p, v = flow(click)
    env = seal_for(v, served, p["request_id"])
    for bad in (
        {},
        {"sealed": None},
        {"sealed": {}},
        {"sealed": {"hub.backup_key": "text"}},
        {"sealed": {"custody.passphrase": env["hub.backup_key"]}},
        {"sealed": {**env, "github.token": env["hub.backup_key"]}},
    ):
        refused(lambda bad=bad: click.sign(p["request_id"], v["view_digest"], **bad), "invalid.input")
    assert click.signatures == 0 and hub_state(served) == {}
    click.sign(p["request_id"], v["view_digest"], sealed=env)  # still the same, unopened envelope
    assert click.signatures == 1


def test_a_review_that_asks_for_no_secret_refuses_a_sealed_value(click, served):
    q = click.prepare("settings.set", {"set": {"grant_hours": 4}})
    v = click.view(q["request_id"])
    click.mono.t += 1.5
    refused(lambda: click.sign(q["request_id"], v["view_digest"], sealed={"hub.backup_key": {}}), "invalid.input")
    assert click.signatures == 0


def test_a_hub_step_is_a_click_only_with_the_sealed_key(click, served):
    p, _ = flow(click)
    prepared = click.signer.approvals[(served.wid, p["request_id"])].prepared
    common = dict(workspace_id=served.wid, person=served.lab.ws.owner.ref, device=served.lab.ws.owner.device, files=None)
    assert views.check_prepared(prepared, sealed_op=True, **common) == "click"
    refused(lambda: views.check_prepared(prepared, **common), "human.click_refused", "terminal_only")
    refused(lambda: click.prepare("hub.join", join_args(), deposit=True), "invalid.input")


def test_the_proof_is_checked_at_the_bind_not_only_at_submit(click, served):
    p, v = flow(click)
    other = click.signer._owner_proof

    def wrong_request(entry, approval, prepared, envelope):  # a proof the host must refuse at the bind: another request
        proof = other(entry, approval, prepared, envelope)
        proof["o"]["request_id"] = "hr_" + "1" * 26
        return proof

    click.signer._owner_proof = wrong_request
    refused(
        lambda: click.sign(p["request_id"], v["view_digest"], sealed=seal_for(v, served, p["request_id"])),
        "hub.proof_invalid",
    )
    # refused at human.get: no claim was written, nothing signed or submitted, the request is still bound
    assert click.signer.records.get(served.wid, p["request_id"]) is None
    assert click.signatures == 0 and hub_state(served) == {}
    assert pending(served, p["request_id"])["state"] == "bound"


def test_the_host_refuses_a_one_click_bind_without_a_proof(click, served):
    p, _ = flow(click)
    dev = served.lab.ws.owner.device
    c = served.client()
    res = c.req("human.get", {"request_id": p["request_id"], "signer": {"device": dev, "auth": "convenience"}})
    assert human_helpers.err(res) == "hub.proof_invalid" and res["error"]["detail"]["reason"] == "proof_missing"


def test_a_dev_real_mismatch_is_refused(click, served):
    p, v = flow(click)

    def tamper(op, args, res):
        if op == "human.get" and isinstance(res.get("data"), dict) and "owner_proof" in res["data"]:
            res["data"]["owner_proof"] = {"dev": False}  # the host's marker flipped on the way: a real-looking proof
        return res

    click.tamper = tamper
    e = refused(
        lambda: click.sign(p["request_id"], v["view_digest"], sealed=seal_for(v, served, p["request_id"])),
        "hub.proof_invalid",
    )
    assert click.signatures == 0 and hub_state(served) == {}


def test_the_key_material_is_never_in_a_result_a_log_or_the_state(click, served):
    p, v = flow(click)
    res = click.sign(p["request_id"], v["view_digest"], sealed=seal_for(v, served, p["request_id"]))
    owner = served.lab.ws.owner
    scalar = owner.pk.private_numbers().private_value.to_bytes(32, "big")
    needles = [w.encode() for w in CODE.split()[:3]] + [CODE.encode(), scalar, scalar.hex().encode()]
    haystack = repr(res).encode() + repr(v).encode()
    for n in needles:
        assert n not in haystack
    for path in Path(served.lab.state).rglob("*"):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            assert CODE.encode() not in data and scalar not in data and scalar.hex().encode() not in data, path


def test_the_key_is_dropped_when_the_request_is_cancelled(click, served):
    p, v = flow(click)
    held = click.signer.approvals[(served.wid, p["request_id"])]
    click.signer.cancel({"workspace_id": served.wid, "request_id": p["request_id"]})
    assert held.seal.closed


def test_the_signer_signs_the_proof_label_only(click, served, monkeypatch):
    seen = []
    real = personkey.TypedCodeSource.signer

    def spy(self, label):
        seen.append(label)
        return real(self, label)

    monkeypatch.setattr(personkey.TypedCodeSource, "signer", spy)
    p, v = flow(click)
    click.sign(p["request_id"], v["view_digest"], sealed=seal_for(v, served, p["request_id"]))
    assert seen == ["sig_hub_owner_proof"]
    src = personkey.TypedCodeSource(bytearray(CODE.encode()), labels=("sig_hub_owner_proof",), person_id=bytes(16))
    with pytest.raises(personkey.PersonMismatch):
        src.__enter__()


def test_the_typed_code_source_wipes_the_code_refuses_other_labels_and_agents(monkeypatch):
    pid = rec._key_at(CODE, FAST)
    from orch import crypto

    person = crypto.person_id(crypto.public_bytes(pid))
    code = bytearray(CODE.encode())
    with personkey.TypedCodeSource(code, labels=("sig_hub_owner_proof",), person_id=person) as pk:
        assert set(code) == {0}  # wiped on entering, whatever happens next
        assert not hasattr(pk, "raw_sign")
        with pytest.raises(personkey.LabelRefused):
            pk.sign("sig_device_cert", b"x")
        pk.signer("sig_hub_owner_proof")
    with pytest.raises(personkey.PersonKeyClosed):
        pk.signer("sig_hub_owner_proof")(personkey.L["sig_hub_owner_proof"] + b"x")
    monkeypatch.setenv("ORCH_GRANT", "g")
    agent = bytearray(CODE.encode())
    with pytest.raises(personkey.HumanOnly):
        personkey.TypedCodeSource(agent, labels=("sig_hub_owner_proof",), person_id=person).__enter__()
    assert set(agent) == {0}
