"""V3 C2a: ``hub.join`` and ``hub.leave``, the owner's signed records (hub contract 9.1). Selective mode only, human
only, signed with the owner's local key; the hub never holds WK_e and no ``hub`` member is added."""

from __future__ import annotations

import copy

import pytest

from orch.host import hubops, review, signcontent, signer
from orch.ops import person_ops
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION
from tests.host.human_helpers import err
from tests.hub.vec import V

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

CARD = V["objects"]["card"]
HUB_ID = V["ids"]["hub"]
ROOT = V["keys"]["hk_root"]["pub"]


@pytest.fixture(autouse=True)
def dev_workspace(monkeypatch):
    """The lab is not a proven sandbox: tests that need a dev workspace say so here (the real proof is devmode's)."""
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)


def join_args(**over):
    return {"hub_card": copy.deepcopy(CARD), "mode": "selective", **over}


def hub_state(served):
    store = served.lab.ws.other()
    try:
        return dict(store.state.workspace.hub), dict(store.state.workspace.members)
    finally:
        store.close()


def test_the_ops_are_declared_human_only_and_emit_one_event():
    assert person_ops.get("hub.join").who == person_ops.get("hub.leave").who == "human"
    assert review.person_op_types("hub.join") == frozenset({"hub.joined"})
    assert review.person_op_types("hub.leave") == frozenset({"hub.left"})
    assert review.events_fit("hub.join", ["hub.joined"]) and not review.events_fit("hub.join", ["hub.left"])
    assert not review.events_fit("hub.leave", ["hub.joined"]) and not review.events_fit("hub.join", ["hub.joined"] * 2)


def test_click_and_convenience_never_sign_it():
    from orch.click import protocol

    for op in ("hub.join", "hub.mode", "hub.leave"):
        assert protocol.op_class(op) == "terminal"  # not a click op, not a typed-word op: terminal only


def test_join_selective_signs_hub_joined_and_adds_no_member(human, served):
    _, members_before = hub_state(served)
    prepared, done, signed = human.run("hub.join", join_args())
    (event,) = signed.events
    assert event["type"] == "hub.joined" and prepared["log"] == "workspace"
    assert event["mode"] == "selective" and event["binding_seq"] == 1 and event["hub_id"] == HUB_ID
    assert event["hk_root_pub"] == ROOT and event["aliases"] == []
    assert done["data"] == {"hub_id": HUB_ID, "mode": "selective", "binding_seq": 1}
    hub, members = hub_state(served)
    assert hub["status"] == "joined" and hub["mode"] == "selective" and hub["hub_id"] == HUB_ID
    assert members == members_before  # no hub member, ever, in selective mode
    assert not any("hub" in m for m in members)


def test_the_review_says_what_the_hub_does_not_get(human):
    prepared = human.prepare("hub.join", join_args())
    content = signcontent.build(prepared, verified=True)
    assert content["headline"] == f"Join hub {HUB_ID[:12]} in selective mode: the hub sees only what you share"
    notes = " ".join(content["notes"])
    assert "never gets the workspace key" in notes and "not a member" in notes
    assert signer._PLAIN["hub.join"].format(hub_id=HUB_ID, mode="selective").startswith("join hub")


def test_leave_signs_hub_left_naming_the_binding(human, served):
    human.run("hub.join", join_args())
    prepared, done, signed = human.run("hub.leave", {})
    (event,) = signed.events
    assert event["type"] == "hub.left" and (event["hub_id"], event["binding_seq"]) == (HUB_ID, 1)
    assert hub_state(served)[0]["status"] == "left"


def test_a_rejoin_needs_a_higher_binding_seq(human, served):
    human.run("hub.join", join_args())
    human.run("hub.leave", {})
    _, _, signed = human.run("hub.join", join_args())
    assert signed.events[0]["binding_seq"] == 2


