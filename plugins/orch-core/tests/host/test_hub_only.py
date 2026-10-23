# ruff: noqa: F811
"""V3 C11 (orch-core#879, design-c11): in full hub mode no local key signs. The rule sits at the single choke point
(``human.append_signed``), reads the mode from the signed log under the store lock, and has one exception, break-glass
(``hubsign.BREAK_GLASS``: the owner's ``hub.left``). Invariants I1, I8, I9, I10, I11 of the note."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.host import convenience, hubops, hubsign
from orch.host import human as hh
from orch.host.errors import HostError
from tests.host import human_helpers
from tests.host.human_helpers import err
from tests.host.lifecycle_helpers import UID

# the fixtures and helpers of the hub op tests (the hub stub, the key store, the dev workspace)
from tests.host.test_hub_ops import (  # noqa: F401
    CARD,
    connect_args,
    dev_workspace,
    join_args,
    keystore,
    stub,
)

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def hub_json(served):
    return hubops._dir(served.service) / "hub.json"


def full_joined(human, served, *, connect=False):
    human.run("hub.join", join_args(mode="full"))
    if connect:
        from tests.host.dispatch_helpers import ok

        ok(served.client().req("hub.connect", connect_args(), idem=True))


def fake_event(typ="gate.approved", *, person="p_x", device="d_x", auth="passphrase", **extra):
    return {
        "id": "01J9ZP0000000000000000E" + str(abs(hash(typ)) % 1000).zfill(3),
        "type": typ,
        "actor": {"kind": "person", "id": person, "device": device},
        "auth": auth,
        "sig": "s" * 86,
        **extra,
    }


def owner_event(served, typ="gate.approved", **kw):
    o = served.lab.ws.owner
    return fake_event(typ, person=o.ref, device=o.device, **kw)


def append(served, events, **kw):
    kw.setdefault("signed_by", None)
    return hh.append_signed(served.service, "workspace", events, [{} for _ in events], **kw)


def log_len(served):
    served.service.store.refresh()
    return served.service.store.log_seq("workspace")


# -- I9: the mode comes from the signed log --------------------------------------------------------------------------


def test_signing_mode_a_workspace_that_never_joined_is_standalone_even_with_corrupt_hub_json(served):
    d = hub_json(served).parent
    d.mkdir(parents=True, exist_ok=True)
    hub_json(served).write_text("{not json")
    assert hubops.signing_mode(served.service) == "standalone"
    # and it still signs locally: nothing is refused
    key = served.ticket()[1]
    assert human_helpers.ok(
        served.client().req("human.prepare", {"op": "log", "args": {"ref": key, "text": "x"}, "handoff": True})
    )


def test_signing_mode_follows_the_log_through_join_leave_and_rejoin(human, served, stub, keystore):
    assert hubops.signing_mode(served.service) == "standalone"
    human.run("hub.join", join_args())
    assert hubops.signing_mode(served.service) == "selective"  # joined but not connected: nothing handed to the hub
    human.run("hub.leave", {})
    assert hubops.signing_mode(served.service) == "standalone"  # a later hub.left is standalone


def test_signing_mode_full_joined_with_hub_json_missing_or_unreadable_is_unknown(human, served):
    full_joined(human, served)
    assert hubops.signing_mode(served.service) == "unknown"  # joined in the log, no hub.json
    hub_json(served).parent.mkdir(parents=True, exist_ok=True)
    hub_json(served).write_text("{not json")
    assert hubops.signing_mode(served.service) == "unknown"
    assert hubsign.HUB_ONLY >= {"unknown", "full"}


def test_signing_mode_full_connected_is_full_and_a_foreign_hub_json_is_unknown(human, served, stub, keystore):
    full_joined(human, served, connect=True)
    assert hubops.signing_mode(served.service) == "full"
    # the C10 cut-over thread rewrites hub.json (read-modify-write under these two locks): write under them too, or its
    # stale copy can land after ours (seen on develop and in CI as 'full' == 'unknown'; the thread goes with R3/R4b)
    from orch.host import cutover

    with cutover._lock_for(served.service), hubops._MU:  # noqa: SLF001
        doc = json.loads(hub_json(served).read_text())
        doc["binding_seq"] += 1  # a hub.json that names another binding can only make it stricter
        hub_json(served).write_text(json.dumps(doc))
    assert hubops.signing_mode(served.service) == "unknown"


def test_signing_mode_a_selective_log_with_a_full_hub_json_is_unknown(human, served, stub, keystore):
    from tests.host.dispatch_helpers import ok

    full_joined(human, served, connect=True)
    doc = json.loads(hub_json(served).read_text())
    human.run("hub.leave", {})
    ok(served.client().req("hub.unbind", {}, idem=True))
    human.run("hub.join", join_args())
    hub_json(served).write_text(json.dumps({**doc, "status": "bound", "mode": "full"}))
    assert hubops.signing_mode(served.service) == "unknown"  # the file is ahead of the log: never looser than it


# -- I1: the choke point ---------------------------------------------------------------------------------------------

SIGNERS = {
    "owner key": dict(signed_by=None, auth="passphrase"),
    "convenience": dict(signed_by=None, auth="convenience"),
    "relay device": dict(signed_by="relay", auth="secure-enclave"),
    "dev-sim column R": dict(signed_by="relay", auth="convenience"),
}
TYPES = ["gate.approved", "grant.issued", "hub.joined", "settings.changed", "device.revoked"]


@pytest.mark.parametrize("mode", ["unknown", "full"])
@pytest.mark.parametrize("typ", TYPES)
@pytest.mark.parametrize("signer", sorted(SIGNERS))
def test_full_mode_refuses_every_local_signature_at_append_signed(
    human, served, stub, keystore, mode, typ, signer, monkeypatch
):
    monkeypatch.setattr(
        hh, "_dev_gate_holds", lambda *a, **k: True
    )  # column R's dev gate passed: only the mode refuses
    full_joined(human, served, connect=(mode == "full"))
    assert hubops.signing_mode(served.service) == mode
    spec = SIGNERS[signer]
    ev = owner_event(served, typ, auth=spec["auth"])
    by = None if spec["signed_by"] is None else (ev["actor"]["id"], ev["actor"]["device"])
    before = log_len(served)
    landed, error = append(served, [ev], signed_by=by)
    assert landed == [] and error is not None and error.code == "sign.hub_only"
    assert error.detail == {"reason": "hub"} and "hub" in (error.hint or "")
    assert log_len(served) == before


@pytest.mark.parametrize("mode", ["standalone", "selective"])
def test_standalone_and_selective_check_nothing(human, served, mode):
    if mode == "selective":
        human.run("hub.join", join_args())
        assert hubops.signing_mode(served.service) == "selective"
    ev = owner_event(served)
    assert hubsign.refusal(served.service, [ev], None) is None
    assert hubsign.refusal(served.service, [ev], (ev["actor"]["id"], ev["actor"]["device"])) is None


def test_selective_still_signs_locally(human, served):
    human.run("hub.join", join_args())
    key = served.ticket()[1]
    _, done, _ = human.run("log", {"ref": key, "text": "still local"})
    assert done["data"] is not None


def test_a_request_prepared_before_the_join_fails_after_it(human, served):
    key = served.ticket()[1]
    prepared = human.prepare("log", {"ref": key, "text": "prepared standalone"})
    signed = human.sign(prepared)
    full_joined(human, served)  # the join lands between prepare and submit
    res = human.submit_res(prepared["request"]["id"], signed.events)
    assert err(res) == "sign.hub_only"


def _journalled_then_joined(human, served):
    key = served.ticket()[1]
    prepared = human.prepare("log", {"ref": key, "text": "journalled"})
    signed = human.sign(prepared)
    svc = served.service
    rid = prepared["request"]["id"]
    full_joined(human, served)
    record = {**svc.requests.read(rid), "submit": {"events": signed.events, "outcome_key": "k"}}
    svc.requests.write(record)
    return svc, rid, record


def test_finish_refuses_a_journalled_request_after_the_join(human, served):
    svc, rid, record = _journalled_then_joined(human, served)
    before = log_len(served)
    with pytest.raises(HostError) as ei:  # the roll forward of a request signed before the join
        hh.finish(svc, svc.requests, record)
    assert ei.value.code == "sign.hub_only"
    assert log_len(served) == before


def test_recover_refuses_a_journalled_request_after_the_join(human, served):
    svc, rid, _ = _journalled_then_joined(human, served)
    before = log_len(served)
    assert dict(hh.recover(svc)) == {rid: "failed"}  # at start-up, before anything is served
    assert log_len(served) == before


def test_the_relay_path_refuses_a_human_verb_in_full_mode(human, served):
    full_joined(human, served)
    ev = owner_event(served, "gate.approved", auth="secure-enclave")
    landed, error = hh.append_relay(
        served.service, "workspace", [ev], [{}], person_id=ev["actor"]["id"], device_id=ev["actor"]["device"]
    )
    assert landed == [] and error.code == "sign.hub_only"


def test_the_early_prepare_check_is_friendly_and_the_hand_off_too(human, served):
    key = served.ticket()[1]
    full_joined(human, served)
    assert err(human.prepare_res("log", {"ref": key, "text": "x"})) == "sign.hub_only"
    assert err(human.prepare_res("log", {"ref": key, "text": "x"}, handoff=True)) == "sign.hub_only"


# -- I10: break-glass ------------------------------------------------------------------------------------------------


def test_break_glass_leave_still_works_in_full_mode_and_unknown(human, served, stub, keystore):
    full_joined(human, served)
    assert hubops.signing_mode(served.service) == "unknown"
    human.run("hub.leave", {})  # the owner's passphrase key, over the person path
    assert hubops.signing_mode(served.service) == "standalone"


def test_break_glass_leave_works_with_the_hub_up_and_connected(human, served, stub, keystore):
    full_joined(human, served, connect=True)
    assert hubops.signing_mode(served.service) == "full"
    human.run("hub.leave", {})
    assert hubops.signing_mode(served.service) == "standalone"


def ws_model(*, owner_device="d_o", other=True):
    return SimpleNamespace(
        members={
            "p_o": SimpleNamespace(role="owner"),
            "p_m": SimpleNamespace(role="maintainer"),
        },
        devices={
            owner_device: SimpleNamespace(person="p_o", exposed=False),
            "d_m": SimpleNamespace(person="p_m", exposed=False),
        },
    )


def left(person="p_o", device="d_o", **kw):
    return fake_event("hub.left", person=person, device=device, **kw)


def test_break_glass_signer_rules():
    ws = ws_model()
    ok = hubsign.break_glass_allowed
    assert ok(ws, left(), None)  # the owner's passphrase device key
    assert not ok(ws, left(auth="convenience"), None)  # never click-to-sign
    assert not ok(ws, left(auth="secure-enclave-unlocked"), None)  # nor a phone key
    assert not ok(ws, left(auth="hub"), None)
    assert not ok(ws, left(), ("p_o", "d_o"))  # not over the relay
    assert not ok(ws, left(person="p_m", device="d_m"), None)  # not a non-owner
    assert not ok(ws, left(device="d_m"), None)  # not a device that is not the owner's
    assert not ok(ws, left(person="p_unknown", device="d_u"), None)
    ev = left()
    ev["actor"] = {**ev["actor"], "via": "c_1"}
    assert not ok(ws, ev, None)  # not a hub channel
    assert not ok(ws, fake_event("hub.joined", person="p_o", device="d_o"), None)  # a second join is a repin: refused
    assert not ok(ws, fake_event("gate.approved", person="p_o", device="d_o"), None)


def test_the_recovery_key_recovers_a_device_and_never_signs_anything_else():
    """L2-C8i-1: ``device.recover`` is the one ``pk`` row (the D50 ``device.added`` shape: the new device signs with the
    recovery certificate, the owner has no valid device); a plain device key or any other label never recovers."""
    ws = ws_model(owner_device="d_m_only")  # the owner's device is not on the roster (lost): only maintainer d_m
    ws.devices.pop("d_m_only")
    at = "2026-10-23T10:00:00Z"
    rec = fake_event("device.added", person="p_o", device="d_new", cert={"x": 1}, at=at)
    rec["device"] = "d_new"
    assert hubsign.break_glass_allowed(ws, rec, None)
    assert not hubsign.break_glass_allowed(ws, rec, ("p_o", "d_new"))  # never the relay
    assert not hubsign.break_glass_allowed(ws, {**rec, "auth": "convenience"}, None)
    assert not hubsign.break_glass_allowed(ws, {**rec, "device": "d_other"}, None)  # not signed by the new device
    assert not hubsign.break_glass_allowed(ws, {k: v for k, v in rec.items() if k != "cert"}, None)
    assert not hubsign.break_glass_allowed(
        ws, {**rec, "actor": {"kind": "agent", "id": "p_o", "device": "d_new"}}, None
    )
    other = fake_event("device.added", person="p_m", device="d_new", cert={"x": 1}, at=at)
    other["device"] = "d_new"
    assert not hubsign.break_glass_allowed(ws, other, None)  # a non-owner recovering
    # a hub.repinned / hub.left in the recovery shape is never "pk": the recovered device does the leaving
    assert not hubsign.break_glass_allowed(
        ws, fake_event("hub.left", person="p_o", device="d_new", cert={"x": 1}), None
    )
    assert not hubsign.break_glass_allowed(
        ws, fake_event("hub.repinned", person="p_o", device="d_new", cert={"x": 1}), None
    )


def test_break_glass_constant_is_the_one_shared_table_of_the_model():
    from orch.model import breakglass

    assert hubops.BREAK_GLASS is hubsign.BREAK_GLASS is breakglass.BREAK_GLASS
    # hub.joined is in the table only through the exact re-affirmation shape (V3-D29), never as any hub.joined
    assert "hub.left" in breakglass.event_types() and breakglass.BREAK_GLASS["hub.mode"].shape is not None


def test_signer_kind_is_fail_closed_for_every_kind_and_an_unknown_label():
    from orch.model.breakglass import SIGNER_KINDS, allowed, signer_kind

    ws = ws_model()
    kinds = {
        "owner_device_key": left(),
        "convenience": left(auth="convenience"),
        "hub": left(auth="hub"),
        "relay": left(signed_by="r"),
        "other": left(auth="some-future-label"),
    }
    for want, ev in kinds.items():
        assert signer_kind(ws, ev) == want, want
    assert signer_kind(ws, left(device="d_pk", cert={"x": 1})) == "other"  # hub.left is never "pk"
    ev = left()
    ev["actor"] = {**ev["actor"], "via": "c_1"}
    assert signer_kind(ws, ev) == "hub"
    ag = left()
    ag["actor"] = {"kind": "agent", "id": "x"}
    assert signer_kind(ws, ag) == "agent"
    assert signer_kind(ws, left(device="d_m")) == "other"  # another person's device
    assert signer_kind(ws, left(device="d_unknown")) == "other"  # a device the roster does not know
    assert not allowed("hub.leave", "other") and not allowed("hub.leave", "click") and not allowed("nope", "pk")
    assert (
        allowed("hub.leave", "owner_device_key")
        and allowed("hub.repin", "pk")
        and not allowed("hub.repin", "owner_device_key")
    )
    assert "other" not in SIGNER_KINDS


def test_a_second_hub_joined_is_refused_in_full_mode(human, served):
    full_joined(human, served)
    landed, error = append(served, [owner_event(served, "hub.joined")])
    assert error.code == "sign.hub_only" and landed == []


# -- I8: join in full leaves convenience off -------------------------------------------------------------------------


def test_a_full_join_needs_click_to_sign_off_first(human, served):
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    assert err(human.prepare_res("hub.join", join_args(mode="full"))) == "sign.hub_only"
    human.prepare("hub.join", join_args())  # selective is unchanged


def test_a_full_connect_needs_click_to_sign_off_and_convenience_on_is_refused_in_full_mode(
    human, served, stub, keystore
):
    full_joined(human, served)
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    assert err(served.client().req("hub.connect", connect_args(), idem=True)) == "sign.hub_only"
    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    entry = {"workspace_id": served.wid, "root": served.lab.root}
    with pytest.raises(HostError) as ei:
        convenience.refuse_turn_on(served.lab.state, entry, UID, person=served.lab.owner)
    assert ei.value.code == "sign.hub_only"


# -- I11: the unsigned safety verbs ----------------------------------------------------------------------------------


def test_grant_forget_is_not_a_signing_op_and_works_in_full_mode(human, served):
    full_joined(human, served)
    res = served.client().req("grant.forget", {"grant": "gr_" + "0" * 26}, idem=True)
    assert err(res) != "sign.hub_only"  # no mode check: it signs nothing, it only drops the runner's copy


# -- direct mode (no host): the log's half of the rule ---------------------------------------------------------------


def test_direct_mode_has_the_same_rule_from_the_log(tmp_path, monkeypatch):
    from orch.custody import select

    monkeypatch.setattr(select, "read_selection", lambda state, wid: None)  # passphrase custody
    from orch.ops.errors import OrchError
    from orch.ops.human import Human

    full = SimpleNamespace(
        hub={"status": "joined", "mode": "full", "hub_id": "h", "binding_seq": 1},
        **vars(ws_model()),
    )
    store = SimpleNamespace(refresh=lambda: None, state=SimpleNamespace(workspace=full))
    call = SimpleNamespace(ws=SimpleNamespace(state_dir=Path("/nonexistent-orch-state")))
    h = object.__new__(type("H", (Human,), {"store": store, "workspace_id": "w" * 32, "call": call}))
    with pytest.raises(OrchError) as ei:
        h._hub_only_check(None, fake_event("gate.approved", person="p_o", device="d_o"))()
    assert ei.value.code == "sign.hub_only"
    h._hub_only_check(None, left())()  # break-glass passes
    store.state.workspace.hub = {"status": "joined", "mode": "selective"}
    h._hub_only_check(None, fake_event("gate.approved", person="p_o", device="d_o"))()
    store.state.workspace.hub = {}
    h._hub_only_check(None, fake_event("gate.approved", person="p_o", device="d_o"))()


def test_the_catalog_code_is_global_and_has_a_closed_detail():
    from orch.ops.errors import ERRORS, GLOBAL_ERRORS

    assert "sign.hub_only" in ERRORS and "sign.hub_only" in GLOBAL_ERRORS
    e = hubsign.hub_only_error()
    assert e.error()["detail"] == {"reason": "hub"} and e.exit == 3 and not e.retryable
    assert HostError("sign.hub_only", detail={"reason": "evil"}).error().get("detail") is None


def test_hub_status_reports_the_signing_mode_for_the_dashboard(human, served, stub, keystore):
    from tests.host.dispatch_helpers import ok

    assert ok(served.client().req("hub.status", {}, idem=False))["data"]["signing"] == "standalone"
    full_joined(human, served)
    assert ok(served.client().req("hub.status", {}, idem=False))["data"]["signing"] == "unknown"
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert ok(served.client().req("hub.status", {}, idem=False))["data"]["signing"] == "full"


# -- L2-C11i-1: custody decides, not the label ---------------------------------------------------------------------


@pytest.fixture
def convenience_custody(monkeypatch):
    """The owner's key sits in convenience custody with the click-to-sign flag OFF (the 'Turn off' leaves it there)."""
    from orch.custody import select

    monkeypatch.setattr(select, "read_selection", lambda state, wid: {"custody": "convenience", "transition": None})


