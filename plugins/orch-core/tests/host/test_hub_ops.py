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

    for op in ("hub.join", "hub.leave"):
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


def test_full_mode_is_refused_with_its_own_code(human, served):
    assert err(human.prepare_res("hub.join", join_args(mode="full"))) == "hub.full_mode_later"
    assert hub_state(served)[0] == {}


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


def test_only_the_owner_may_be_aliased(human):
    other = {"person_id": V["ids"]["person_alice"], "user_id": V["ids"]["user_alice"]}
    assert err(human.prepare_res("hub.join", join_args(aliases=[other]))) == "hub.refused"
    me = {"person_id": human.person.ref.removeprefix("p_"), "user_id": V["ids"]["user_alice"]}
    _, _, signed = human.run("hub.join", join_args(aliases=[me]))
    assert signed.events[0]["aliases"] == [me]


def test_agents_are_refused(human, served):
    c = served.client()
    for op, args in (("hub.join", join_args()), ("hub.leave", {})):
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
    for bad in (
        {**JOINED, "mode": "full"},
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

    def send(self, message):
        from tests.hub.fake import sign_with
        from tests.hub.vec import private_key

        self.sent.append(copy.deepcopy(message))
        m = message["o"]
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


def test_rejoin_after_a_leave_with_the_hub_down_delivers_the_old_unbound_first(human, served, stub, monkeypatch):
    """B1: join, leave with the hub down, rejoin (higher seq): the owed unbound goes to the old hub first, or the
    rejoin is refused; it is never lost."""
    from tests.host.dispatch_helpers import ok

    c = served.client()
    human.run("hub.join", join_args())
    ok(c.req("hub.connect", connect_args(), idem=True))
    monkeypatch.setattr(hubops, "TRANSPORT", None)
    human.run("hub.leave", {})
    assert not ok(c.req("hub.unbind", {}, idem=True))["data"]["delivered"]
    human.run("hub.join", join_args())  # binding 2
    assert err(c.req("hub.connect", connect_args(), idem=True)) == "hub.unbind_pending"
    assert stub.ops() == ["join"]  # no second join went out
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: stub)
    ok(c.req("hub.connect", connect_args(), idem=True))
    assert stub.ops() == ["join", "binding_put", "join"]
    assert stub.sent[1]["o"]["body"]["object"]["o"]["binding_seq"] == 1
    assert stub.sent[2]["o"]["body"]["binding"]["o"]["binding_seq"] == 2


def test_unbind_flushes_an_owed_unbound_even_when_the_log_says_joined_again(human, served, stub, monkeypatch):
    from tests.host.dispatch_helpers import ok

    c = served.client()
    human.run("hub.join", join_args())
    ok(c.req("hub.connect", connect_args(), idem=True))
    monkeypatch.setattr(hubops, "TRANSPORT", None)
    human.run("hub.leave", {})
    ok(c.req("hub.unbind", {}, idem=True))
    human.run("hub.join", join_args())
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: stub)
    assert ok(c.req("hub.unbind", {}, idem=True))["data"]["delivered"]
    assert stub.ops() == ["join", "binding_put"]


def test_a_crash_between_leave_and_unbind_says_to_finish_the_leave(human, served, stub):
    from tests.host.dispatch_helpers import ok

    c = served.client()
    human.run("hub.join", join_args())
    ok(c.req("hub.connect", connect_args(), idem=True))
    human.run("hub.leave", {})  # the host never ran hub.unbind
    human.run("hub.join", join_args())
    assert err(c.req("hub.connect", connect_args(), idem=True)) == "hub.unbind_pending"