def test_a_full_join_says_the_hub_reads_everything_and_adds_the_hub_member(human, served):
    prepared = human.prepare("hub.join", join_args(mode="full"))
    content = signcontent.build(prepared, verified=True)
    assert (
        content["headline"]
        == f"Join hub {HUB_ID[:12]} in full mode: the hub will be able to read everything in this workspace"
    )
    assert "the hub will be able to read everything in this workspace" in " ".join(content["notes"])
    assert signer._PLAIN["hub.join.full"].format(hub_id=HUB_ID).endswith("read everything in this workspace")
    _, done, signed = human.run("hub.join", join_args(mode="full"))
    assert signed.events[0]["mode"] == "full" and done["data"]["mode"] == "full"
    hub, _ = hub_state(served)
    assert hub["mode"] == "full" and hub["member"] == {"kind": "hub", "hub_id": HUB_ID}
    human.run("hub.leave", {})
    hub, _ = hub_state(served)
    assert hub["status"] == "left" and "member" not in hub  # the hub member goes with the leave


def test_selective_to_full_is_only_the_owner_signed_mode_verb(human, served):
    assert err(human.prepare_res("hub.mode", {"hub_card": copy.deepcopy(CARD), "mode": "full"})) == "hub.not_joined"
    human.run("hub.join", join_args())
    hub, _ = hub_state(served)
    assert "member" not in hub
    prepared = human.prepare("hub.mode", {"hub_card": copy.deepcopy(CARD), "mode": "full"})
    content = signcontent.build(prepared, verified=True)
    assert "read everything in this workspace" in content["headline"]
    _, done, signed = human.run("hub.mode", {"hub_card": copy.deepcopy(CARD), "mode": "full"})
    assert (
        signed.events[0]["mode"] == "full"
        and signed.events[0]["binding_seq"] == 2
        and signed.events[0]["aliases"] == []
    )
    hub, _ = hub_state(served)
    assert hub["status"] == "joined" and hub["mode"] == "full" and hub["binding_seq"] == 2 and hub["member"]
    # V3-D29 (C10): full mode may be signed again by the owner while the direct path is open (it only triggers the
    # cut-over); a join is still refused
    assert err(human.prepare_res("hub.join", join_args(mode="full"))) == "hub.already_joined"


def test_mode_switch_is_not_a_join_and_not_for_another_hub(human, served):
    human.run("hub.join", join_args())
    card = copy.deepcopy(CARD)
    card["o"]["hub_id"] = "0" * 32
    assert err(human.prepare_res("hub.mode", {"hub_card": card, "mode": "full"})) == "hub.refused"
    assert err(human.prepare_res("hub.mode", {"hub_card": copy.deepcopy(CARD), "mode": "selective"})) != ""
    assert "member" not in hub_state(served)[0]


def test_full_to_selective_is_a_leave_then_a_selective_join(human, served):
    human.run("hub.join", join_args(mode="full"))
    assert err(human.prepare_res("hub.join", join_args())) == "hub.already_joined"  # no direct downgrade
    human.run("hub.leave", {})
    human.run("hub.join", join_args())
    hub, _ = hub_state(served)
    assert hub["mode"] == "selective" and "member" not in hub and hub["binding_seq"] == 2


def test_joining_twice_and_leaving_without_a_join_are_refused(human):
    assert err(human.prepare_res("hub.leave", {})) == "hub.not_joined"
    human.run("hub.join", join_args())
    assert err(human.prepare_res("hub.join", join_args())) == "hub.already_joined"


def test_a_real_workspace_refuses_a_dev_hub(human, monkeypatch):
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: False)
    assert err(human.prepare_res("hub.join", join_args())) == "hub.refused"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["o"].update(hub_id="0" * 32),
        lambda c: c["o"].update(card_seq=9),
        lambda c: c.update(sig="AAAA"),
        lambda c: c.pop("op_cert"),
    ],
)
def test_a_bad_hub_card_is_refused(human, mutate):
    card = copy.deepcopy(CARD)
    mutate(card)
    assert err(human.prepare_res("hub.join", join_args(hub_card=card))) == "hub.refused"


def test_a_card_of_another_hub_than_the_invite_names_is_refused_and_nothing_is_written(human, served):
    """D12c/G5: a valid card of a different hub (whoever handed over the file) is not what the owner was told."""
    res = human.prepare_res("hub.join", join_args(hub_id="0" * 32))
    assert err(res) == "hub.refused" and "other_hub" in res["error"]["message"]
    hub, _ = hub_state(served)
    assert not hub  # no hub.joined, no pin
    assert err(human.prepare_res("hub.join", join_args(hub_id=HUB_ID.upper()))) == "invalid.input"  # not lower-case hex