def test_a_full_join_needs_passphrase_custody_not_just_the_flag_off(human, served, convenience_custody):
    assert not convenience.enabled(served.lab.state, served.wid, UID)  # the flag is off
    assert err(human.prepare_res("hub.join", join_args(mode="full"))) == "sign.hub_only"
    human.prepare("hub.join", join_args())  # selective is unchanged


def test_hub_mode_to_full_needs_passphrase_custody(human, served, monkeypatch):
    human.run("hub.join", join_args())
    from orch.custody import select

    monkeypatch.setattr(select, "read_selection", lambda state, wid: {"custody": "convenience", "transition": None})
    assert err(human.prepare_res("hub.mode", {"hub_card": copy.deepcopy(CARD), "mode": "full"})) == "sign.hub_only"


def test_a_full_connect_needs_passphrase_custody(human, served, stub, keystore, monkeypatch):
    full_joined(human, served)
    from orch.custody import select

    monkeypatch.setattr(select, "read_selection", lambda state, wid: {"custody": "convenience", "transition": None})
    assert err(served.client().req("hub.connect", connect_args(), idem=True)) == "sign.hub_only"


def test_an_unreadable_custody_setting_counts_as_convenience(human, served, monkeypatch):
    from orch.custody import select

    def boom(state, wid):
        raise OSError("unreadable")

    monkeypatch.setattr(select, "read_selection", boom)
    assert err(human.prepare_res("hub.join", join_args(mode="full"))) == "sign.hub_only"


