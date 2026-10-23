"""C2b: the full join (spec 8 step 3, 8a; hub contract 9.1, 10.2): the hub becomes a member, ``WK_e`` is sealed to
``HXK`` over the link, a leave starts a new epoch the hub never gets, and selective mode never seals anything."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag

from orch import crypto
from orch.crypto import suite as S
from orch.hubclient import (
    HubLink,
    HubRefused,
    HubState,
    join_full,
    join_selective,
    leave,
    seal_to_hub,
    settle_rotation,
    switch_full,
)
from tests.wsproto import r6kit

from .fake import FakeHub, HostLink
from .vec import V, private_key, pub

WS = V["ids"]["workspace_a"]
HUB_ID = V["ids"]["hub"]
NOW = V["now_ms"]
WSK = private_key("wsk_a")


def sign_wsk(b: bytes) -> bytes:
    return S.sign(WSK, b)


class Wire:
    def __init__(self, hub: FakeHub) -> None:
        self.hub, self.down, self.sent, self.refuse_ops = hub, False, [], set()

    def send(self, message: dict[str, Any]) -> dict[str, Any]:
        if self.down or message["o"]["op"] in self.refuse_ops:
            raise ConnectionRefusedError
        self.sent.append(message)
        return self.hub.send(message)

    def ops(self) -> list[str]:
        return [m["o"]["op"] for m in self.sent]


class Epochs:
    """The host's adapter over a real membership key store (``orch.host.hubops._Epochs``)."""

    def __init__(self, mk, local_owner=None, sealed_epoch=None) -> None:
        from orch.host.hubops import _Epochs

        self._e = _Epochs(mk, local_owner, sealed_epoch)

    def __getattr__(self, name):
        return getattr(self._e, name)


@pytest.fixture
def mk(tmp_path):
    from orch.identity.epochs import MembershipKeys

    keys = MembershipKeys(tmp_path / "membership", WS, now_ms=lambda: NOW)
    from orch.identity.epochs import R6Gate

    R6Gate(keys).install(lambda: r6kit.KitStore())
    r6kit.rotate(keys)  # epoch 1 committed
    yield keys
    keys.close()


class Host:
    def __init__(self, hub: FakeHub, mk, local_owner=None) -> None:
        self.hub, self.wire, self.state, self.log, self.mk = hub, Wire(hub), None, [], mk
        self.epochs = Epochs(mk, local_owner, self._sealed_epoch)
        self.kw = dict(
            invite="invite-1", hub_card=V["objects"]["card"], v2_card=V["objects"]["v2_card_a"],
            pk_pub=pub("pk_alice"), workspace_id=WS, wsk_pub=pub("wsk_a"), sign_wsk=sign_wsk, dev_mode=True,
            now_ms=lambda: NOW, aliases=[],
        )  # fmt: skip

    def _save(self, s: HubState) -> None:
        self.state = HubState.from_json(copy.deepcopy(s.to_json()))

    def common(self, **over: Any) -> dict[str, Any]:
        return dict(state=self.state, transport=self.wire, save=self._save, **{**self.kw, **over})

    def join_full(self, **over: Any):
        kw = self.common(**over)
        return join_full(
            epochs=self.epochs, record_joined=lambda p: self.log.append(("hub.joined", p)),
            old_transport=lambda h, e: self.wire, **kw,
        )  # fmt: skip

    def join_selective(self, **over: Any):
        kw = self.common(**over)
        return join_selective(
            record_joined=lambda p: self.log.append(("hub.joined", p)), old_transport=lambda h, e: self.wire, **kw
        )

    def switch(self, seq: int = 2, **over: Any):
        keep = {"state", "hub_card", "workspace_id", "wsk_pub", "sign_wsk", "dev_mode", "now_ms", "transport", "save"}
        kw = {k: v for k, v in self.common(**over).items() if k in keep}
        return switch_full(binding_seq=seq, epochs=self.epochs, **kw)

    def seal(self, **over: Any):
        return seal_to_hub(
            state=self.state, hub_card=V["objects"]["card"], workspace_id=WS, sign_wsk=sign_wsk, dev_mode=True,
            now_ms=lambda: NOW, transport=self.wire, save=self._save, epochs=self.epochs, **over,
        )  # fmt: skip

    def _sealed_epoch(self):
        st = self.state
        return None if st is None or st.sealed is None else int(st.sealed["epoch"])

    def leave(self):
        return leave(
            state=self.state, workspace_id=WS, sign_wsk=sign_wsk, dev_mode=True, now_ms=lambda: NOW,
            transport_for=lambda h, e: self.wire, record_left=lambda p: self.log.append(("hub.left", p)),
            save=self._save, epochs=self.epochs,
        )  # fmt: skip