def test_a_join_without_the_invites_hub_id_is_refused(human, served):
    args = {"hub_card": copy.deepcopy(CARD), "mode": "selective"}
    assert (
        err(human.c.req("human.prepare", {"op": "hub.join", "args": args, "signer": human.signer})) == "invalid.input"
    )
    assert not hub_state(served)[0]


def test_the_card_of_the_hub_the_invite_names_is_accepted(human):
    _, _, signed = human.run("hub.join", join_args(hub_id=HUB_ID))
    assert signed.events[0]["hub_id"] == HUB_ID


def test_only_the_owner_may_be_aliased(human):
    other = {"person_id": V["ids"]["person_alice"], "user_id": V["ids"]["user_alice"]}
    assert err(human.prepare_res("hub.join", join_args(aliases=[other]))) == "hub.refused"
    me = {"person_id": human.person.ref.removeprefix("p_"), "user_id": V["ids"]["user_alice"]}
    _, _, signed = human.run("hub.join", join_args(aliases=[me]))
    assert signed.events[0]["aliases"] == [me]


def test_agents_are_refused(human, served):
    c = served.client()
    mode_args = {"hub_card": copy.deepcopy(CARD), "mode": "full"}
    for op, args in (
        ("hub.join", join_args()),
        ("hub.join", join_args(mode="full")),
        ("hub.mode", mode_args),
        ("hub.leave", {}),
    ):
        assert err(c.req(op, args, idem=True)) == "human_only"
        assert err(c.req(op, args, grant=served.grant, session=SESSION, idem=True)) == "human_only"
    for op in ("hub.connect", "hub.unbind", "hub.status"):
        args = {"invite": "x", "hub_card": copy.deepcopy(CARD)} if op == "hub.connect" else {}
        assert err(c.req(op, args, grant=served.grant, session=SESSION, idem=True)) in ("op.not_allowed", "human_only")
    assert hub_state(served)[0] == {}


# --- the signer binds each event to its op ---------------------------------------------------------------------------


def _prepared(op, event, log="workspace"):
    env = {"v": 2, "id": "x", "actor": {}, "auth": "a", "hash_v": 1, "roster_v": 1, "based_on": "b", "sig": "s"}
    return {"op": op, "log": log, "events": [{**env, **event}], "material": {}}


JOINED = {
    "type": "hub.joined",
    "hub_id": HUB_ID,
    "hk_root_pub": ROOT,
    "mode": "selective",
    "binding_seq": 1,
    "aliases": [],
}


def test_the_signer_refuses_what_hub_join_must_not_sign():
    signer._person_op(_prepared("hub.join", JOINED))  # the exact shape passes
    signer._person_op(_prepared("hub.join", {**JOINED, "mode": "full"}))  # C2b: the review says what full means
    for bad in (
        {**JOINED, "mode": "weird"},
        {**JOINED, "wk_seal": "AA"},  # no key material rides along
        {**JOINED, "member": "p_x"},
        {k: v for k, v in JOINED.items() if k != "aliases"},
        {**JOINED, "type": "hub.left"},
    ):
        with pytest.raises(signer.SignerRefused):
            signer._person_op(_prepared("hub.join", bad))
    with pytest.raises(signer.SignerRefused):
        signer._person_op(_prepared("hub.join", JOINED, log="DEMO-1"))
    with pytest.raises(signer.SignerRefused):  # another op cannot carry the event either
        signer._person_op(_prepared("settings.set", JOINED))
    left = {"type": "hub.left", "hub_id": HUB_ID, "binding_seq": 1}
    signer._person_op(_prepared("hub.leave", left))
    with pytest.raises(signer.SignerRefused):
        signer._person_op(_prepared("hub.leave", {**left, "extra": 1}))
    with pytest.raises(signer.SignerRefused):
        signer._person_op(_prepared("hub.leave", JOINED))


# --- the host ops: connect, unbind, status --------------------------------------------------------------------------


