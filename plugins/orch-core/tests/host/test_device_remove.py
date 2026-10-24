"""#1198 C15: ``device.remove`` is a plain click with two lockout guards: never the device that signs the removal, and
never the last working device of an owner. The model's rule (the device's own person or an owner) is unchanged."""

from __future__ import annotations

import pytest

from orch.click.protocol import CLICK_OPS, op_class
from orch.host import review, signer
from orch.ops import person_ops
from tests.host import human_helpers
from tests.host.human_helpers import err
from tests.host.test_human_fixes import _owner_device

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def removed(served, device: str) -> bool:
    store = served.lab.ws.other()
    try:
        return store.state.workspace.devices[device].removed
    finally:
        store.close()


def second_owner(lab):
    """A member with the role owner and one device (the first owner adds it as a member, then makes it an owner)."""
    p = lab.add_member()
    ws = lab.ws
    ws.tick()
    store = ws.other()
    ws.store = store
    try:
        store.append(
            ws.person_event(ws.owner, "workspace", "role.changed", person=p.ref, role="owner"), log="workspace"
        )
    finally:
        store.close()
        ws.store = None
    return p


def test_it_is_a_click_op_and_a_person_op():
    assert "device.remove" in CLICK_OPS and op_class("device.remove") == "click"
    assert person_ops.get("device.remove").who == "human"
    assert review.person_op_types("device.remove") == frozenset({"device.removed"})


def test_an_owner_removes_a_second_device_of_their_own(human, served, lab):
    other = _owner_device(lab, ("look", "decide", "operate", "type"))
    prepared, done, signed = human.run("device.remove", {"device": other})
    (ev,) = signed.events
    assert ev["type"] == "device.removed" and ev["device"] == other
    assert removed(served, other)
    assert prepared["material"]["review"] is not None


def test_the_device_that_signs_cannot_be_removed(human, served, lab):
    _owner_device(lab, ("look", "decide", "operate", "type"))  # so it is not the last device either
    res = human.prepare_res("device.remove", {"device": human.person.device})
    assert err(res) == "device.lockout"
    assert "device you are signing with" in res["error"]["message"]
    assert not removed(served, human.person.device)


def test_the_signer_refuses_the_own_device_even_if_a_host_prepared_it(human, served, lab):
    other = _owner_device(lab, ("look", "decide", "operate", "type"))
    prepared = human.prepare("device.remove", {"device": other})
    forged = {**prepared, "events": [{**prepared["events"][0], "device": human.person.device}]}
    with pytest.raises(signer.SignerRefused):
        human.check(forged)


def test_the_last_working_device_of_an_owner_stays(human, served, lab):
    mara = second_owner(lab)
    res = human.prepare_res("device.remove", {"device": mara.device})
    assert err(res) == "device.lockout"
    assert "last working device of an owner" in res["error"]["message"]
    assert not removed(served, mara.device)


def test_a_member_may_lose_their_only_device(human, served, lab):
    member = lab.add_member()
    human.run("device.remove", {"device": member.device})
    assert removed(served, member.device)


def test_an_unknown_or_removed_device_is_not_found(human, served, lab):
    assert err(human.prepare_res("device.remove", {"device": "d_" + "0" * 32})) == "not_found"
    member = lab.add_member()
    human.run("device.remove", {"device": member.device})
    assert err(human.prepare_res("device.remove", {"device": member.device})) == "not_found"
