"""#1097 (design-1097-key-exposure v2): a device key with positive evidence of convenience custody (a signed
``device.exposed`` mark) may not enter full hub mode or sign break-glass, whatever ``auth`` label it writes. A key
with no mark counts as clean. Only a new key
(``device.rotated``) clears the exposure."""

from __future__ import annotations

import pytest

from orch.model import Code, admit, replay
from tests.model.test_hub_consumers import HUB, ROOT
from tests.model.world import World
from tests.schema.examples import cert, hex32

NEW = "d_" + hex32("device-sev-new")


def strict(w: World):
    return replay(w.ws, w.tl, verifier=w.verifier, now=w.at(), expected_workspace_id=w.workspace_id)


def codes(w: World) -> list[str]:
    return [i.code for i in strict(w).workspace.invalid]


def second_device(w: World, name: str = "b") -> dict:
    """A second, clean device of sev (added by sev's first device); returns its actor."""
    dev = "d_" + hex32("device-sev-" + name)
    w.wev("device.added", "sev", device=dev, cert=cert(w.people["sev"][2:], dev[2:]))
    return {"kind": "person", "id": w.people["sev"], "device": dev}


def mark(w: World, who="sev", **kw):
    a = w.actor(who) if isinstance(who, str) else who
    return w.wev("device.exposed", a, device=a["device"], reason="convenience", **kw)


@pytest.fixture
def w() -> World:
    """sev (owner, device A) and mara; a second clean device B of sev; joined to a hub in FULL mode (by B)."""
    w = World(validate=False).bootstrap({"mara": "maintainer"})
    w.b = second_device(w)  # type: ignore[attr-defined]
    w.wev("hub.joined", w.b, hub_id=HUB, hk_root_pub=ROOT, mode="full", binding_seq=1, aliases=[])  # type: ignore[attr-defined]
    return w


def leave(w: World, actor, **extra):
    return w.build_unappended("workspace", "hub.left", actor, roster_v=w.roster_v, hub_id=HUB, binding_seq=1, **extra)


# --- the mark ------------------------------------------------------------------------------------------------------


def test_the_mark_exposes_only_its_own_device_and_is_monotonic(w):
    mark(w)
    devs = strict(w)._core.ws.devices
    assert devs[w.dev["sev"]].exposed and not devs[w.b["device"]].exposed
    mark(w)  # idempotent, nothing clears it
    assert strict(w)._core.ws.devices[w.dev["sev"]].exposed


def test_a_mark_for_another_device_or_by_another_person_is_refused(w):
    _, r = w.try_("workspace", "device.exposed", "sev", roster_v=w.roster_v, device=w.b["device"], reason="convenience")
    assert r.code == Code.ROLE_DENIED  # a device marks itself
    _, r = w.try_("workspace", "device.exposed", "mara", roster_v=w.roster_v, device=w.dev["sev"], reason="convenience")
    assert r.code == Code.ROLE_DENIED


def test_the_hub_and_an_agent_cannot_write_a_mark(w):
    for actor in ({"kind": "agent", "id": "a_x", "session": "s_x", "unattended": True}, {"kind": "host"}):
        e = w.build_unappended("workspace", "device.exposed", actor, device=w.dev["sev"], reason="convenience")
        assert admit(strict(w), e, log="workspace").code in (Code.EVENT_BAD_ACTOR, Code.EVENT_UNKNOWN_TYPE)
    hub_line = w.build_unappended("workspace", "device.exposed", "sev", roster_v=w.roster_v, device=w.dev["sev"],
                                  reason="convenience", auth="hub")  # fmt: skip
    assert hasattr(admit(strict(w), hub_line, log="workspace"), "code")  # an auth hub label without a hub actor


def test_a_mark_lands_in_full_mode_even_from_an_exposed_key(w):
    st = strict(w)
    e = w.build_unappended("workspace", "device.exposed", "sev", roster_v=w.roster_v, device=w.dev["sev"],
                           reason="convenience")  # fmt: skip
    assert not hasattr(admit(st, e, log="workspace"), "code")  # A is clean: its mark lands
    mark(w)
    e2 = w.build_unappended("workspace", "device.exposed", "sev", roster_v=w.roster_v, device=w.dev["sev"],
                            reason="convenience")  # fmt: skip
    assert not hasattr(admit(strict(w), e2, log="workspace"), "code")  # again, from the exposed key itself
    conv = w.build_unappended("workspace", "device.exposed", "sev", roster_v=w.roster_v, device=w.dev["sev"],
                              reason="convenience", auth="convenience")  # fmt: skip
    assert not hasattr(admit(strict(w), conv, log="workspace"), "code")