class StubHub:
    """A hub that takes every message and answers it, signed by the vector HK_op. It checks nothing: the contract's
    checks are in tests/hub (the fake hub); here we look at what the host SENDS."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.snapshot_gets: list[dict] = []

    def send(self, message):
        from tests.hub.fake import sign_with
        from tests.hub.vec import private_key

        m = message["o"]
        # connect fetches the first snapshot; the stub answers {}, which the host refuses and audits (the op itself is
        # covered by tests/hub/test_snapshot.py and test_hub_join_real.py), so it is kept apart from what join sends
        (self.snapshot_gets if m["op"] == "snapshot_get" else self.sent).append(copy.deepcopy(message))
        o = {
            "v": 2, "suite": 2, "kind": "hub.link", "hv": 0, "hub_id": m["hub_id"], "workspace_id": m["workspace_id"],
            "dir": "h2w", "seq": 1, "ts_ms": m["ts_ms"], "op": m["op"], "re": m["seq"], "body": {},
        }  # fmt: skip
        return sign_with(private_key("hk_op"), "sig_hub_route", o)

    def ops(self):
        return [m["o"]["op"] for m in self.sent]


@pytest.fixture
def stub(monkeypatch, served):
    hub = StubHub()
    store = served.lab.ws.other()
    try:
        genesis = store.genesis_event()
    finally:
        store.close()
    card = {
        "delegation": {"o": {"wsk_pub": genesis["wsk_pub"]}},
        "o": {},
        "sig": "",
        "sealed": "",
    }  # shape the stub needs
    monkeypatch.setattr(hubops, "CARD_SOURCE", lambda service: card)
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: hub)
    return hub


def connect_args(**over):
    return {"invite": "invite-1", "hub_card": copy.deepcopy(CARD), **over}


def test_connect_before_a_join_is_refused(human, served, stub):
    c = served.client()
    assert err(c.req("hub.connect", connect_args(), idem=True)) == "hub.not_joined"
    assert err(c.req("hub.unbind", {}, idem=True)) == "hub.not_joined"
    assert stub.sent == []


def test_join_then_connect_sends_the_selective_binding_and_never_a_key(human, served, stub):
    from tests.host.dispatch_helpers import ok

    human.run("hub.join", join_args())
    got = ok(served.client().req("hub.connect", connect_args(), idem=True))["data"]
    assert got["status"] == "bound" and got["binding_seq"] == 1 and got["hub_id"] == HUB_ID
    assert stub.ops() == ["join"]
    body = stub.sent[0]["o"]["body"]
    assert set(body) == {"invite", "card", "pk_pub", "binding"}
    b = body["binding"]["o"]
    assert (b["mode"], b["binding_seq"], b["dev"], b["hub_id"]) == ("selective", 1, True, HUB_ID)
    keys = set()

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                keys.add(k)
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(body)
    assert "wk_seal" not in stub.ops() and not {k for k in keys if "wk" in k.lower().replace("wsk", "") or "wrap" in k}
    status = ok(served.client().req("hub.status", {}, idem=False))["data"]
    assert status["joined"] and status["link"] == "bound" and status["mode"] == "selective"
    _, members = hub_state(served)
    assert not any("hub" in m for m in members)  # still no hub member


def test_status_reports_an_unreachable_hub_and_a_reachable_one(human, served, stub, monkeypatch):
    """V3 D78 (#1102): `link` is not `bound` while the hub address does not answer; the binding stays visible."""
    import socket

    from orch.host import hublinkprobe
    from tests.host.dispatch_helpers import ok

    human.run("hub.join", join_args())
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    hublinkprobe.clear()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    monkeypatch.setattr(hubops, "_addr", lambda args, state, dev: f"127.0.0.1:{port}")
    up = ok(served.client().req("hub.status", {}, idem=False))["data"]
    assert (up["link"], up["binding"], up["reachable"]) == ("bound", "bound", True)
    srv.close()  # the hub process dies
    hublinkprobe.clear()
    down = ok(served.client().req("hub.status", {}, idem=False))["data"]
    assert (down["link"], down["binding"], down["reachable"]) == ("unreachable", "bound", False)
    assert down["peer_outbox"] == 0 and down["link_error"] is None


def test_connect_with_the_hub_down_keeps_the_join_and_resumes(human, served, stub, monkeypatch):
    from tests.host.dispatch_helpers import ok

    human.run("hub.join", join_args())
    monkeypatch.setattr(hubops, "TRANSPORT", None)  # no link
    assert err(served.client().req("hub.connect", connect_args(), idem=True)) == "hub.unreachable"
    assert ok(served.client().req("hub.status", {}, idem=False))["data"]["link"] == "joining"
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: stub)
    got = ok(served.client().req("hub.connect", connect_args(), idem=True))["data"]
    assert got["status"] == "bound" and hub_state(served)[0]["binding_seq"] == 1  # no second hub.joined


