"""R2 T1 (#1590): the remote access policy and device list live in the workspace log, signed by the owner with the
remote owner proof (hub contract 10.2c). The model is the authority; no host file, no hub, no agent and no remote
session can turn anything on.

Two layers: the model (``World``, a fake verifier) for who may append what and what a full ``hub.joined`` does, and the
host (served workspace, real proofs) for the owner proof at binding and at submit, the isolation notice and the
custodial refusals replayed against the real host."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from orch.crypto.encoding import b64u
from orch.host import hubproof
from orch.hubclient import wire
from orch.hubclient.errors import HubRefused
from orch.model import Code, admit
from orch.model.types import ts
from orch.store.render import thaw
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import err
from tests.host.test_hub_deliver import Env, hub_snapshot, mint  # noqa: F401 - fixtures and helpers
from tests.host.test_hub_ops import connect_args, dev_workspace, join_args, stub  # noqa: F401 - fixtures
from tests.model.world import World
from tests.schema.examples import hex32

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

DAY_MS = 86_400_000
CONTRACT = Path(__file__).resolve().parents[4] / "contracts" / "hub"
VECTORS = json.loads((CONTRACT / "vectors_remote.json").read_text())


def point(n: int = 0) -> str:
    """A real P-256 public key (b64u of the 65-byte point), fresh each call."""
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.public_key().public_numbers()
    return b64u(b"\x04" + raw.x.to_bytes(32, "big") + raw.y.to_bytes(32, "big"))


# --- the model ----------------------------------------------------------------------------------------------------


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer"})


def changed_payload(w, at: str, **over: Any) -> dict[str, Any]:
    p = {
        "on": True, "level": "work", "until_ms": ts(at) * 1000 + DAY_MS, "premises": "no", "agents_risk": False,
        "hub_mode": "none",
        "remote_v": remote(w).get("version", 0),
    }  # fmt: skip
    return {**p, **over}


def turn_on(w, actor="sev", **over):
    at = w.tick()
    return w.try_("workspace", "remote.changed", actor, at=at, **changed_payload(w, at, **over))


def device(w, at: str, n: int = 1, **over: Any) -> dict[str, Any]:
    raw = n.to_bytes(32, "big")
    transport = b64u(raw)
    presence = over.pop("presence_pub", None) or point()
    fp = wire.device_fp("platform", raw, wire.unb64u(presence, 65)).hex()
    p = {
        "device_id": "rd_" + hex32(f"dev{n}"), "name": f"Laptop {n}", "person": w.people["sev"],
        "presence_kind": "platform", "transport_pub": transport, "presence_pub": presence, "level_cap": "work",
        "expires_ms": ts(at) * 1000 + 30 * DAY_MS, "fingerprint": fp, "code_id": "rc_" + hex32(f"code{n}"),
        "remote_v": remote(w).get("version", 0),
    }  # fmt: skip
    return {**p, **over}


def add_device(w, n: int = 1, actor="sev", **over):
    at = w.tick()
    return w.try_("workspace", "remote.device_added", actor, at=at, **device(w, at, n, **over))


def remote(w):
    return thaw(w.state().workspace.remote)


def code(res) -> tuple[str, str]:
    return res.code, res.detail


def join_hub(w, seq=1, mode="selective", hub="38d8abed52ccfb538da401fd54e64bdc", root=None):
    from tests.model.test_hub_events import ROOT

    return w.try_(
        "workspace", "hub.joined", "sev", hub_id=hub, hk_root_pub=root or ROOT, mode=mode, binding_seq=seq, aliases=[]
    )


def leave_hub(w, seq=1, hub="38d8abed52ccfb538da401fd54e64bdc"):
    return w.try_("workspace", "hub.left", "sev", hub_id=hub, binding_seq=seq)


def test_the_owner_turns_remote_on_and_the_model_holds_the_policy(w):
    e, res = turn_on(w, level="terminals", until_ms=ts(w.at()) * 1000 + 3 * DAY_MS + 10_000)
    assert not hasattr(res, "code"), res
    r = remote(w)
    assert r["on"] is True and r["level"] == "terminals" and r["premises"] == "no" and r["off_reason"] is None
    assert r["devices"] == {} and r["device_version"] == 0


def test_h_actor_cannot_turn_on(w):
    for typ, payload in (
        ("remote.changed", changed_payload(w, w.at())),
        ("remote.device_added", device(w, w.at())),
    ):
        e, res = w.try_("workspace", typ, World.HOST, **payload)
        assert res.code == Code.EVENT_BAD_ACTOR, (typ, res)
    assert remote(w) == {}


def test_a_member_below_owner_cannot_turn_on(w):
    e, res = turn_on(w, actor="mara")
    assert res.code == Code.ROLE_DENIED
    e, res = add_device(w, actor="mara", person=w.people["mara"])
    assert res.code == Code.ROLE_DENIED


def test_only_h_may_record_disable_remove_rotate_and_codes(w):
    turn_on(w)
    for typ, payload in (
        ("remote.disabled", {"by": "owner"}),
        ("remote.code_issued", {"code_id": "rc_" + hex32("c"), "expires_ms": ts(w.at()) * 1000 + 900_000}),
        ("remote.code_burned", {"code_id": "rc_" + hex32("c")}),
        ("remote.device_removed", {"device_id": "rd_" + hex32("d"), "reason": "owner"}),
        ("remote.key_rotated", {"wrk_fp": hex32("fp")}),
    ):
        e, res = w.try_("workspace", typ, "sev", **payload)
        assert res.code == Code.EVENT_BAD_ACTOR, (typ, res)


def test_an_h_line_can_turn_off_but_never_on(w):
    e, res = w.try_("workspace", "remote.disabled", World.HOST, by="owner")
    assert code(res) == (Code.REMOTE_INVALID, "not_on")  # nothing to turn off, and it turns nothing on
    turn_on(w)
    e, res = w.try_("workspace", "remote.disabled", World.HOST, by="owner")
    assert not hasattr(res, "code")
    r = remote(w)
    assert r["on"] is False and r["off_reason"] == "owner"
    e, res = w.try_("workspace", "remote.disabled", World.HOST, by="expired")
    assert res.code == Code.REMOTE_INVALID  # it is off already


def test_the_hub_mode_the_owner_saw_must_be_the_models(w):
    e, res = turn_on(w, hub_mode="selective")
    assert code(res) == (Code.REMOTE_INVALID, "hub_mode_mismatch")
    join_hub(w, mode="selective")
    e, res = turn_on(w, hub_mode="none")
    assert code(res) == (Code.REMOTE_INVALID, "hub_mode_mismatch")
    e, res = turn_on(w, hub_mode="selective", premises="yes", until_ms=ts(w.at()) * 1000 + DAY_MS)
    assert not hasattr(res, "code"), res


def test_premises_yes_full_refused(w):
    join_hub(w, mode="full")
    e, res = turn_on(w, hub_mode="full", premises="yes")
    assert code(res) == (Code.REMOTE_INVALID, "needs_selective")
    e, res = turn_on(w, hub_mode="full", premises="no")  # not at a customer's place: the owner's call
    assert not hasattr(res, "code"), res


def test_until_off_refused_for_premises_yes_and_terminals(w):
    assert code(turn_on(w, premises="yes", until_ms=None)[1]) == (Code.REMOTE_INVALID, "until_required")
    assert code(turn_on(w, level="terminals", until_ms=None)[1]) == (Code.REMOTE_INVALID, "until_required")
    assert not hasattr(turn_on(w, level="work", until_ms=None)[1], "code")  # "until I turn it off": no, below Terminals
    assert code(turn_on(w, until_ms=1)[1]) == (Code.REMOTE_INVALID, "until_past")


@pytest.mark.parametrize("how", ["mode_switch", "leave_rejoin", "other_hub"])
def test_full_join_turns_off(w, how):
    """D93, stricter than "if yes": any full ``hub.joined`` ends remote access, whatever the premises answer."""
    from tests.model.test_hub_events import ROOT

    join_hub(w, mode="selective")
    e, res = turn_on(w, hub_mode="selective", premises="no")
    assert not hasattr(res, "code") and remote(w)["on"] is True
    if how == "mode_switch":
        e, res = join_hub(w, seq=2, mode="full")
    elif how == "leave_rejoin":
        leave_hub(w)
        e, res = join_hub(w, seq=2, mode="full")
    else:
        leave_hub(w)
        root = point()
        e, res = join_hub(w, seq=2, mode="full", hub=wire.hub_id(wire.unb64u(root, 65)), root=root)
    assert not hasattr(res, "code"), res
    r = remote(w)
    assert r["on"] is False and r["off_reason"] == "hub_full"
    e, res = turn_on(w, hub_mode="full", premises="no")  # the owner may turn it on again, knowingly, at the workspace
    assert not hasattr(res, "code"), res
    assert ROOT


def test_a_selective_join_or_a_leave_leaves_remote_on(w):
    turn_on(w)
    join_hub(w, mode="selective")
    assert remote(w)["on"] is True
    leave_hub(w)
    assert remote(w)["on"] is True


def test_a_device_is_listed_with_its_fingerprint_and_the_version_rises(w):
    e, res = add_device(w, 1)
    assert not hasattr(res, "code"), res
    r = remote(w)
    d = r["devices"][e["device_id"]]
    assert d["confirmed_by"] == w.people["sev"] and d["added_ms"] == ts(e["at"]) * 1000 and r["device_version"] == 1
    e2, res = add_device(w, 2)
    assert not hasattr(res, "code") and remote(w)["device_version"] == 2
    removed, res = w.try_("workspace", "remote.device_removed", World.HOST, device_id=e["device_id"], reason="owner")
    assert not hasattr(res, "code")
    r = remote(w)
    assert list(r["devices"]) == [e2["device_id"]] and r["device_version"] == 3


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"fingerprint": "0" * 64}, "fingerprint"),
        ({"person": "p_" + hex32("someone")}, "person_mismatch"),
        ({"expires_ms": 1}, "device_life"),
        ({"expires_ms": 9_999_999_999_999}, "device_life"),
    ],
)
def test_a_bad_device_is_refused(w, over, why):
    e, res = add_device(w, 1, **over)
    assert code(res) == (Code.REMOTE_INVALID, why)
    assert remote(w) == {}


def test_a_device_id_or_key_is_never_used_twice(w):
    first, _ = add_device(w, 1)
    at = w.tick()
    dup_id = device(w, at, 2, device_id=first["device_id"])
    assert code(w.try_("workspace", "remote.device_added", "sev", at=at, **dup_id)[1]) == (
        Code.REMOTE_INVALID,
        "device_exists",
    )
    dup_key = device(w, at, 1, device_id="rd_" + hex32("other"), code_id="rc_" + hex32("c2"))
    dup_key["presence_pub"] = first["presence_pub"]
    dup_key["fingerprint"] = wire.device_fp(
        "platform", wire.unb64u(dup_key["transport_pub"], 32), wire.unb64u(dup_key["presence_pub"], 65)
    ).hex()
    assert code(w.try_("workspace", "remote.device_added", "sev", at=at, **dup_key)[1]) == (
        Code.REMOTE_INVALID,
        "device_exists",
    )


def test_at_most_64_devices(w):
    for n in range(1, 65):
        e, res = add_device(w, n)
        assert not hasattr(res, "code"), (n, res)
    e, res = add_device(w, 65)
    assert code(res) == (Code.REMOTE_INVALID, "full")
    assert len(remote(w)["devices"]) == 64


SIG = "A" * 86  # the schema's shape of a signature


def proof_of(request_id: str) -> dict[str, Any]:
    """An owner proof object the fake verifier accepts; the model reads only its request id."""
    return {"o": {"request_id": request_id}, "sig": SIG}


def test_remote_proof_for_old_counter_refused(w):
    turn_on(w)  # the counter is 1 now
    assert remote(w)["version"] == 1
    at = w.tick()
    e, res = w.try_("workspace", "remote.changed", "sev", at=at, **changed_payload(w, at, remote_v=0))
    assert code(res) == (Code.REMOTE_INVALID, "stale_remote")
    e, res = w.try_("workspace", "remote.changed", "sev", at=at, **changed_payload(w, at, remote_v=2))
    assert code(res) == (Code.REMOTE_INVALID, "stale_remote")  # a future counter is as wrong as an old one
    e, res = w.try_("workspace", "remote.changed", "sev", at=at, **changed_payload(w, at, level="view"))
    assert not hasattr(res, "code") and remote(w)["version"] == 2


def test_same_remote_proof_reused_refused(w):
    rid = "hr_01J9ZP00000000000000000RQ1"
    at = w.tick()
    e, res = w.try_("workspace", "remote.changed", "sev", at=at, owner_proof=proof_of(rid), **changed_payload(w, at))
    assert not hasattr(res, "code"), res
    assert remote(w)["used_requests"] == [rid]
    at = w.tick()  # the right counter now, but the same proof (request id)
    e, res = w.try_("workspace", "remote.changed", "sev", at=at, owner_proof=proof_of(rid), **changed_payload(w, at))
    assert code(res) == (Code.REMOTE_INVALID, "proof_reused")
    at = w.tick()
    e, res = w.try_("workspace", "remote.device_added", "sev", at=at, owner_proof=proof_of(rid), **device(w, at, 1))
    assert code(res) == (Code.REMOTE_INVALID, "proof_reused")  # one request id is one proof, across both line types


def test_replayed_device_add_after_remove_refused(w):
    at = w.tick()
    line = device(w, at, 1)
    e, res = w.try_("workspace", "remote.device_added", "sev", at=at, **line)
    assert not hasattr(res, "code"), res
    w.try_("workspace", "remote.device_removed", World.HOST, device_id=e["device_id"], reason="owner")
    at = w.tick()  # the very same signed payload, delivered again: the counter moved, so it is refused
    e2, res = w.try_("workspace", "remote.device_added", "sev", at=at, **line)
    assert code(res) == (Code.REMOTE_INVALID, "stale_remote")
    assert remote(w)["devices"] == {}


def test_device_id_or_code_id_ever_seen_refused(w):
    first, _ = add_device(w, 1)
    w.try_("workspace", "remote.device_removed", World.HOST, device_id=first["device_id"], reason="owner")
    at = w.tick()  # a fresh proof at the right counter, but the id was used once in the life of the log
    again = device(w, at, 2, device_id=first["device_id"])
    assert code(w.try_("workspace", "remote.device_added", "sev", at=at, **again)[1]) == (
        Code.REMOTE_INVALID,
        "ever_seen",
    )
    same_code = device(w, at, 3, code_id=first["code_id"])
    assert code(w.try_("workspace", "remote.device_added", "sev", at=at, **same_code)[1]) == (
        Code.REMOTE_INVALID,
        "ever_seen",
    )
    fresh = device(w, at, 4)
    assert not hasattr(w.try_("workspace", "remote.device_added", "sev", at=at, **fresh)[1], "code")


def test_key_rotated_records_the_wrk_fingerprint_and_a_removed_device_must_exist(w):
    e, res = w.try_("workspace", "remote.key_rotated", World.HOST, wrk_fp=hex32("w1"))
    assert not hasattr(res, "code") and remote(w)["wrk_fp"] == hex32("w1")
    e, res = w.try_("workspace", "remote.device_removed", World.HOST, device_id="rd_" + hex32("x"), reason="owner")
    assert code(res) == (Code.REMOTE_INVALID, "device_unknown")


def test_a_refused_remote_line_changes_nothing_and_a_failing_proof_refuses(w):
    turn_on(w)
    before = remote(w)
    at = w.tick()
    e = w.build_unappended("workspace", "remote.changed", "sev", at=at, **changed_payload(w, at, level="view"))
    w.verifier.proof_codes[e["id"]] = "proof_invalid"
    res = admit(w.state(), e, log="workspace")
    assert code(res) == (Code.REMOTE_INVALID, "owner_proof: proof_invalid")
    assert remote(w) == before
    call = next(c for c in reversed(w.verifier.proof_calls) if c["event"]["id"] == e["id"])
    assert call["prior"] is None and call["at_ms"] == ts(e["at"]) * 1000  # the line's own time; no place for a remote


def test_a_verifier_without_the_proof_check_refuses_remote_lines(w):
    from orch.model import remote as model_remote
    from orch.model.verifier import RefuseHub

    at = w.tick()
    e = w.build_unappended("workspace", "remote.changed", "sev", at=at, **changed_payload(w, at))
    ws = w.state()._core.ws
    for v in (RefuseHub(), object()):
        r = model_remote.remote_apply(ws, e, v, dev=False)
        assert r.code == Code.REMOTE_INVALID and "unavailable" in r.detail
    ws.remote = {}


# --- the owner proof ----------------------------------------------------------------------------------------------


def remote_args(**over):
    return {"level": "work", "premises": "no", "days": 3, "agents_risk": True, **over}


def submit_with(human, prepared, proof):
    from tests.host.test_hub_owner_proof import submit_with as hub_submit_with

    return hub_submit_with(human, prepared, proof)


def reason(res):
    assert err(res) == "remote.proof_invalid", res
    return res["error"]["detail"]["reason"]


def remote_state(served):
    store = served.lab.ws.other()
    try:
        return thaw(store.state.workspace.remote)
    finally:
        store.close()


def test_remote_changed_needs_owner_proof(human, served):
    prepared = human.prepare("remote.set", remote_args())
    assert prepared["events"][0]["owner_proof"] is None  # prepared with the member, null
    assert reason(submit_with(human, prepared, None)) == "proof_missing"
    assert remote_state(served) == {}  # a refused submit appends nothing
    proof = human.owner_proof(prepared)
    assert proof["o"]["kind"] == "remote.owner_proof" and proof["o"]["op"] == "remote.set"
    assert "data" in ok(submit_with(human, prepared, proof))
    state = remote_state(served)
    assert state["on"] is True and state["level"] == "work" and state["agents_risk"] is True


def test_a_remote_proof_must_be_the_one_of_this_request(human):
    prepared = human.prepare("remote.set", remote_args())
    now = int(human.served.lab.ws.clock[0] * 1000)
    for over, why in (
        ({"request_id": "hr_01J9ZP00000000000000000RQ9"}, "proof_invalid"),
        ({"workspace_id": "0" * 32}, "proof_invalid"),
        ({"issued_ms": now - 700_000, "expires_ms": now - 100_000}, "proof_expired"),
        ({"dev": False}, "dev_mismatch"),
        ({"sign": human.person.dk_sign}, "proof_invalid"),  # the owner's device key is not the person key
        ({"payload": {**hubproof.payload_of(prepared["events"][0]), "level": "view"}}, "proof_invalid"),
    ):
        assert reason(submit_with(human, prepared, human.owner_proof(prepared, **over))) == why, over


def test_hub_proof_not_accepted_for_remote_and_the_reverse(human, served):
    from orch.canon.jcs import dumps as cj

    prepared = human.prepare("remote.set", remote_args())
    proof = human.owner_proof(prepared)
    # a remote proof's object signed under the HUB label
    as_hub = {"o": proof["o"], "sig": b64u(human.person.pk_sign(wire.SIG_OWNER_PROOF + cj(proof["o"])))}
    assert reason(submit_with(human, prepared, as_hub)) == "proof_invalid"
    # a hub-kind object (op remote.set is not a hub op) cannot be built or verified
    o = {**proof["o"], "kind": "hub.owner_proof"}
    forged = {"o": o, "sig": b64u(human.person.pk_sign(wire.SIG_OWNER_PROOF + cj(o)))}
    assert reason(submit_with(human, prepared, forged)) == "proof_invalid"
    with pytest.raises(HubRefused):  # the builder never makes one either
        wire.build_owner_proof(
            workspace_id=served.wid, pk_pub=human.person.pk_pub, op="remote.set",
            payload={**hubproof.payload_of(prepared["events"][0]), "level": "bogus"},
            request_id="hr_01J9ZP00000000000000000RQ9",
            issued_ms=1, expires_ms=2, dev=True, sign=human.person.pk_sign,
        )  # fmt: skip


def test_a_remote_proof_is_not_accepted_for_a_hub_step(human, monkeypatch):
    from orch.canon.jcs import dumps as cj

    prepared = human.prepare("hub.join", join_args())
    proof = human.owner_proof(prepared)
    o = {**proof["o"], "kind": "remote.owner_proof"}
    wrong_family = {"o": o, "sig": b64u(human.person.pk_sign(wire.SIG_REMOTE_OWNER_PROOF + cj(o)))}
    assert human.submit_res  # the hub submit path
    res = submit_with(human, prepared, wrong_family)
    assert err(res) == "hub.proof_invalid" and res["error"]["detail"]["reason"] == "proof_invalid"
    only_label = {"o": proof["o"], "sig": b64u(human.person.pk_sign(wire.SIG_REMOTE_OWNER_PROOF + cj(proof["o"])))}
    res = submit_with(human, prepared, only_label)
    assert err(res) == "hub.proof_invalid" and res["error"]["detail"]["reason"] == "proof_invalid"
    # the hub step still lands with its own proof
    assert "data" in ok(submit_with(human, prepared, human.owner_proof(prepared)))


def test_the_remote_proof_is_made_under_its_own_label_by_the_terminal_signer():
    """The Terminal path asks the person-key source for exactly the remote label for a remote step."""
    from orch.host import signer

    seen: list[tuple[str, ...]] = []

    class Source:
        def __init__(self, *, labels, person_id, action):
            seen.append(tuple(labels))
            self.pk_pub = None

    prepared = {"op": "remote.set", "events": [{"type": "remote.changed"}], "owner_proof": {"dev": True}}
    with pytest.raises(Exception):  # noqa: B017, PT011 - the stub has no key: only the requested label matters here
        signer.terminal_owner_proof(prepared, "hr_01J9ZP00000000000000000RQ9", workspace_id="0" * 32,
                                    person="p_" + "0" * 32, pk_factory=_enter(Source))  # fmt: skip
    assert seen == [("sig_remote_owner_proof",)]


class _enter:
    def __init__(self, source):
        self.source = source

    def __call__(self, **kw):
        self.obj = self.source(**kw)
        return self

    def __enter__(self):
        return self.obj

    def __exit__(self, *exc):
        return False


def test_binding_needs_the_proof_for_the_one_click_key_and_checks_one_that_is_sent(human):
    """One click cannot make the backup-key proof: the bind of a remote step needs one (like a hub step), and a proof
    sent with the bind is checked at once."""
    prepared = human.prepare("remote.set", remote_args())
    rid = prepared["request"]["id"]
    assert ok(human.get(rid, signer=human.signer))["data"]["request"]["id"] == rid  # Terminal: the proof comes later
    other = human.prepare("remote.set", remote_args())
    res = human.get(other["request"]["id"], signer={**human.signer, "auth": "convenience"})
    assert reason(res) == "proof_missing"
    bad = human.owner_proof(prepared, request_id="hr_01J9ZP00000000000000000RQ9")
    res = human.c.req("human.get", {"request_id": rid, "signer": human.signer, "owner_proof": bad})
    assert reason(res) == "proof_invalid"
    res = human.c.req(
        "human.get", {"request_id": rid, "signer": human.signer, "owner_proof": human.owner_proof(prepared)}
    )
    assert res["ok"] is True


# --- the isolation notice (#1198 H1) -------------------------------------------------------------------------------


def test_agents_risk_required_tier_a(human, served, monkeypatch):
    from orch.host import orgstate

    monkeypatch.setattr(orgstate, "isolated", lambda service: False)
    res = human.prepare_res("remote.set", {"level": "work", "premises": "no", "days": 3})
    assert err(res) == "remote.agents_risk"
    prepared = human.prepare("remote.set", remote_args())
    assert prepared["events"][0]["agents_risk"] is True  # the event records that the owner accepted it
    monkeypatch.setattr(orgstate, "isolated", lambda service: True)
    prepared = human.prepare("remote.set", {"level": "work", "premises": "no", "days": 3})
    assert prepared["events"][0]["agents_risk"] is False  # isolated: no risk to record, no flag needed


def test_until_off_refused_by_the_op_for_premises_yes_and_terminals(human, monkeypatch):
    from orch.host import orgstate

    monkeypatch.setattr(orgstate, "isolated", lambda service: True)
    for args in ({"level": "work", "premises": "yes"}, {"level": "terminals", "premises": "no"}):
        assert err(human.prepare_res("remote.set", args)) == "invalid.input"
    prepared = human.prepare("remote.set", {"level": "work", "premises": "no"})  # "until I turn it off": allowed here
    assert prepared["events"][0]["until_ms"] is None


def test_premises_yes_with_a_full_hub_refused_by_the_op(human, served, stub, monkeypatch):  # noqa: F811
    from orch.host import orgstate

    monkeypatch.setattr(orgstate, "isolated", lambda service: True)
    human.run("hub.join", join_args(mode="full"))
    res = human.prepare_res("remote.set", {"level": "work", "premises": "yes", "days": 2})
    assert err(res) == "remote.needs_selective"
    prepared = human.prepare("remote.set", {"level": "work", "premises": "no", "days": 2})
    assert prepared["events"][0]["hub_mode"] == "full"


def test_the_agent_column_cannot_run_the_remote_ops(human, served):
    from tests.host.dispatch_helpers import SESSION

    for op, args in (("remote.set", remote_args()), ("remote.device_confirm", {"pending": "rp_" + "0" * 32,
                     "level_cap": "view", "days": 30})):  # fmt: skip
        res = served.client().req(op, args, grant=served.grant, session=SESSION, idem=True)
        assert res["ok"] is False and res["error"]["code"] in ("op.not_allowed", "human_only", "unknown_command"), res


def test_device_confirm_needs_a_pending_device_and_adds_it_with_the_proof(human, served, monkeypatch):
    from orch.host import orgstate, remotefacts

    monkeypatch.setattr(orgstate, "isolated", lambda service: True)
    res = human.prepare_res("remote.device_confirm", {"pending": "rp_" + "1" * 32, "level_cap": "view", "days": 30})
    assert err(res) == "remote.no_pending"
    pending = {
        "name": "Severin's MacBook", "presence_kind": "platform", "transport_pub": b64u(b"\x07" * 32),
        "presence_pub": point(), "code_id": "rc_" + "2" * 32,
    }  # fmt: skip
    from orch.host.remote import enrollment

    live = {"on": True}  # the enroll stream of the pending device
    monkeypatch.setattr(
        remotefacts, "_pending_source", lambda service, pid: pending if pid == "rp_" + "1" * 32 else None
    )
    monkeypatch.setattr(
        enrollment, "still_pending", lambda service, code_id: live["on"] and code_id == pending["code_id"]
    )
    prepared = human.prepare("remote.device_confirm", {"pending": "rp_" + "1" * 32, "level_cap": "work", "days": 30})
    stale = human.prepare("remote.device_confirm", {"pending": "rp_" + "1" * 32, "level_cap": "view", "days": 30})
    live["on"] = False  # R2 review L2: the stream closed after the prepare
    assert err(submit_with(human, stale, human.owner_proof(stale))) == "remote.no_pending"
    assert not remote_state(served).get("devices")
    live["on"] = True
    ev = prepared["events"][0]
    assert ev["person"] == human.person.ref and ev["level_cap"] == "work" and ev["owner_proof"] is None
    want = wire.device_fp("platform", b"\x07" * 32, wire.unb64u(pending["presence_pub"], 65)).hex()
    assert ev["fingerprint"] == want and ev["device_id"].startswith("rd_")
    assert reason(submit_with(human, prepared, None)) == "proof_missing"
    assert "data" in ok(submit_with(human, prepared, human.owner_proof(prepared)))
    devices = remote_state(served)["devices"]
    assert list(devices) == [ev["device_id"]] and devices[ev["device_id"]]["fingerprint"] == want


def test_the_six_words_of_a_fingerprint_are_the_contract_ones():
    """Contract 16 row 60: the first 66 bits of ``device_fp`` as six BIP-39 words (the vector's fingerprint)."""
    case = next(c for c in VECTORS["cases"] if c["name"] == "device_fp_platform")
    a = case["args"]
    fp = wire.device_fp(a["presence_kind"], bytes.fromhex(a["transport_pub_hex"]), wire.unb64u(a["presence_pub"], 65))
    assert fp.hex() == case["expect"]["hex"]
    words = wire.fingerprint_words(fp)
    assert len(words.split()) == 6 and words == wire.fingerprint_words(fp)
    assert wire.fingerprint_words(bytes(32)).split() == ["abandon"] * 6  # all-zero bits are the first word six times


# --- nothing custodial can touch it ---------------------------------------------------------------------------------


@pytest.fixture
def env(served, human, stub):  # noqa: F811
    return Env(served, human)


REMOTE_TYPES = ("remote.changed", "remote.device_added", "remote.disabled")


@pytest.mark.parametrize("typ", REMOTE_TYPES)
def test_custodial_remote_types_refused(env, typ):
    """``event_deliver`` with ``auth: "hub"``: no remote type is custodial, whatever the role or the payload."""
    before = (len(env.lines("workspace")), len(env.lines(env.uid)))
    e = mint(
        env.wid,
        "workspace",
        typ,
        id="01J9ZP00000000000000099999",
        based_on=env.head("workspace"),
        roster_v=env.roster_v(),
    )
    assert env.deliver(e, "workspace") is not None
    assert (len(env.lines("workspace")), len(env.lines(env.uid))) == before
    store = env.store()
    try:
        assert dict(store.state.workspace.remote) == {}
    finally:
        store.close()


CUSTODIAL = [c for c in VECTORS["cases"] if c["check"] == "custodial_cannot"]


@pytest.mark.parametrize("case", CUSTODIAL, ids=[c["name"] for c in CUSTODIAL])
def test_custodial_cannot_replayed_on_the_real_host(env, case):
    """Every ``custodial_cannot_*`` vector, minted as a real hub-signed delivery to the real host: refused, and
    nothing lands in any log (§8.5 "No custodial type can")."""
    ev = copy.deepcopy(case["args"]["event"])
    log = case["args"]["log"]
    typ = ev["type"]
    payload = {k: v for k, v in ev.items() if k not in ("v", "id", "based_on", "hash_v", "roster_v", "type")}
    before = (len(env.lines("workspace")), len(env.lines(env.uid)))
    e = mint(
        env.wid, log if log == "workspace" else env.uid, typ, id="01J9ZP00000000000000088888",
        based_on=env.head(log if log == "workspace" else env.uid), roster_v=env.roster_v(), **payload,
    )  # fmt: skip
    assert env.deliver(e, "workspace" if log == "workspace" else env.uid) is not None
    assert (len(env.lines("workspace")), len(env.lines(env.uid))) == before


def test_no_remote_type_is_a_hub_type_and_every_remote_type_has_a_column():
    from orch.model import authz
    from orch.ops import actors

    remote_types = {t for t in authz.ACTOR_RULES if t.startswith("remote.")}
    assert remote_types == {
        "remote.changed", "remote.device_added", "remote.disabled", "remote.code_issued", "remote.code_burned",
        "remote.device_removed", "remote.key_rotated", "remote.session_opened", "remote.session_closed",
    }  # fmt: skip
    assert not remote_types & authz.HUB_TYPES
    assert {t for t, letters in authz.ACTOR_RULES.items() if t.startswith("remote.") and "P" in letters} == {
        "remote.changed",
        "remote.device_added",
    }
    assert remote_types <= set(actors.EVENT_ACTORS)