@pytest.fixture
def host(mk) -> Host:
    return Host(FakeHub(), mk)


def test_a_full_join_seals_the_current_wk_and_the_hub_can_unwrap_it(host, mk):
    r = host.join_full()
    assert r.payload["mode"] == "full" and host.log == [("hub.joined", r.payload)]
    assert host.wire.ops() == ["join", "wk_seal"]
    ws = host.hub.workspaces[WS]
    assert ws.mode == "full" and ws.binding["binding"]["mode"] == "full"
    assert ws.wk == {1: mk.wk(1)}  # the fake hub opened the wrap with the vector HXK: it holds exactly WK_1
    seal = host.wire.sent[1]["o"]["body"]
    assert set(seal) == {"epoch", "wrap"} and seal["epoch"] == 1
    assert host.state.mode == "full" and host.state.status == "bound" and host.state.sealed["epoch"] == 1


def test_the_wrap_is_bound_to_this_hub_workspace_and_epoch(host, mk):
    host.join_full()
    wrap = crypto.unb64u(host.wire.sent[1]["o"]["body"]["wrap"])
    hxk = private_key("hxk")
    me, ws = bytes.fromhex(HUB_ID), bytes.fromhex(WS)
    assert crypto.open_sealed(hxk, me, "wk", ws, 1, wrap) == mk.wk(1)
    other_hub = bytes.fromhex(V["ids"]["hub_rogue"])
    for args in ((other_hub, "wk", ws, 1), (me, "wk", ws, 2), (me, "wk", bytes.fromhex(V["ids"]["workspace_b"]), 1)):
        with pytest.raises(InvalidTag):  # a replay for another hub, epoch or workspace opens nothing
            crypto.open_sealed(hxk, args[0], args[1], args[2], args[3], wrap)


def test_a_replayed_wk_seal_is_refused_by_the_hub(host, mk):
    host.join_full()
    body = copy.deepcopy(host.wire.sent[1]["o"]["body"])
    link = HostLink(host.hub)
    link.seq = 50
    for bad in ({**body, "epoch": 2}, {**body, "wrap": crypto.b64u(bytes(113))}):
        reply = link.request("wk_seal", bad)
        assert reply["body"] == {"error": "bad_wrap"}
    # sealed for a different key (another hub's HXK): the hub cannot open it
    other = mk.sealed_wk(1, pub("dk_mallory.kx"), HUB_ID)
    assert link.request("wk_seal", {"epoch": 1, "wrap": crypto.b64u(other)})["body"] == {"error": "bad_wrap"}
    assert list(host.hub.workspaces[WS].wk) == [1]


def test_no_epoch_means_nothing_is_sent(tmp_path):
    from orch.identity.epochs import MembershipKeys, R6Gate

    empty = MembershipKeys(tmp_path / "m2", WS, now_ms=lambda: NOW)
    R6Gate(empty).install(lambda: r6kit.KitStore())
    h = Host(FakeHub(), empty)
    with pytest.raises(HubRefused) as e:
        h.join_full()
    assert e.value.code == "no_epoch" and h.wire.sent == [] and h.state is None and h.log == []
    empty.close()