def test_connect_refuses_another_hub_than_the_one_on_the_log(human, served, stub):
    human.run("hub.join", join_args())
    card = copy.deepcopy(V["objects"]["card"])
    card["o"]["hk_root_pub"] = V["keys"]["hk_root_rogue"]["pub"]
    assert err(served.client().req("hub.connect", connect_args(hub_card=card), idem=True)) == "hub.refused"
    assert stub.sent == []


def test_connect_needs_a_directory_card(human, served, monkeypatch):
    monkeypatch.setattr(hubops, "CARD_SOURCE", lambda service: None)
    human.run("hub.join", join_args())
    assert err(served.client().req("hub.connect", connect_args(), idem=True)) == "hub.no_card"


def test_leave_then_unbind_sends_the_signed_unbound(human, served, stub):
    from tests.host.dispatch_helpers import ok

    human.run("hub.join", join_args())
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert err(served.client().req("hub.unbind", {}, idem=True)) == "hub.not_joined"  # not left on the log yet
    human.run("hub.leave", {})
    got = ok(served.client().req("hub.unbind", {}, idem=True))["data"]
    assert got["unbound"] and got["delivered"] and stub.ops() == ["join", "binding_put"]
    u = stub.sent[1]["o"]["body"]["object"]["o"]
    assert u["kind"] == "hub.unbound" and u["binding_seq"] == 1 and u["hub_id"] == HUB_ID and u["dev"] is True
    assert ok(served.client().req("hub.status", {}, idem=False))["data"]["link"] == "unbound"


def test_leave_works_with_the_hub_down_and_the_unbound_is_delivered_later(human, served, stub, monkeypatch):
    from tests.host.dispatch_helpers import ok

    human.run("hub.join", join_args())
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    monkeypatch.setattr(hubops, "TRANSPORT", None)
    human.run("hub.leave", {})  # the owner's record never needs the hub
    got = ok(served.client().req("hub.unbind", {}, idem=True))["data"]
    assert got["unbound"] and not got["delivered"]
    assert ok(served.client().req("hub.status", {}, idem=False))["data"]["unbound_pending"] is True
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: stub)
    got = ok(served.client().req("hub.unbind", {}, idem=True))["data"]
    assert got["delivered"] and stub.ops() == ["join", "binding_put"]
    assert ok(served.client().req("hub.status", {}, idem=False))["data"]["unbound_pending"] is False


def test_a_leave_before_any_connect_has_nothing_to_tell_the_hub(human, served, stub):
    from tests.host.dispatch_helpers import ok

    human.run("hub.join", join_args())
    human.run("hub.leave", {})
    got = ok(served.client().req("hub.unbind", {}, idem=True))["data"]
    assert got["unbound"] is False and stub.sent == []


def test_a_convenience_key_never_signs_a_hub_op(human, served):
    from orch.host import convenience

    for op in ("hub.join", "hub.leave"):
        with pytest.raises(Exception) as e:
            convenience.refuse_hub_op(op, "convenience")
        assert getattr(e.value, "code", "") == "sign.convenience_unavailable"
        convenience.refuse_hub_op(op, "passphrase")  # the owner's key at the terminal
    convenience.refuse_hub_op("settings.set", "convenience")  # other ops keep their own rules
    body = {"op": "hub.join", "args": join_args(), "signer": {**human.signer, "auth": "convenience"}}
    assert err(human.c.req("human.prepare", body)) == "sign.convenience_unavailable"
    assert hub_state(served)[0] == {}


