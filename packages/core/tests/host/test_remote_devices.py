"""R2 T2a (#1590): the remote device list the host exports and reads (hub contract 10.2c "The device list").

The model is the authority; ``remote/devices.json`` is a WSK-signed export of it (0600, version = the model's device
version). The reader's rules replay the shared ``device_list_*`` and ``device_lookup`` vectors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orch.crypto.encoding import unb64u
from orch.host import hubkeys, remote
from orch.host.custody import read_json
from orch.host.remote import devices
from orch.hubclient.errors import HubRefused
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.test_remote_ops import add_device, call, clock_ms, model, run_on, turn_on

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

CONTRACT = Path(__file__).resolve().parents[4] / "contracts" / "hub"
DOC = json.loads((CONTRACT / "vectors_remote.json").read_text())
PUB = {n: unb64u(v["pub"], 65) for n, v in DOC["keys"]["p256"].items()}


def exported(served) -> dict[str, Any]:
    return read_json(devices.list_path(served.service), served.service.host_uid)


def test_device_list_export_wsk_signed_0600_version_rises(human, served):
    turn_on(human)
    assert not devices.list_path(served.service).exists()  # version 0: nothing to say yet
    wsk = hubkeys.wsk_pub(served.service)
    wid = served.service.workspace_id
    d1 = add_device(human, served, 1)
    path = devices.list_path(served.service)
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    first = devices.check_list(exported(served), wsk, wid, 0)  # the reader's rules accept it
    assert first["version"] == 1 and [d["device_id"] for d in first["devices"]] == [d1]
    assert first["issued_ms"] == clock_ms(served) and "fingerprint" not in first["devices"][0]
    d2 = add_device(human, served, 2)
    second = devices.check_list(exported(served), wsk, wid, first["version"])
    assert second["version"] == 2 and [d["device_id"] for d in second["devices"]] == sorted([d1, d2])
    ok(call(served, "remote.device_remove", {"device_id": d1}))
    third = devices.check_list(exported(served), wsk, wid, second["version"])
    assert third["version"] == 3 and [d["device_id"] for d in third["devices"]] == [d2]
    with pytest.raises(HubRefused) as e:  # a reader that holds version 3 refuses the older one
        devices.check_list({**exported(served), "o": {**exported(served)["o"], "version": 2}}, wsk, wid, 3)
    assert e.value.code in ("stale_version", "bad_signature")
    assert model(served)["device_version"] == 3


def test_a_file_older_than_the_model_is_replaced_never_trusted(human, served):
    turn_on(human)
    add_device(human, served, 1)
    path = devices.list_path(served.service)
    path.write_text('{"o":{},"sig":"x"}')  # a forged or stale file
    path.chmod(0o600)
    run_on(served, remote.attach, served.service)  # host start
    devices.check_list(exported(served), hubkeys.wsk_pub(served.service), served.service.workspace_id, 1)
    # the gate reads the model: a forged file changes nothing about who is listed
    assert len(model(served)["devices"]) == 1


def test_a_replayed_device_add_after_remove_stays_refused_through_the_ops(human, served):
    turn_on(human)
    pid = "rp_" + f"{9:032x}"
    from orch.host.remote import enrollment
    from tests.host.test_remote_ops import Pending_for

    Pending_for(served, pid, 9)
    prepared = human.prepare("remote.device_confirm", {"pending": pid, "level_cap": "work", "days": 30})
    signed = human.sign(prepared)
    done = ok(human.submit_res(prepared["request"]["id"], signed.events))
    dev = done["data"]["device_id"]
    ok(call(served, "remote.device_remove", {"device_id": dev}))
    assert model(served)["devices"] == {}
    again = human.submit_res(
        prepared["request"]["id"], signed.events
    )  # delivered again: the host answers from its record
    assert "error" not in again or again["ok"] is False
    assert model(served)["devices"] == {}
    types = [e["type"] for e, _ in served.service.store.events_after("workspace", 0, 1 << 30)]
    assert types.count("remote.device_added") == 1 and model(served)["devices"] == {}  # the model's rule is T1's
    assert enrollment is not None


# --- vectors against the real reader ---

LISTS = [c for c in DOC["cases"] if c["check"] == "device_list"]


@pytest.mark.parametrize("c", LISTS, ids=[c["name"] for c in LISTS])
def test_device_list_vector(c):
    a = c["args"]
    try:
        devices.check_list(a["signed"], PUB[a["wsk"]], a["workspace_id"], a["held_version"])
        got: dict[str, Any] = {"ok": True}
    except HubRefused as e:
        got = {"error": e.code}
    assert got == c["expect"], c.get("note")


LOOKUPS = [c for c in DOC["cases"] if c["check"] == "device_lookup"]


@pytest.mark.parametrize("c", LOOKUPS, ids=[c["name"] for c in LOOKUPS])
def test_device_lookup_vector(c):
    a = c["args"]
    as_model = {
        "devices": {d["device_id"]: {k: v for k, v in d.items() if k != "device_id"} for d in a["list"]["devices"]}
    }
    hit = devices.lookup(as_model, a["device_id"], a["now_ms"], {d["person"] for d in a["list"]["devices"]})
    got = {"ok": hit["device_id"]} if hit else {"error": "remote.device"}
    assert got == c["expect"], c.get("note")