def test_a_down_hub_at_the_seal_leaves_a_bound_join_and_the_seal_is_repeatable(host, mk):
    host.wire.refuse_ops = {"wk_seal"}
    with pytest.raises(HubRefused) as e:
        host.join_full()
    assert e.value.code == "hub_unreachable"
    assert host.state.status == "bound" and host.state.sealed is None and host.hub.workspaces[WS].wk == {}
    host.wire.refuse_ops = set()
    assert host.seal() == 1
    assert host.hub.workspaces[WS].wk == {1: mk.wk(1)} and host.state.sealed["epoch"] == 1
    n = len(host.wire.sent)
    assert host.seal() == 1 and len(host.wire.sent) == n  # already sealed: nothing is sent again


def test_a_selective_workspace_never_has_a_hub_member_or_a_seal(host, mk):
    r = host.join_selective()
    assert r.payload["mode"] == "selective" and host.state.mode == "selective" and host.state.sealed is None
    assert host.wire.ops() == ["join"] and host.hub.workspaces[WS].wk_seals == []
    assert host.state.hxk_pub is not None  # the key is known, and still never used
    link = HubLink(host.wire, hub_id=HUB_ID, workspace_id=WS, hk_op_pub=pub("hk_op"), sign=sign_wsk, mode="selective",
                   now_ms=lambda: NOW)  # fmt: skip
    with pytest.raises(HubRefused) as e:
        link.call("wk_seal", {"epoch": 1, "wrap": crypto.b64u(bytes(113))})
    assert e.value.code == "wrong_mode" and host.wire.ops() == ["join"]
    with pytest.raises(HubRefused) as e:
        host.join_selective(mode="full")
    assert e.value.code == "full_mode_later"
    with pytest.raises(HubRefused):
        host.seal()  # sealing needs a full binding


def test_selective_to_full_is_a_binding_switch_then_a_seal(host, mk):
    host.join_selective()
    assert host.hub.workspaces[WS].wk_seals == []
    st = host.switch(seq=2)
    assert st.mode == "full" and st.binding_seq == 2 and st.status == "bound" and st.sealed["epoch"] == 1
    assert host.wire.ops() == ["join", "binding_put", "wk_seal"]
    assert host.hub.workspaces[WS].mode == "full" and host.hub.workspaces[WS].wk == {1: mk.wk(1)}


def test_a_switch_with_the_hub_down_resumes_with_the_same_signed_binding(host):
    host.join_selective()
    host.wire.down = True
    with pytest.raises(HubRefused):
        host.switch(seq=2)
    assert host.state.status == "switching" and host.state.mode == "full"
    signed = copy.deepcopy(host.state.binding)
    host.wire.down = False
    host.switch(seq=2)
    assert host.state.binding == signed and host.state.status == "bound" and host.state.sealed["epoch"] == 1
    with pytest.raises(HubRefused):  # a bound full hub is not switched again
        host.switch(seq=3)


def test_a_switch_needs_a_higher_seq_and_the_pinned_hub(host):
    host.join_selective()
    with pytest.raises(HubRefused) as e:
        host.switch(seq=1)
    assert e.value.code == "stale_binding"
    card = copy.deepcopy(V["objects"]["card"])
    card["o"]["hk_root_pub"] = V["keys"]["hk_root_rogue"]["pub"]
    with pytest.raises(HubRefused) as e:
        host.switch(seq=2, hub_card=card)
    assert e.value.code == "other_hub" and host.hub.workspaces[WS].mode == "selective"


def test_leave_from_full_removes_the_member_and_owes_a_new_epoch_the_hub_never_gets(host, mk):
    host.join_full()
    r = host.leave()
    assert not r.rotation_done and host.state.rotate_after == 1 and host.state.status == "unbound"
    assert mk.gate_up() is True  # the rotation was asked for through the key store's own gate
    assert host.hub.workspaces[WS].binding["status"] == "unbound"
    # the membership machinery makes the next epoch (stand-in for the publisher thread)
    r6kit.rotate(mk)
    assert mk.current_epoch() == 2
    assert settle_rotation(state=host.state, epochs=host.epochs, save=host._save, request=False)
    assert host.state.rotate_after is None
    held = host.hub.workspaces[WS].wk
    assert list(held) == [1] and held[1] != mk.wk(2)  # the hub holds the old key only
    assert all(m["o"]["op"] != "wk_seal" or m["o"]["body"]["epoch"] == 1 for m in host.wire.sent)