def test_same_hub_rejoin_delivers_the_owed_unbound_first_then_binds(human, served, stub, monkeypatch):
    from tests.host.dispatch_helpers import ok

    c = served.client()
    human.run("hub.join", join_args())
    ok(c.req("hub.connect", connect_args(), idem=True))
    monkeypatch.setattr(hubops, "TRANSPORT", None)
    human.run("hub.leave", {})
    assert not ok(c.req("hub.unbind", {}, idem=True))["data"]["delivered"]
    human.run("hub.join", join_args())  # binding 2
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: stub)
    ok(c.req("hub.connect", connect_args(), idem=True))
    assert stub.ops() == ["join", "binding_put", "join"]
    assert stub.sent[1]["o"]["body"]["object"]["o"]["binding_seq"] == 1
    assert stub.sent[2]["o"]["body"]["binding"]["o"]["binding_seq"] == 2
    assert ok(c.req("hub.status", {}, idem=False))["data"]["owed"] == []


def test_unbind_flushes_an_owed_unbound_even_when_the_log_says_joined_again(human, served, stub, monkeypatch):
    from tests.host.dispatch_helpers import ok

    c = served.client()
    human.run("hub.join", join_args())
    ok(c.req("hub.connect", connect_args(), idem=True))
    monkeypatch.setattr(hubops, "TRANSPORT", None)
    human.run("hub.leave", {})
    ok(c.req("hub.unbind", {}, idem=True))
    assert len(ok(c.req("hub.status", {}, idem=False))["data"]["owed"]) == 1
    human.run("hub.join", join_args())
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: stub)
    assert ok(c.req("hub.unbind", {}, idem=True))["data"]["delivered"]
    assert stub.ops() == ["join", "binding_put"]


def test_a_crash_between_leave_and_unbind_still_lets_the_rejoin_through(human, served, stub):
    from tests.host.dispatch_helpers import ok

    c = served.client()
    human.run("hub.join", join_args())
    ok(c.req("hub.connect", connect_args(), idem=True))
    human.run("hub.leave", {})  # the host never ran hub.unbind
    human.run("hub.join", join_args())
    ok(c.req("hub.connect", connect_args(), idem=True))
    assert stub.ops() == ["join", "binding_put", "join"]
    assert stub.sent[1]["o"]["body"]["object"]["o"]["binding_seq"] == 1


# --- C2b: the full join through the host ------------------------------------------------------------------------------


@pytest.fixture
def keystore(monkeypatch, served, tmp_path):
    """A membership key store for the served workspace with epoch 1 committed, standing in for the relay's."""
    from orch.identity.epochs import MembershipKeys, R6Gate
    from tests.wsproto import r6kit

    mk = MembershipKeys(tmp_path / "membership", served.wid, now_ms=lambda: 1_790_000_000_000)
    R6Gate(mk).install(lambda: r6kit.KitStore())
    r6kit.rotate(mk)
    monkeypatch.setattr(
        hubops,
        "epochs_for",
        lambda service, create=False: hubops._Epochs(mk, None, lambda: hubops._sealed_epoch(service)),
    )
    yield mk
    mk.close()


def _unwrap(msg, wid, epoch):
    from orch import crypto
    from tests.hub.vec import private_key

    body = msg["o"]["body"]
    assert set(body) == {"epoch", "wrap"} and body["epoch"] == epoch
    return crypto.open_sealed(
        private_key("hxk"), bytes.fromhex(HUB_ID), "wk", bytes.fromhex(wid), epoch, crypto.unb64u(body["wrap"])
    )


def test_a_full_join_through_the_host_seals_wk_to_the_hub_and_shows_the_member(human, served, stub, keystore):
    from tests.host.dispatch_helpers import ok

    human.run("hub.join", join_args(mode="full"))
    got = ok(served.client().req("hub.connect", connect_args(), idem=True))["data"]
    assert got["mode"] == "full" and got["status"] == "bound" and got["sealed_epoch"] == 1
    assert stub.ops() == ["join", "wk_seal"]
    assert stub.sent[0]["o"]["body"]["binding"]["o"]["mode"] == "full"
    assert _unwrap(stub.sent[1], served.wid, 1) == keystore.wk(1)
    status = ok(served.client().req("hub.status", {}, idem=False))["data"]
    assert status["mode"] == "full" and status["member"] is True and status["sealed_epoch"] == 1
    assert status["rotation"] == "none"
    assert hub_state(served)[0]["member"] == {"kind": "hub", "hub_id": HUB_ID}