def test_a_transition_to_convenience_counts_as_convenience(human, served, monkeypatch):
    from orch.custody import select

    monkeypatch.setattr(
        select, "read_selection", lambda state, wid: {"custody": "passphrase", "transition": {"to": "convenience"}}
    )
    assert err(human.prepare_res("hub.join", join_args(mode="full"))) == "sign.hub_only"


def test_moving_the_key_into_convenience_custody_is_refused_in_full_mode(human, served, stub, keystore):
    entry = {"workspace_id": served.wid, "root": served.lab.root}
    convenience.refuse_full_mode_entry(served.lab.state, entry, UID)  # standalone: allowed
    full_joined(human, served)
    with pytest.raises(HostError) as ei:
        convenience.refuse_full_mode_entry(served.lab.state, entry, UID)
    assert ei.value.code == "sign.hub_only"


def test_break_glass_is_refused_while_custody_is_convenience(human, served, stub, keystore, monkeypatch):
    full_joined(human, served)
    from orch.custody import select

    monkeypatch.setattr(select, "read_selection", lambda state, wid: {"custody": "convenience", "transition": None})
    # any process of the owner's UID could sign a leave labelled passphrase: refused at the choke point
    before = log_len(served)
    landed, error = append(served, [owner_event(served, "hub.left")])
    assert landed == [] and error.code == "sign.hub_only" and "convenience custody" in error.message
    assert err(human.prepare_res("hub.leave", {})) == "sign.hub_only"
    assert log_len(served) == before


def test_direct_mode_refuses_break_glass_while_custody_is_convenience(convenience_custody, tmp_path):
    from orch.ops.errors import OrchError
    from orch.ops.human import Human

    full = SimpleNamespace(
        hub={"status": "joined", "mode": "full", "hub_id": "h", "binding_seq": 1}, **vars(ws_model())
    )
    store = SimpleNamespace(refresh=lambda: None, state=SimpleNamespace(workspace=full))
    call = SimpleNamespace(ws=SimpleNamespace(state_dir=tmp_path))
    h = object.__new__(type("H", (Human,), {"store": store, "workspace_id": "w" * 32, "call": call}))
    with pytest.raises(OrchError):
        h._hub_only_check(None, left())()


def test_another_members_device_row_of_the_matrix(human, served, stub, keystore):
    full_joined(human, served)
    ev = fake_event("gate.approved", person="p_" + "9" * 32, device="d_" + "9" * 32)
    landed, error = append(served, [ev])
    assert landed == [] and error.code == "sign.hub_only"
