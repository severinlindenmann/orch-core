# ruff: noqa: F811
"""#1097 on the host: the signed exposure mark, the gated acts it refuses (``sign.key_exposed``) at prepare and at the
append choke point, the break-glass mark that lands in full mode, and the guided rotation (``device.rotate``) with a
NEW device that the roster does not know yet."""

from __future__ import annotations

from orch import crypto
from orch.host import hubops, hubsign
from orch.host import human as hh
from orch.identity import certs
from tests.host import human_helpers
from tests.host.human_helpers import Human, err, ok
from tests.host.test_hub_ops import (  # noqa: F401
    CARD,
    connect_args,
    dev_workspace,
    join_args,
    keystore,
    stub,
)
from tests.host.test_hub_optional import full_joined, owner_event

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def device_of(served, ref):
    served.service.store.refresh()
    return served.service.store.state.workspace.devices[ref]


def left(served, **extra):
    hub = served.service.store.state.workspace.hub
    return owner_event(served, "hub.left", hub_id=hub["hub_id"], binding_seq=hub["binding_seq"], **extra)


# -- the mark ------------------------------------------------------------------------------------------------------


def test_the_mark_is_a_person_op_signed_by_the_device_itself(human, served):
    owner = served.lab.ws.owner
    assert not device_of(served, owner.device).exposed
    prepared, _, signed = human.run("device.mark_exposed", {})
    (ev,) = signed.events
    assert ev["type"] == "device.exposed" and ev["device"] == owner.device and ev["reason"] == "convenience"
    assert device_of(served, owner.device).exposed
    assert any(f == "type" and v == "device.exposed" for f, v in human.backend.prompts[-1])  # described, fail closed
    human.run("device.mark_exposed", {})  # idempotent


def test_a_mark_can_be_written_in_full_hub_mode_even_by_an_exposed_key(human, served, stub, keystore):
    full_joined(human, served, connect=True)
    assert hubops.signing_mode(served.service) == "full"
    human.run("device.mark_exposed", {})  # lands: it only removes authority
    human.run("device.mark_exposed", {})  # and again, now from an exposed key


# -- the gated acts ------------------------------------------------------------------------------------------------


def test_a_clean_key_still_joins_full_and_leaves(human, served, stub, keystore):
    full_joined(human, served, connect=True)
    human.run("hub.leave", {})


def test_keys_rotate_is_refused_for_an_exposed_key_read_from_the_roster(human, served, stub, keystore):
    assert hubsign.local_key_exposed(served.service, None) is True  # unknown person: fail closed
    assert hubsign.local_key_exposed(served.service, served.lab.ws.owner.ref) is False
    human.run("device.mark_exposed", {})
    assert hubsign.local_key_exposed(served.service, served.lab.ws.owner.ref) is True
    assert err(served.client().req("keys.rotate", {}, idem=True)) == "sign.key_exposed"


# -- the guided rotation --------------------------------------------------------------------------------------------


class NewDevice:
    """A new device key of the owner (not in the roster yet)."""

    def __init__(self, owner):
        self.owner = owner
        self.key = crypto.generate_private_key()
        self.sig_pub = crypto.public_bytes(self.key)
        self.ref = owner.ref

    @property
    def device(self):
        return certs.device_ref(self.sig_pub)

    def dk_sign(self, payload):
        return crypto.sign(self.key, payload)

    def args(self, served, *, old=None, roster_v=None):
        owner = self.owner
        ws = served.service.store.state.workspace
        cert = certs.make_device_cert(
            owner.pk_pub, owner.pk_sign, dk_sig_pub=self.sig_pub,
            dk_kx_pub=crypto.public_bytes(crypto.generate_private_key()),
            label_sealed=b"x", created_ms=served.lab.ws.clock[0] * 1000, expires_ms=None,
            scopes_max=["look", "decide", "operate", "type"],
        )  # fmt: skip
        old = old or owner.device
        rotation = certs.make_rotation(
            owner.pk_pub, owner.pk_sign, workspace_id=served.wid, old_hex=old[2:], new_hex=self.device[2:],
            roster_v=ws.roster_v if roster_v is None else roster_v,
        )  # fmt: skip
        return {"old": old, "cert": cert, "rotation": rotation}


def test_rotation_replaces_the_old_key_and_the_new_one_is_clean(human, served):
    owner = served.lab.ws.owner
    human.run("device.mark_exposed", {})
    new = NewDevice(owner)
    h2 = Human(served, person=new)
    prepared = h2.prepare("device.rotate", new.args(served))
    signed = h2.sign(prepared)
    ok(h2.submit_res(prepared["request"]["id"], signed.events))
    assert device_of(served, owner.device).removed
    fresh = device_of(served, new.device)
    assert not fresh.removed and not fresh.exposed
    assert err(human.prepare_res("log", {"ref": served.ticket()[1], "text": "x"})) == "device.invalid"


def test_rotation_in_full_mode_with_the_hub_down_lets_the_new_key_leave(human, served, stub, keystore):
    owner = served.lab.ws.owner
    full_joined(human, served, connect=True)
    human.run("device.mark_exposed", {})
    new = NewDevice(owner)
    h2 = Human(served, person=new)
    prepared = h2.prepare("device.rotate", new.args(served))  # a pk row: no hub, no owner device key needed
    ok(h2.submit_res(prepared["request"]["id"], h2.sign(prepared).events))
    h2.run("hub.leave", {})  # the new key is clean: the break-glass leave works
    assert served.service.store.state.workspace.hub["status"] == "left"


def test_a_rotation_for_another_workspace_or_a_stale_roster_is_refused(human, served):
    owner = served.lab.ws.owner
    new = NewDevice(owner)
    h2 = Human(served, person=new)
    bad = new.args(served, roster_v=99)
    assert err(h2.prepare_res("device.rotate", bad)) is not None
    other = new.args(served)
    other["rotation"] = certs.make_rotation(
        owner.pk_pub, owner.pk_sign, workspace_id="ab" * 16, old_hex=owner.device[2:], new_hex=new.device[2:],
        roster_v=served.service.store.state.workspace.roster_v,
    )  # fmt: skip
    assert err(h2.prepare_res("device.rotate", other)) is not None
    assert not device_of(served, owner.device).removed


def test_only_the_signer_may_be_the_new_device(human, served):
    owner = served.lab.ws.owner
    new = NewDevice(owner)
    args = new.args(served)
    # the old device (a roster device) cannot sign someone else's rotation: the certificate names another device
    assert err(human.prepare_res("device.rotate", args)) is not None


def test_hubsign_error_text_names_the_way_out():
    e = hubsign.key_exposed_error()
    assert e.code == "sign.key_exposed" and "--rotate" in (e.hint or "")
    assert hh.ROTATE_OP == "device.rotate"