def test_a_full_join_without_an_epoch_key_sends_nothing(human, served, stub, monkeypatch):
    monkeypatch.setattr(hubops, "epochs_for", lambda service, create=False: None)
    human.run("hub.join", join_args(mode="full"))
    assert err(served.client().req("hub.connect", connect_args(), idem=True)) == "hub.no_epoch"
    assert stub.sent == []


def test_selective_through_the_host_never_seals_even_with_epoch_keys(human, served, stub, keystore):
    from tests.host.dispatch_helpers import ok

    human.run("hub.join", join_args())
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    human.run("hub.leave", {})
    ok(served.client().req("hub.unbind", {}, idem=True))
    assert stub.ops() == ["join", "binding_put"] and "wk_seal" not in stub.ops()
    assert "member" not in hub_state(served)[0]
    assert not keystore.gate_up()  # a selective leave rotates nothing


def test_the_mode_switch_through_the_host_needs_no_invite_and_seals(human, served, stub, keystore):
    from tests.host.dispatch_helpers import ok

    c = served.client()
    human.run("hub.join", join_args())
    ok(c.req("hub.connect", connect_args(), idem=True))
    human.run("hub.mode", {"hub_card": copy.deepcopy(CARD), "mode": "full"})
    got = ok(c.req("hub.connect", {"hub_card": copy.deepcopy(CARD)}, idem=True))["data"]
    assert got["mode"] == "full" and got["binding_seq"] == 2 and got["sealed_epoch"] == 1
    assert stub.ops() == ["join", "binding_put", "wk_seal"]
    assert stub.sent[1]["o"]["body"]["object"]["o"]["mode"] == "full"
    ok(c.req("hub.connect", {"hub_card": copy.deepcopy(CARD)}, idem=True))  # a repeat is a no-op re-seal check
    assert stub.ops() == ["join", "binding_put", "wk_seal"]
    assert hub_state(served)[0]["member"]["kind"] == "hub"


def test_a_connect_without_an_invite_never_starts_a_join(human, served, stub, keystore):
    human.run("hub.join", join_args(mode="full"))
    assert err(served.client().req("hub.connect", {"hub_card": copy.deepcopy(CARD)}, idem=True)) == "invalid.input"
    assert stub.sent == []


def test_leave_from_full_through_the_host_requests_the_rotation_and_keeps_it_owed(human, served, stub, keystore):
    from tests.host.dispatch_helpers import ok
    from tests.wsproto import r6kit

    c = served.client()
    human.run("hub.join", join_args(mode="full"))
    ok(c.req("hub.connect", connect_args(), idem=True))
    human.run("hub.leave", {})
    assert "member" not in hub_state(served)[0]
    got = ok(c.req("hub.unbind", {}, idem=True))["data"]
    assert got["binding_ended"] and got["rotation"] == "pending" and keystore.gate_up()
    assert got["unbound"] is False  # not finished while the key the hub must never get is not replaced
    assert ok(c.req("hub.status", {}, idem=False))["data"]["rotation"] == "pending"
    n = len(stub.sent)
    human.run("hub.join", join_args(mode="full"))  # the owner may sign a new full join at once ...
    assert err(c.req("hub.connect", connect_args(invite="invite-2"), idem=True)) == "hub.rotation_pending"
    assert len(stub.sent) == n  # ... but nothing is sent, and no key, until the new epoch exists
    human.run("hub.leave", {})
    r6kit.rotate(keystore)  # what the membership machinery does with the raised gate
    assert ok(c.req("hub.status", {}, idem=False))["data"]["rotation"] == "none"
    got = ok(c.req("hub.unbind", {}, idem=True))["data"]
    assert got["rotation"] == "none"
    sealed = [m for m in stub.sent if m["o"]["op"] == "wk_seal"]
    assert len(sealed) == 1 and _unwrap(sealed[0], served.wid, 1) == keystore.wk(1)  # the new key is never sent
    assert keystore.wk(2) != keystore.wk(1)


