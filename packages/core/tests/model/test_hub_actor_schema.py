"""C3b task 1 (ticket-format section 15.1): the schemas of a hub-signed (custodial) line and of ``hub.pinned``."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from orch import schema
from orch.schema import SchemaError
from tests.hub.vec import VECTORS_DIR

X: dict[str, Any] = json.loads((VECTORS_DIR / "c3b_ledger.json").read_text())
CUSTODIAL = X["lines"]["custodial_gate_approved"]["event"]
PINNED = {**X["lines"]["hub_pinned_snapshot"]["event"], "id": "01J9ZP0000000000000000PNN1"}
# the vector's own pin id (...PIN1) is not a ULID (I is not in the alphabet); the line bytes tests do not validate it
USER = CUSTODIAL["actor"]["id"]


def fresh(e: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(e)


def ok(e: dict[str, Any], log: str) -> None:
    schema.validate("event", e, log=log)


def refused(e: dict[str, Any], log: str, path: str = "") -> None:
    with pytest.raises(SchemaError) as exc:
        schema.validate("event", e, log=log)
    if path:
        assert exc.value.path == path, exc.value


def test_the_vector_lines_validate() -> None:
    ok(fresh(CUSTODIAL), "ticket")
    ok(fresh(PINNED), "workspace")


def test_a_device_actor_with_auth_hub_is_refused() -> None:
    e = fresh(CUSTODIAL)
    e["actor"] = {"kind": "person", "id": "p_" + "1" * 32, "device": "d_" + "2" * 32}
    refused(e, "ticket")


def test_a_via_actor_with_another_auth_is_refused_and_so_are_the_hub_fields() -> None:
    e = fresh(CUSTODIAL)
    e["auth"] = "secure-enclave-unlocked"
    refused(e, "ticket")  # the via actor and the hub fields belong to auth "hub" only
    e = fresh(CUSTODIAL)
    del e["actor"]["via"]
    e["actor"]["device"] = "d_" + "2" * 32
    e["actor"]["id"] = "p_" + "1" * 32
    e["auth"] = "convenience"
    refused(e, "ticket")  # device shape plus hub fields


@pytest.mark.parametrize(
    "field", ["channel", "channel_factor", "hub_id", "snapshot_version", "channel_cert", "uk_cert", "channel_proof"]
)
def test_every_hub_field_is_required_with_auth_hub(field: str) -> None:
    e = fresh(CUSTODIAL)
    del e[field]
    refused(e, "ticket")


def test_hub_actor_shape_is_exact() -> None:
    for mut in (
        lambda a: a.update(device="d_" + "2" * 32),  # a via actor never has a device
        lambda a: a.update(id="u_xyz"),
        lambda a: a.update(id="p_" + "1" * 32),  # a via actor is a hub user
        lambda a: a.update(via="NOT-HEX"),
        lambda a: a.pop("via"),
    ):
        e = fresh(CUSTODIAL)
        mut(e["actor"])
        refused(e, "ticket")


def test_unknown_channel_factor_and_item_hash_shape() -> None:
    e = fresh(CUSTODIAL)
    e["channel_factor"] = "sms"
    refused(e, "ticket", "/channel_factor")
    e = fresh(CUSTODIAL)
    e["item_hash"] = "sha256:" + "a" * 64
    ok(e, "ticket")  # selective mode only is the model's rule; the schema allows the field with auth hub
    e["item_hash"] = "nope"
    refused(e, "ticket", "/item_hash")


def test_item_hash_without_auth_hub_is_refused() -> None:
    e = fresh(X["lines"]["custodial_gate_approved"]["event"])
    for k in ("channel", "channel_factor", "hub_id", "snapshot_version", "channel_cert", "uk_cert", "channel_proof"):
        del e[k]
    e["actor"] = {"kind": "person", "id": "p_" + "1" * 32, "device": "d_" + "2" * 32}
    e["auth"] = "convenience"
    ok(e, "ticket")
    e["item_hash"] = "sha256:" + "a" * 64
    refused(e, "ticket")


def test_hub_pinned_carries_exactly_one_signed_hub_object() -> None:
    ok(fresh(PINNED), "workspace")
    e = fresh(PINNED)
    e["object"]["o"]["kind"] = "hub.nothing"
    refused(e, "workspace")
    e = fresh(PINNED)
    e["actor"] = {"kind": "person", "id": "p_" + "1" * 32, "device": "d_" + "2" * 32}
    refused(e, "workspace")  # the host appends it
    e = fresh(PINNED)
    del e["hub_id"]
    refused(e, "workspace")
    refused(fresh(PINNED), "ticket")  # workspace log only


def test_person_references_accept_a_hub_user() -> None:
    drop = {"gate", "gate_gen", "hash", "policy_hash"}
    e = {k: v for k, v in fresh(CUSTODIAL).items() if k not in drop}
    e.update(type="people.changed", role="watchers", add=[USER], remove=[])
    ok(e, "ticket")
    e["add"] = ["x_" + "1" * 32]
    refused(e, "ticket")
    e = {k: v for k, v in fresh(CUSTODIAL).items() if k not in drop}
    e.update(type="visibility.changed", visibility={"restricted": [USER]})
    ok(e, "ticket")


def test_the_common_definitions() -> None:
    schema.validate_def("hubUserId", USER)
    for bad in ("p_" + "1" * 32, "u_" + "1" * 31, "u_" + "G" * 32):
        with pytest.raises(SchemaError):
            schema.validate_def("hubUserId", bad)
    schema.validate_def("personRef", USER)
    schema.validate_def("personRef", "p_" + "1" * 32)
    schema.validate_def("auth", "hub")
