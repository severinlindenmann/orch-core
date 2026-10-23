"""#1097 (design-1097-key-exposure v2): a device key with positive evidence of convenience custody (a signed
``device.exposed`` mark, or an admitted line of that device with ``auth: "convenience"``) may not enter full hub mode
or sign break-glass, whatever ``auth`` label it writes. A key with no such evidence counts as clean. Only a new key
(``device.rotated``) clears the exposure."""

from __future__ import annotations

import pytest

from orch.model import Code, admit, breakglass, replay
from tests.model.test_hub_consumers import HUB, ROOT
from tests.model.world import World
from tests.schema.examples import cert, hex32

NEW = "d_" + hex32("device-sev-new")


def strict(w: World):
    return replay(w.ws, w.tl, verifier=w.verifier, now=w.at(), expected_workspace_id=w.workspace_id, hub_only=True)


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


def reaffirm(w: World, actor):
    return w.build_unappended(
        "workspace", "hub.joined", actor, roster_v=w.roster_v, hub_id=HUB, hk_root_pub=ROOT, mode="full",
        binding_seq=1, aliases=[],
    )  # fmt: skip


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


def test_a_copied_exposed_key_cannot_forge_a_passphrase_labelled_hub_left(w):
    mark(w)  # device A was in convenience custody: someone may hold a copy
    forged = leave(w, "sev", auth="passphrase")
    r = admit(strict(w), forged, log="workspace")
    assert r.code == Code.SIGN_KEY_EXPOSED
    # on replay: the same line sitting in a log is invalid, with the same code
    w.ws.append({**forged, "host_sig": "x", "sig": "x"})
    assert Code.SIGN_KEY_EXPOSED.value in codes(w)
    assert strict(w).workspace.hub["status"] == "joined"  # and the hub binding is untouched


def test_the_hub_mode_reaffirmation_is_refused_for_an_exposed_key_and_allowed_for_a_clean_one(w):
    assert not hasattr(admit(strict(w), reaffirm(w, w.b), log="workspace"), "code")  # B: clean
    mark(w)
    assert admit(strict(w), reaffirm(w, "sev"), log="workspace").code == Code.SIGN_KEY_EXPOSED


def test_clean_keys_are_unaffected_a_legacy_key_leaves_and_reaffirms(w):
    assert not hasattr(admit(strict(w), leave(w, w.b), log="workspace"), "code")
    assert not hasattr(admit(strict(w), leave(w, "sev"), log="workspace"), "code")  # A has no evidence: clean
    assert not hasattr(admit(strict(w), reaffirm(w, "sev"), log="workspace"), "code")


# --- full-mode entry -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["passphrase", "convenience"])
def test_a_first_full_join_is_refused_for_an_exposed_key_and_for_a_convenience_label(label):
    s = World(validate=False).bootstrap({})
    if label == "passphrase":
        s.wev("device.exposed", "sev", device=s.dev["sev"], reason="convenience")
    e = s.build_unappended("workspace", "hub.joined", "sev", roster_v=s.roster_v, hub_id=HUB, hk_root_pub=ROOT,
                           mode="full", binding_seq=1, aliases=[], auth=label)  # fmt: skip
    assert admit(strict(s), e, log="workspace").code == Code.SIGN_KEY_EXPOSED
    sel = s.build_unappended("workspace", "hub.joined", "sev", roster_v=s.roster_v, hub_id=HUB, hk_root_pub=ROOT,
                             mode="selective", binding_seq=1, aliases=[], auth=label)  # fmt: skip
    assert not hasattr(admit(strict(s), sel, log="workspace"), "code")  # a selective join is not gated


def test_a_clean_key_joins_full_and_a_marked_one_does_not():
    s = World(validate=False).bootstrap({})
    full = dict(roster_v=s.roster_v, hub_id=HUB, hk_root_pub=ROOT, mode="full", binding_seq=1, aliases=[])
    assert not hasattr(
        admit(strict(s), s.build_unappended("workspace", "hub.joined", "sev", **full), log="workspace"), "code"
    )
    s.wev("device.exposed", "sev", device=s.dev["sev"], reason="convenience")
    r = admit(strict(s), s.build_unappended("workspace", "hub.joined", "sev", **full), log="workspace")
    assert r.code == Code.SIGN_KEY_EXPOSED


def test_the_signer_kinds_and_the_table():
    assert "exposed" in breakglass.SIGNER_KINDS
    assert all(not breakglass.allowed(v, "exposed") for v in breakglass.BREAK_GLASS if v != "device.mark_exposed")
    assert breakglass.allowed("device.mark_exposed", "exposed")
    assert breakglass.allowed("device.rotate", "pk") and not breakglass.allowed("device.rotate", "owner_device_key")


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


@pytest.mark.parametrize("label", LABELS)
def test_an_exposed_device_is_refused_whatever_label_it_writes(w, label):
    """The exposure is read from the roster before the auth label: no label hides it (full join, leave, reaffirm)."""
    mark(w)
    extra = {} if label is None else {"auth": label}
    for build in (leave, reaffirm):
        e = build(w, "sev")
        if label is None:
            e.pop("auth", None)
        else:
            e["auth"] = label
        assert breakglass.signer_kind(strict(w)._core.ws, e) == "exposed", (label, extra)
        assert admit(strict(w), e, log="workspace").code == Code.SIGN_KEY_EXPOSED, (build.__name__, label)
    full = w.build_unappended(
        "workspace", "hub.joined", "sev", roster_v=w.roster_v, hub_id=HUB, hk_root_pub=ROOT, mode="full",
        binding_seq=2, aliases=[],
    )  # fmt: skip
    full["auth"] = label
    if label is None:
        full.pop("auth")
    assert admit(strict(w), full, log="workspace").code == Code.SIGN_KEY_EXPOSED


@pytest.mark.parametrize("label", LABELS)
def test_an_exposed_device_is_refused_a_first_full_join_whatever_label(label):
    s = World(validate=False).bootstrap({})
    s.wev("device.exposed", "sev", device=s.dev["sev"], reason="convenience")
    e = s.build_unappended("workspace", "hub.joined", "sev", roster_v=s.roster_v, hub_id=HUB, hk_root_pub=ROOT,
                           mode="full", binding_seq=1, aliases=[])  # fmt: skip
    e.pop("auth", None)
    if label is not None:
        e["auth"] = label
    assert admit(strict(s), e, log="workspace").code == Code.SIGN_KEY_EXPOSED
