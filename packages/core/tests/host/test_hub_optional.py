# ruff: noqa: F811
"""A1 (D83, ~/orch-dev/state/a1-hub-optional-design.md): a local signature is valid in EVERY hub mode, with the hub
reachable or not. The hub is an additional signer. Invariants I1 (local signing in every mode), I5 (leave and the
signing mode follow the log), and the mode read the dashboard shows."""

from __future__ import annotations

import json

import pytest

from orch.host import hubops, hubsign
from orch.host import human as hh
from tests.host import human_helpers

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
    assert hubsign.SEALS_TO_HUB >= {"unknown", "full"}


def test_signing_mode_full_connected_is_full_and_a_foreign_hub_json_is_unknown(human, served, stub, keystore):
    full_joined(human, served, connect=True)
    assert hubops.signing_mode(served.service) == "full"
    with hubops._MU:
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


# -- I1: a local signature is valid in every mode ------------------------------------------------------------------


SIGNERS = {
    "owner key": dict(signed_by=None, auth="passphrase"),
    "convenience": dict(signed_by=None, auth="convenience"),
}


@pytest.mark.parametrize("mode", ["unknown", "full"])
def test_full_mode_takes_a_local_signature_from_the_roster(human, served, stub, keystore, mode):
    full_joined(human, served, connect=(mode == "full"))
    assert hubops.signing_mode(served.service) == mode
    key = served.ticket()[1]
    _, done, _ = human.run("log", {"ref": key, "text": "signed locally in " + mode})
    assert done["data"] is not None


def test_a_request_prepared_before_the_join_lands_after_it(human, served):
    key = served.ticket()[1]
    prepared = human.prepare("log", {"ref": key, "text": "prepared standalone"})
    signed = human.sign(prepared)
    full_joined(human, served)  # the join lands between prepare and submit
    res = human.submit_res(prepared["request"]["id"], signed.events)
    assert res["ok"] is True


def test_the_prepare_and_the_hand_off_are_both_open_in_full_mode(human, served):
    key = served.ticket()[1]
    full_joined(human, served)
    assert human.prepare_res("log", {"ref": key, "text": "x"})["ok"] is True
    assert human.prepare_res("log", {"ref": key, "text": "x"}, handoff=True)["ok"] is True


def test_leave_works_in_full_mode_and_unknown(human, served, stub, keystore):
    full_joined(human, served)
    assert hubops.signing_mode(served.service) == "unknown"
    human.run("hub.leave", {})
    assert hubops.signing_mode(served.service) == "standalone"


def test_leave_works_with_the_hub_up_and_connected(human, served, stub, keystore):
    full_joined(human, served, connect=True)
    assert hubops.signing_mode(served.service) == "full"
    human.run("hub.leave", {})
    assert hubops.signing_mode(served.service) == "standalone"


def test_the_removed_catalog_code_is_gone():
    from orch.ops.errors import ERRORS

    assert "sign.hub_only" not in ERRORS


def test_hub_status_reports_the_signing_mode_for_the_dashboard(human, served, stub, keystore):
    from tests.host.dispatch_helpers import ok

    assert ok(served.client().req("hub.status", {}, idem=False))["data"]["signing"] == "standalone"
    full_joined(human, served)
    assert ok(served.client().req("hub.status", {}, idem=False))["data"]["signing"] == "unknown"
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert ok(served.client().req("hub.status", {}, idem=False))["data"]["signing"] == "full"