def test_a_full_rejoin_waits_for_the_rotation_and_then_seals_only_the_new_epoch(host, mk):
    host.join_full()
    host.leave()
    n = len(host.wire.sent)
    with pytest.raises(HubRefused) as e:
        host.join_full(binding_seq=2, invite="invite-2")
    assert e.value.code == "rotation_pending" and len(host.wire.sent) == n
    r6kit.rotate(mk)
    host.hub.invites = None
    host.join_full(binding_seq=2, invite="invite-2")
    assert sorted(host.hub.workspaces[WS].wk) == [1, 2] and host.hub.workspaces[WS].wk[2] == mk.wk(2)
    assert host.state.rotate_after is None and host.state.sealed["epoch"] == 2


def test_full_to_selective_leaves_no_member_and_seals_nothing_new(host, mk):
    host.join_full()
    host.leave()
    sealed = len(host.hub.workspaces[WS].wk_seals)
    host.join_selective(binding_seq=2, invite="invite-2")
    assert host.state.mode == "selective" and host.state.sealed is None
    assert len(host.hub.workspaces[WS].wk_seals) == sealed  # the selective join sent no wk_seal
    assert host.state.rotate_after == 1  # the rotation is still owed and still tracked
    r6kit.rotate(mk)
    assert settle_rotation(state=host.state, epochs=host.epochs, save=host._save, request=False)


def test_a_leave_that_cannot_rotate_yet_stays_owed_across_a_restart(host):
    host.join_full()
    host.wire.down = True
    r = host.leave()  # hub down: still works
    assert not r.delivered and not r.rotation_done
    again = HubState.from_json(copy.deepcopy(host.state.to_json()))
    assert again.rotate_after == 1 and again.status == "unbound"


def test_the_state_file_refuses_a_bad_seal_record(host):
    host.join_full()
    doc = copy.deepcopy(host.state.to_json())
    for bad in ({"epoch": 0, "hxk_pub": doc["hxk_pub"]}, {"epoch": 1}, {"epoch": 1, "hxk_pub": "x"}, "yes"):
        with pytest.raises(HubRefused):
            HubState.from_json({**doc, "sealed": bad})
    with pytest.raises(HubRefused):
        HubState.from_json({**doc, "rotate_after": -1})
    legacy = {k: v for k, v in doc.items() if k not in ("hxk_pub", "sealed", "rotate_after")}
    assert HubState.from_json(legacy).sealed is None  # a C2a hub.json still reads


def test_only_the_committed_epoch_is_ever_sealed(host, mk):
    from orch.host.hubops import _Epochs

    r6kit.expose_rotation(mk)  # epoch 2 claimed and exposed, not committed
    e = _Epochs(mk)
    with pytest.raises(HubRefused):
        e.seal_to_hub(2, pub("hxk"), HUB_ID)
    assert e.seal_to_hub(1, pub("hxk"), HUB_ID)


def test_leave_then_rejoin_over_an_unended_binding_retires_the_full_state(host, mk):
    """hub.left was logged but no unbind ran: a full rejoin must not re-seal the epoch the hub already holds, and a
    selective rejoin must still owe the rotation."""
    host.join_full()
    for rejoin in ("full", "selective"):
        st = copy.deepcopy(host.state.to_json())
        h2 = Host(FakeHub(), mk)
        h2.state = HubState.from_json(st)
        with pytest.raises(HubRefused) if rejoin == "full" else _nothing():
            (h2.join_full if rejoin == "full" else h2.join_selective)(binding_seq=2, invite="invite-2")
        assert h2.state.rotate_after == 1 and h2.state.sealed is None
        if rejoin == "full":
            assert h2.wire.sent == []  # nothing reached the hub


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_relay_less_workspace_makes_its_own_genesis_epoch_and_rotates_locally(tmp_path):
    from orch.identity.epochs import MembershipKeys, R6Gate

    own = MembershipKeys(tmp_path / "m3", WS, now_ms=lambda: NOW)
    R6Gate(own).install(lambda: r6kit.KitStore())
    owner = V["ids"]["person_alice"]
    h = Host(FakeHub(), own, local_owner=owner)
    with pytest.raises(HubRefused):  # without ensure_epoch there is nothing to seal
        h.join_full()
    h = Host(FakeHub(), own, local_owner=owner)
    h.epochs.ensure_epoch()
    assert own.current_epoch() == 1
    h.join_full()
    assert h.hub.workspaces[WS].wk == {1: own.wk(1)}
    r = h.leave()
    assert r.rotation_done and h.state.rotate_after is None and own.current_epoch() == 2  # rotated at once
    assert list(h.hub.workspaces[WS].wk) == [1] and own.wk(2) != own.wk(1)
    own.close()


