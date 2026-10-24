"""C2a step 1: core's hub client (``orch.hubclient``) joins and leaves selectively against the in-process fake hub."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from orch.crypto import suite as S
from orch.crypto.encoding import unb64u
from orch.hubclient import HubRefused, HubState, flush_outbox, join_selective, leave, wire

from . import contract as H
from .fake import FakeHub
from .vec import V, private_key, pub

WS = V["ids"]["workspace_a"]
NOW = V["now_ms"]
WSK = private_key("wsk_a")


def sign_wsk(b: bytes) -> bytes:
    return S.sign(WSK, b)


class Wire:
    """An in-process HubTransport on the fake hub; ``down`` makes it unreachable."""

    def __init__(self, hub: FakeHub) -> None:
        self.hub, self.down, self.sent = hub, False, []

    refuse_ops: set[str] = set()

    def send(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.down or message["o"]["op"] in self.refuse_ops:
            raise ConnectionRefusedError
        self.sent.append(message)
        return self.hub.send(message)


class Host:
    """The injected pieces of a host: log recorders and a state store."""

    def __init__(self, hub: FakeHub, **over: Any) -> None:
        self.hub, self.wire, self.state, self.log = hub, Wire(hub), None, []
        self.routes: dict[str, Any] = {}  # hub_id -> the transport of THAT hub (default: the one wire)
        self.kw = dict(
            invite="invite-1", hub_card=V["objects"]["card"], v2_card=V["objects"]["v2_card_a"],
            pk_pub=pub("pk_alice"), workspace_id=WS, wsk_pub=pub("wsk_a"), sign_wsk=sign_wsk, dev_mode=True,
            now_ms=lambda: NOW, aliases=[{"person_id": V["ids"]["person_alice"], "user_id": V["ids"]["user_alice"]}],
        )  # fmt: skip
        self.kw.update(over)

    def _save(self, s: HubState) -> None:
        self.state = HubState.from_json(copy.deepcopy(s.to_json()))

    def join(self, **over: Any):
        kw = {**self.kw, **over}
        r = join_selective(
            state=self.state, transport=self.wire, record_joined=lambda p: self.log.append(("hub.joined", p)),
            save=self._save, old_transport=self.tf, **kw,
        )  # fmt: skip
        return r

    def tf(self, hub_id: str, entry: dict) -> Any:
        return self.routes.get(hub_id, self.wire)

    def leave(self):
        return leave(
            state=self.state, workspace_id=WS, sign_wsk=sign_wsk, dev_mode=True, now_ms=lambda: NOW,
            transport_for=self.tf, record_left=lambda p: self.log.append(("hub.left", p)), save=self._save,
        )  # fmt: skip


@pytest.fixture
def host(hub: FakeHub) -> Host:
    return Host(hub)


def code(fn) -> str:
    with pytest.raises(HubRefused) as e:
        fn()
    return e.value.code


def test_join_selective(host: Host, hub: FakeHub) -> None:
    r = host.join()
    assert [t for t, _ in host.log] == ["hub.joined"]
    p = host.log[0][1]
    assert set(p) == {"hub_id", "hk_root_pub", "mode", "binding_seq", "aliases"}
    assert p["mode"] == "selective" and p["binding_seq"] == 1 and p["hub_id"] == hub.hub_id
    assert r.state.status == "bound" and host.state.status == "bound"
    ws = hub.workspaces[WS]
    assert ws.mode == "selective" and ws.binding["status"] == "bound"
    assert hub.published_cards[WS] == V["objects"]["v2_card_a"]
    assert [c for c in hub.calls if c[0] == WS] == [(WS, "join", "ok")]


def test_hub_never_sees_wk_material(host: Host, hub: FakeHub) -> None:
    host.join()
    ws = hub.workspaces[WS]
    assert ws.wk_seals == [] and all(op != "wk_seal" for _, op, _ in hub.calls)
    # nothing the hub received is a wrap, a member list or a key: only the join body members
    for m in host.wire.sent:
        assert m["o"]["op"] == "join" and set(m["o"]["body"]) == {"invite", "card", "pk_pub", "binding"}


def test_wk_seal_refused_in_selective(host: Host, hub: FakeHub) -> None:
    from orch.hubclient.link import HubLink

    host.join()
    link = HubLink(host.wire, hub_id=hub.hub_id, workspace_id=WS, hk_op_pub=hub.hk_op_pub, sign=sign_wsk,
                   mode="selective", now_ms=lambda: NOW)  # fmt: skip
    n = len(host.wire.sent)
    assert code(lambda: link.call("wk_seal", {"epoch": 1, "wrap": "AA"})) == "wrong_mode"
    assert code(lambda: link.call("nope", {})) == "unknown_op"
    assert len(host.wire.sent) == n  # refused before anything was sent


def test_full_mode_refused(host: Host) -> None:
    assert code(lambda: host.join(mode="full")) == "full_mode_later"
    assert host.log == [] and host.wire.sent == []


def test_dev_mismatch_refused(host: Host) -> None:
    assert code(lambda: host.join(dev_mode=False)) == "dev_refused"  # a real workspace refuses a dev hub
    assert host.log == [] and host.wire.sent == []


def test_real_hub_card_refused_by_dev_workspace(host: Host) -> None:
    card = copy.deepcopy(V["objects"]["card"])
    card["o"]["dev"] = False  # also breaks the signature, but the dev rule must hold on its own
    with pytest.raises(HubRefused) as e:
        wire.check_dev(False, True)
    assert e.value.code == "dev_mismatch"
    assert code(lambda: host.join(hub_card=card)) in ("bad_signature", "dev_mismatch")
    assert host.log == []


@pytest.mark.parametrize(
    "mut,want",
    [
        (lambda c: c["o"].update(hub_id="0" * 32), "hub_id_mismatch"),
        (lambda c: c["o"].update(card_seq=2), "bad_signature"),
        (lambda c: c["o"].update(extra=1), "bad_signature"),
        (lambda c: c.update(sig="AAAA"), "malformed"),
        (lambda c: c.pop("op_cert"), "malformed"),
        (lambda c: c["op_cert"]["o"].update(expires_ms=1), "bad_signature"),
        (lambda c: c["o"].update(v=3), "malformed"),
        (lambda c: c["o"].update(suite=1), "wrong_suite"),
    ],
)
def test_bad_hub_card_refused(host: Host, mut, want: str) -> None:
    card = copy.deepcopy(V["objects"]["card"])
    mut(card)
    assert code(lambda: host.join(hub_card=card)) == want
    assert host.log == [] and host.wire.sent == [] and host.state is None


def test_expired_card_refused(host: Host) -> None:
    assert code(lambda: host.join(now_ms=lambda: NOW + 10**13)) == "cert_expired"


def test_hub_refusal_leaves_a_pending_join_that_can_resume(host: Host, hub: FakeHub) -> None:
    hub.invites = {}
    assert code(lambda: host.join()) == "bad_invite"
    assert host.state.status == "joining" and len(host.log) == 1
    hub.invites = {"invite-1": None}
    r = host.join()  # resumes: no second hub.joined, same binding
    assert len(host.log) == 1 and r.payload == {} and host.state.status == "bound"


def test_cannot_join_twice(host: Host) -> None:
    host.join()
    assert code(lambda: host.join()) == "already_joined"
    assert len(host.log) == 1


def test_alias_never_repointed(host: Host) -> None:
    host.join()
    host.leave()
    other = [{"person_id": V["ids"]["person_alice"], "user_id": V["ids"]["user_bob"]}]
    assert code(lambda: host.join(aliases=other)) == "alias_repointed"


def test_leave(host: Host, hub: FakeHub) -> None:
    host.join()
    r = host.leave()
    assert r.delivered and r.payload == {"hub_id": hub.hub_id, "binding_seq": 1}
    assert [t for t, _ in host.log] == ["hub.joined", "hub.left"]
    assert host.state.status == "unbound" and host.state.outbox == {}
    assert hub.workspaces[WS].binding["status"] == "unbound"
    u = r.unbound["o"]
    assert u["kind"] == "hub.unbound" and u["binding_seq"] == 1 and u["dev"] is True
    H.verify_unbound(r.unbound, workspace_id=WS, wsk_pub=pub("wsk_a"), dev_mode=True)


def test_no_resurrect_after_leave(host: Host, hub: FakeHub, link) -> None:
    host.join()
    old = host.state.binding
    host.leave()
    # the old binding (and any at or below the leave) is refused by the hub store
    from .fake import HubError

    with pytest.raises(HubError) as e:
        link.seq = 100
        link.call("binding_put", {"object": old})
    assert e.value.code == "unbound"
    # a rejoin needs a higher binding_seq
    r = host.join()
    assert host.state.binding_seq == 2 and r.payload["binding_seq"] == 2 and host.state.status == "bound"


def test_leave_with_hub_down(host: Host, hub: FakeHub) -> None:
    host.join()
    host.wire.down = True
    r = host.leave()
    assert not r.delivered and host.state.status == "unbound" and list(host.state.outbox) == [hub.hub_id]
    assert [t for t, _ in host.log] == ["hub.joined", "hub.left"]
    assert hub.workspaces[WS].binding["status"] == "bound"  # the hub has not heard yet
    host.state = HubState.from_json(host.state.to_json())  # the outbox survives a save/load
    host.wire.down = False
    assert flush_outbox(state=host.state, workspace_id=WS, sign_wsk=sign_wsk, now_ms=lambda: NOW,
                        transport_for=host.tf, save=host._save) == [hub.hub_id]  # fmt: skip
    assert host.state.outbox == {} and hub.workspaces[WS].binding["status"] == "unbound"


def test_leave_without_join_refused(host: Host) -> None:
    assert code(host.leave) == "not_joined"
    assert host.log == []


def test_leave_records_before_anything_is_sent(host: Host) -> None:
    host.join()
    n = len(host.wire.sent)

    def boom(_p):
        raise RuntimeError("owner declined")

    with pytest.raises(RuntimeError):
        leave(state=host.state, workspace_id=WS, sign_wsk=sign_wsk, dev_mode=True, now_ms=lambda: NOW,
              transport_for=host.tf, record_left=boom, save=host._save)  # fmt: skip
    assert len(host.wire.sent) == n and host.state.status == "bound"


def test_declined_join_sends_nothing(host: Host) -> None:
    def boom(_p):
        raise RuntimeError("owner declined")

    with pytest.raises(RuntimeError):
        join_selective(
            state=None, transport=host.wire, record_joined=boom, save=host._save, old_transport=host.tf, **host.kw
        )
    assert host.wire.sent == [] and host.state is None


def test_state_from_json_strict(host: Host) -> None:
    host.join()
    d = host.state.to_json()
    for bad in ({**d, "extra": 1}, {**d, "status": "x"}, {**d, "hub_id": "0" * 32}, {**d, "binding_seq": 0}):
        assert code(lambda bad=bad: HubState.from_json(bad)) == "malformed"
    assert code(lambda: HubState.from_json([])) == "malformed"


def test_payload_vectors() -> None:
    p = wire.joined_payload(
        hk_root_pub=pub("hk_root"), mode="selective", binding_seq=1,
        aliases=[{"person_id": V["ids"]["person_alice"], "user_id": V["ids"]["user_alice"]}],
    )  # fmt: skip
    assert p == {k: V["hub_joined"]["event"][k] for k in p}
    assert wire.left_payload(hub_id_=V["ids"]["hub"], binding_seq=1) == {"hub_id": V["ids"]["hub"], "binding_seq": 1}


def test_built_objects_match_vectors() -> None:
    b = wire.build_binding(workspace_id=WS, wsk_pub=pub("wsk_a"), hk_root_pub=pub("hk_root"), mode="selective",
                           binding_seq=1, valid_from_ms=V["objects"]["binding_a_1"]["o"]["valid_from_ms"],
                           dev=True, sign=sign_wsk)  # fmt: skip
    assert b["o"] == V["objects"]["binding_a_1"]["o"]
    assert S.verify(pub("wsk_a"), unb64u(b["sig"], 64), H.signing_bytes(H.L["sig_hub_binding"], b["o"]))
    u = wire.build_unbound(
        workspace_id=WS,
        hub_id_=V["ids"]["hub"],
        binding_seq=1,
        unbound_ms=V["objects"]["unbound_a_1"]["o"]["unbound_ms"],
        dev=True,
        sign=sign_wsk,
    )
    assert u["o"] == V["objects"]["unbound_a_1"]["o"]


def test_the_wire_labels_are_the_custody_labels_of_the_workspace_key():
    from orch.crypto.labels import L
    from orch.custody.base import HOST_LABELS, PERSON_LABELS

    for const, key in (
        (wire.SIG_BINDING, "sig_hub_binding"),
        (wire.SIG_UNBOUND, "sig_hub_unbound"),
        (wire.SIG_LINK, "sig_hub_link"),
    ):
        assert const == L[key] and const in HOST_LABELS and const not in PERSON_LABELS  # WSK only, never a person key


def test_rejoin_with_the_old_hub_dead_proceeds_and_the_unbound_stays_owed(host: Host, hub: FakeHub) -> None:
    host.join()
    host.wire.down = True
    host.leave()
    host.wire.down = False
    host.wire.refuse_ops = {"binding_put"}  # the old hub is dead for the unbound only (same hub, so same wire)
    host.join(binding_seq=2)  # the dead old hub (the same one, down) does not veto the rejoin
    assert host.state.status == "bound" and host.state.binding_seq == 2
    assert list(host.state.outbox) == [hub.hub_id]  # still owed, kept across the new binding


def test_same_hub_rejoin_delivers_first_then_binds(host: Host, hub: FakeHub) -> None:
    host.join()
    host.wire.down = True
    host.leave()
    host.wire.down = False
    host.join(binding_seq=2)
    assert host.state.outbox == {} and host.state.binding_seq == 2
    assert [c[1] for c in hub.calls if c[0] == WS] == ["join", "binding_put", "join"]


class Dead:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, message):
        self.sent.append(message)
        raise ConnectionRefusedError


def test_an_unbound_owed_to_another_hub_never_reaches_the_new_hub(host: Host, hub: FakeHub) -> None:
    host.join()
    host.leave()
    # pretend the first hub was another one, owed an unbound and down
    old_id = "ab" * 16
    host.state.outbox = {old_id: {**_owed(host), "hk_op_pub": host.state.hk_op_pub}}
    dead = Dead()
    host.routes[old_id] = dead
    host.join(binding_seq=2)
    assert host.state.status == "bound" and old_id in host.state.outbox  # the join went on; the entry stays
    assert len(dead.sent) == 1 and dead.sent[0]["o"]["op"] == "binding_put"  # one try, over its own transport
    assert [c[1] for c in hub.calls if c[0] == WS].count("binding_put") == 1  # only the leave's own, delivered then


def _owed(host: Host) -> dict:
    u = wire.build_unbound(workspace_id=WS, hub_id_="ab" * 16, binding_seq=1, unbound_ms=NOW, dev=True, sign=sign_wsk)
    return {"unbound": u, "addr": None, "tls_spki": None, "mode": "selective"}


def test_an_old_hub_that_refuses_keeps_its_entry_and_the_join_proceeds(host: Host, hub: FakeHub) -> None:
    host.join()
    host.leave()
    old_id = "cd" * 16
    host.state.outbox = {old_id: {**_owed(host), "hk_op_pub": host.state.hk_op_pub}}
    host.routes[old_id] = Wire(FakeHub())  # a hub that does not know the workspace: it refuses
    host.join(binding_seq=2)
    assert host.state.status == "bound" and old_id in host.state.outbox


def card_of_another_hub() -> dict:
    from orch.canon.jcs import dumps as cj

    root, op = S.generate_private_key(), S.generate_private_key()
    root_pub, op_pub = S.public_bytes(root), S.public_bytes(op)
    hid = wire.hub_id(root_pub)
    from orch.crypto.encoding import b64u

    opo = {"v": 2, "suite": 2, "kind": "hub.op_cert", "hv": 0, "hub_id": hid, "cert_id": "11" * 16, "op_seq": 1,
           "hk_op_pub": b64u(op_pub), "issued_ms": NOW - 1000, "expires_ms": NOW + 10**9, "dev": True}  # fmt: skip
    o = {"v": 2, "suite": 2, "kind": "hub.card", "hv": 0, "hub_id": hid, "hk_root_pub": b64u(root_pub),
         "hxk_pub": b64u(op_pub), "relay_url": None, "tls_spki_sha256": b64u(b"\x02" * 32), "card_seq": 1, "reader": 1,
         "issued_ms": NOW - 1000, "dev": True}  # fmt: skip
    return {"o": o, "op_cert": {"o": opo, "sig": b64u(S.sign(root, wire.SIG_OP_CERT + cj(opo)))},
            "sig": b64u(S.sign(op, wire.SIG_CARD + cj(o)))}  # fmt: skip


def test_state_bound_to_hub_a_then_the_log_joins_hub_b_without_an_unbind(host: Host, hub: FakeHub) -> None:
    """Left on the log, never unbound, then joined with ANOTHER hub's card: A's unbound is owed first and B's pin is
    not taken from A's state (no other_hub); B being down is only unreachable."""
    host.join()
    a_id = host.state.hub_id
    b_card = card_of_another_hub()
    dead = Dead()
    host.routes[a_id] = Wire(hub)
    host.wire = dead  # the new hub B is down
    with pytest.raises(HubRefused) as e:
        host.join(hub_card=b_card, binding_seq=2)
    assert e.value.code == "hub_unreachable"
    assert host.state.hub_id == b_card["o"]["hub_id"] and host.state.status == "joining"
    assert hub.workspaces[WS].binding["status"] == "unbound"  # A heard its unbound, over A's own transport
    assert dead.sent and all(m["o"]["op"] == "join" for m in dead.sent)  # B saw only its join


def test_a_stale_lower_seq_entry_is_sent_once_and_never_resurrects(host: Host, hub: FakeHub) -> None:
    host.join()
    host.leave()
    host.join(binding_seq=3)
    host.state.outbox = {hub.hub_id: {**_owed(host), "hk_op_pub": host.state.hk_op_pub}}
    host.state.outbox[hub.hub_id]["unbound"] = wire.build_unbound(
        workspace_id=WS, hub_id_=hub.hub_id, binding_seq=1, unbound_ms=NOW, dev=True, sign=sign_wsk
    )
    done = flush_outbox(state=host.state, workspace_id=WS, sign_wsk=sign_wsk, now_ms=lambda: NOW,
                        transport_for=host.tf, save=host._save)  # fmt: skip
    assert done == [hub.hub_id] and host.state.outbox == {}
    assert hub.workspaces[WS].binding["status"] == "bound"  # a lower unbound cannot end binding 3
