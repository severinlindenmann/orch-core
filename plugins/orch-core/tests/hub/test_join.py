"""C2a step 1: core's hub client (``orch.hub``) joins and leaves selectively against the in-process fake hub."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from orch.crypto import suite as S
from orch.crypto.encoding import unb64u
from orch.hub import HubRefused, HubState, flush_unbound, join_selective, leave, wire

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

    def send(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.down:
            raise ConnectionRefusedError
        self.sent.append(message)
        return self.hub.send(message)


class Host:
    """The injected pieces of a host: log recorders and a state store."""

    def __init__(self, hub: FakeHub, **over: Any) -> None:
        self.hub, self.wire, self.state, self.log = hub, Wire(hub), None, []
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
            save=self._save, **kw,
        )  # fmt: skip
        return r

    def leave(self):
        return leave(
            state=self.state, workspace_id=WS, sign_wsk=sign_wsk, dev_mode=True, now_ms=lambda: NOW,
            transport=self.wire, record_left=lambda p: self.log.append(("hub.left", p)), save=self._save,
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
    from orch.hub.link import HubLink

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
    assert host.state.status == "unbound" and host.state.pending_unbound is None
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
    assert not r.delivered and host.state.status == "unbound" and host.state.pending_unbound is not None
    assert [t for t, _ in host.log] == ["hub.joined", "hub.left"]
    assert hub.workspaces[WS].binding["status"] == "bound"  # the hub has not heard yet
    # the queue survives a save/load and is delivered when the hub is back
    host.state = HubState.from_json(host.state.to_json())
    host.wire.down = False
    assert flush_unbound(state=host.state, workspace_id=WS, sign_wsk=sign_wsk, now_ms=lambda: NOW,
                         transport=host.wire, save=host._save)  # fmt: skip
    assert host.state.pending_unbound is None and hub.workspaces[WS].binding["status"] == "unbound"


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
              transport=host.wire, record_left=boom, save=host._save)  # fmt: skip
    assert len(host.wire.sent) == n and host.state.status == "bound"


def test_declined_join_sends_nothing(host: Host) -> None:
    def boom(_p):
        raise RuntimeError("owner declined")

    with pytest.raises(RuntimeError):
        join_selective(state=None, transport=host.wire, record_joined=boom, save=host._save, **host.kw)
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