# --- the headline forgery ------------------------------------------------------------------------------------------


def test_clean_keys_are_unaffected_an_unmarked_key_leaves(w):
    assert not hasattr(admit(strict(w), leave(w, w.b), log="workspace"), "code")
    assert not hasattr(admit(strict(w), leave(w, "sev"), log="workspace"), "code")  # A has no mark: clean


# --- full-mode entry -----------------------------------------------------------------------------------------------


# --- rotation ------------------------------------------------------------------------------------------------------


def rotated(w: World, **over):
    """A device.rotated of sev: the NEW device signs, replacing device A."""
    o = {
        "v": 2, "suite": 2, "kind": "device_rotation", "workspace_id": w.workspace_id, "person_id": w.people["sev"][2:],
        "old": w.dev["sev"][2:], "new": NEW[2:], "roster_v": w.roster_v,
    }  # fmt: skip
    fields = dict(
        old=w.dev["sev"], device=NEW, cert=cert(w.people["sev"][2:], NEW[2:]),
        rotation={"o": {**o, **over.pop("rot", {})}, "sig": "s"}, roster_v=w.roster_v,
    )  # fmt: skip
    fields.update(over)
    return w.build_unappended("workspace", "device.rotated", {"kind": "person", "id": w.people["sev"], "device": NEW},
                              **fields)  # fmt: skip


def test_rotation_replaces_the_old_key_in_full_mode_with_the_hub_down(w):
    mark(w)
    st = strict(w)
    e = rotated(w)
    assert not hasattr(admit(st, e, log="workspace"), "code")  # a pk row: works in full mode, nothing from the hub
    w.ws.append({**e, "host_sig": "x"})
    devs = strict(w)._core.ws.devices
    assert devs[w.dev["sev"]].removed and not devs[NEW].exposed and not devs[NEW].removed
    # the new key leaves; the old (copied) key is dead
    new_actor = {"kind": "person", "id": w.people["sev"], "device": NEW}
    assert not hasattr(admit(strict(w), leave(w, new_actor), log="workspace"), "code")
    assert admit(strict(w), leave(w, "sev"), log="workspace").code == Code.DEVICE_INVALID


@pytest.mark.parametrize(
    "over, code",
    [
        ({"rot": {"workspace_id": hex32("other-ws")}}, Code.DEVICE_CERT),  # replayed from another workspace
        ({"rot": {"roster_v": 99}}, Code.DEVICE_CERT),  # a stale roster version
        ({"rot": {"old": hex32("device-mara")}}, Code.DEVICE_CERT),  # not for this old device
        ({"rot": {"new": hex32("device-else")}}, Code.DEVICE_CERT),
        ({"old": "d_" + hex32("device-mara")}, Code.DEVICE_UNKNOWN),  # old must be a device of the same person
        ({"old": "d_" + hex32("nobody")}, Code.DEVICE_UNKNOWN),
    ],
)
def test_a_rotation_must_be_bound_to_this_workspace_person_and_devices(w, over, code):
    e = rotated(w, **over)
    if "old" in over:
        e["rotation"]["o"]["old"] = over["old"][2:]
    r = admit(strict(w), e, log="workspace")
    assert r.code == code


def test_a_rotation_cannot_reuse_an_existing_device(w):
    e = rotated(w)
    e["device"] = w.b["device"]
    e["actor"] = {**e["actor"], "device": w.b["device"]}
    r = admit(strict(w), e, log="workspace")
    assert hasattr(r, "code")


def test_a_failed_verifier_refuses_the_rotation(w):
    w.verifier.bad_embedded.add("zz")
    e = rotated(w, id="zz")
    assert admit(strict(w), e, log="workspace").code == Code.DEVICE_CERT


LABELS = [None, "passphrase", "click", "convenience", "secure-enclave-unlocked", "no-such-label"]