def test_a_relay_linked_key_store_never_makes_a_local_epoch(tmp_path):
    from orch.identity.epochs import CustodyRefused, MembershipKeys, MembershipUnsafe

    linked = MembershipKeys(tmp_path / "m4", WS, now_ms=lambda: NOW, previously_linked=True)
    with pytest.raises((CustodyRefused, MembershipUnsafe)):  # a linked store with no head is unsafe too
        linked.advance_local(V["ids"]["person_alice"])
    linked.close()


def test_a_crash_between_expose_and_commit_is_completed_by_the_next_advance(tmp_path):
    from orch.identity.epochs import MembershipKeys, R6Gate

    crash = {"on": True}

    def fault(point):
        if crash["on"] and point == "after_expose":
            raise RuntimeError("crash")

    own = MembershipKeys(tmp_path / "m5", WS, now_ms=lambda: NOW, fault=fault)
    R6Gate(own).install(lambda: r6kit.KitStore())
    owner = V["ids"]["person_alice"]
    with pytest.raises(RuntimeError):
        own.advance_local(owner)
    assert own.current_epoch() == 0 and own.head()["exposed"] is not None  # wedged without the fix
    crash["on"] = False
    assert own.advance_local(owner) == 1  # the complete, valid exposed body is committed, not duplicated
    assert own.current_epoch() == 1 and own.head()["exposed"] is None
    assert own.advance_local(owner) == 2 and own.wk(2) != own.wk(1)
    own.close()


def test_an_invalid_exposed_body_is_abandoned_and_a_new_epoch_follows(tmp_path):
    from orch.identity.epochs import MembershipKeys, R6Gate

    crash = {"on": True}

    def fault(point):
        if crash["on"] and point == "after_expose":
            raise RuntimeError("crash")

    own = MembershipKeys(tmp_path / "m6", WS, now_ms=lambda: NOW, fault=fault)
    R6Gate(own).install(lambda: r6kit.KitStore())
    owner = V["ids"]["person_alice"]
    with pytest.raises(RuntimeError):
        own.advance_local(owner)
    own._head["exposed"]["digest"] = "sha256:" + "0" * 64  # damaged on disk: not complete and valid
    crash["on"] = False
    e = own.advance_local(owner)
    assert e == 2 and own.current_epoch() == 2 and own.head()["exposed"] is None  # epoch 1 burned, never used
    assert 1 in own.head()["burned"]["epochs"] or own.is_burned(1)
    own.close()


def test_a_relay_linked_later_in_the_process_stops_local_epochs(tmp_path):
    from orch.identity.epochs import CustodyRefused, MembershipKeys, R6Gate

    linked = {"v": False}
    own = MembershipKeys(tmp_path / "m7", WS, now_ms=lambda: NOW)
    R6Gate(own).install(lambda: r6kit.KitStore())
    own.link_probe = lambda: linked["v"]
    owner = V["ids"]["person_alice"]
    assert own.advance_local(owner) == 1 and own.has_local_epochs()
    assert own.previously_linked is False
    linked["v"] = True  # a relay is linked after the key store was opened
    assert own.previously_linked is True
    with pytest.raises(CustodyRefused):
        own.advance_local(owner)
    assert own.current_epoch() == 1
    own.link_probe = lambda: (_ for _ in ()).throw(OSError("unreadable"))
    with pytest.raises(CustodyRefused):  # an unreadable link state counts as linked
        own.advance_local(owner)
    own.close()