def test_a_full_rejoin_over_an_unended_binding_still_owes_the_rotation(human, served, stub, keystore):
    """The leave is logged but `hub.unbind` never ran: the next connect must not re-seal the SAME epoch."""
    from tests.host.dispatch_helpers import ok
    from tests.wsproto import r6kit

    c = served.client()
    human.run("hub.join", join_args(mode="full"))
    ok(c.req("hub.connect", connect_args(), idem=True))
    human.run("hub.leave", {})  # no hub.unbind
    human.run("hub.join", join_args(mode="full"))  # binding 2
    n = len(stub.sent)
    assert err(c.req("hub.connect", connect_args(invite="invite-2"), idem=True)) == "hub.rotation_pending"
    assert len(stub.sent) == n and keystore.gate_up()  # nothing sent; the retire asked for the rotation
    r6kit.rotate(keystore)
    ok(c.req("hub.connect", connect_args(invite="invite-2"), idem=True))
    sealed = [m for m in stub.sent if m["o"]["op"] == "wk_seal"]
    assert [m["o"]["body"]["epoch"] for m in sealed] == [1, 2]
    assert _unwrap(sealed[1], served.wid, 2) == keystore.wk(2)


def test_a_selective_rejoin_over_an_unended_full_binding_owes_the_rotation(human, served, stub, keystore):
    from tests.host.dispatch_helpers import ok
    from tests.wsproto import r6kit

    c = served.client()
    human.run("hub.join", join_args(mode="full"))
    ok(c.req("hub.connect", connect_args(), idem=True))
    human.run("hub.leave", {})  # no hub.unbind
    human.run("hub.join", join_args())  # full -> selective
    ok(c.req("hub.connect", connect_args(invite="invite-2"), idem=True))
    status = ok(c.req("hub.status", {}, idem=False))["data"]
    assert status["mode"] == "selective" and status["rotation"] == "pending"
    assert [m["o"]["op"] for m in stub.sent].count("wk_seal") == 1  # nothing new was sealed
    r6kit.rotate(keystore)
    assert ok(c.req("hub.status", {}, idem=False))["data"]["rotation"] == "none"


def test_status_warns_when_a_routine_rotation_was_not_resealed(human, served, stub, keystore):
    from tests.host.dispatch_helpers import ok
    from tests.wsproto import r6kit

    c = served.client()
    human.run("hub.join", join_args(mode="full"))
    ok(c.req("hub.connect", connect_args(), idem=True))
    assert ok(c.req("hub.status", {}, idem=False))["data"]["reseal_needed"] is False
    r6kit.rotate(keystore)  # a routine rotation while the hub is bound
    assert ok(c.req("hub.status", {}, idem=False))["data"]["reseal_needed"] is True
    ok(c.req("hub.connect", {"hub_card": copy.deepcopy(CARD)}, idem=True))
    assert ok(c.req("hub.status", {}, idem=False))["data"]["reseal_needed"] is False
    assert _unwrap([m for m in stub.sent if m["o"]["op"] == "wk_seal"][1], served.wid, 2) == keystore.wk(2)


def test_epochs_for_sees_a_relay_linked_after_the_first_open(served, tmp_path, monkeypatch):
    from orch.host import relayr6

    linked = {"v": False}
    monkeypatch.setattr(relayr6, "_previously_linked", lambda service: linked["v"])
    monkeypatch.setattr(relayr6, "membership_path", lambda service: tmp_path / "membership")
    from orch.crypto.encoding import unb64u
    from orch.host import relayr3

    monkeypatch.setattr(relayr3, "owner_pk_pub", lambda service: unb64u(V["keys"]["pk_alice"]["pub"], 65))
    svc = served.service
    first = hubops.epochs_for(svc, create=True)
    assert first is not None and first._local_owner is not None  # relay-less: local epochs allowed
    linked["v"] = True
    second = hubops.epochs_for(svc, create=True)
    assert second is not None and second._local_owner is None  # now the relay path's; never a local epoch
    relayr6.close(svc)


def test_linking_a_relay_is_refused_for_a_workspace_with_local_epochs(served):
    from orch.host import relaylink
    from orch.host.errors import HostError

    svc = served.service
    mark = relaylink.local_epochs_path(svc.state_dir, svc.workspace_id)
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text("{}")
    with pytest.raises(HostError) as e:
        with relaylink.link_reservation(svc):
            pass
    assert e.value.code == "relay.local_epochs" and "H5" in str(e.value)
