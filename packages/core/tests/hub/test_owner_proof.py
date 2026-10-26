"""#1198 C8: the production builder of the owner proof (protocol 9.6) against the vendored vectors and the checks.

The vectors replay every check (test_vectors_hub); here the builder the signer (C10) will call: it makes exactly the
vector's bytes, its proof passes the host check, and it refuses what a verifier would refuse."""

from __future__ import annotations

import copy

import pytest

from orch.canon.jcs import dumps as cj
from orch.crypto import suite as S
from orch.hubclient import wire
from orch.hubclient.errors import HubRefused

from .vec import V, private_key, pub

PROOF = V["objects"]["owner_proof_join_a"]
OBJ = PROOF["o"]


def _build(**kw):
    args = {
        "workspace_id": OBJ["workspace_id"], "pk_pub": pub("pk_alice"), "op": OBJ["op"], "payload": OBJ["payload"],
        "request_id": OBJ["request_id"], "issued_ms": OBJ["issued_ms"], "expires_ms": OBJ["expires_ms"], "dev": True,
        "sign": lambda msg: S.sign(private_key("pk_alice"), msg),
    }  # fmt: skip
    return wire.build_owner_proof(**{**args, **kw})


def test_builder_makes_the_vector_bytes_and_passes_the_host_check():
    got = _build()
    assert got["o"] == OBJ
    entry = next(e for e in V["signatures"] if e["name"] == "owner_proof")
    assert wire.SIG_OWNER_PROOF + cj(got["o"]) == bytes.fromhex(entry["signed_hex"])
    o = wire.check_owner_proof(
        got, pk_pub=pub("pk_alice"), workspace_id=OBJ["workspace_id"], op="hub.join", payload=OBJ["payload"],
        request_id=OBJ["request_id"], now_ms=V["now_ms"], dev_mode=True,
    )  # fmt: skip
    assert o["person_id"] == V["ids"]["person_alice"]


@pytest.mark.parametrize(
    "kw",
    [
        {"expires_ms": OBJ["issued_ms"] + 600_001},
        {"expires_ms": OBJ["issued_ms"]},
        {"op": "hub.mode"},  # a selective payload is never a mode switch
        {"op": "hub.repin"},
        {"request_id": "hr_x"},
        {"payload": {**OBJ["payload"], "owner_proof": None}},
        {"payload": {**OBJ["payload"], "aliases": list(reversed(OBJ["payload"]["aliases"] * 2))}},
    ],
)
def test_builder_refuses_what_a_verifier_refuses(kw):
    with pytest.raises(HubRefused) as e:
        _build(**kw)
    assert e.value.code == "malformed"


def test_a_device_key_signature_is_not_a_proof():
    forged = _build(sign=lambda msg: S.sign(private_key("dk_alice_mac.sig"), msg))
    with pytest.raises(HubRefused) as e:
        wire.check_owner_proof(
            forged, pk_pub=pub("pk_alice"), workspace_id=OBJ["workspace_id"], op="hub.join", payload=OBJ["payload"],
            request_id=OBJ["request_id"], now_ms=V["now_ms"], dev_mode=True,
        )  # fmt: skip
    assert e.value.code == "proof_invalid"


def test_recorded_check_requires_the_proof_whatever_the_auth():
    payload = {**copy.deepcopy(OBJ["payload"]), "owner_proof": None}
    with pytest.raises(HubRefused) as e:
        wire.check_recorded_owner_proof(
            "hub.joined", payload, at_ms=V["now_ms"], prior=None, workspace_id=OBJ["workspace_id"],
            actor_person_id=OBJ["person_id"], pk_pub=pub("pk_alice"), dev_mode=True,
        )  # fmt: skip
    assert e.value.code == "proof_missing"


def test_one_user_id_never_names_two_people():
    twice = copy.deepcopy(OBJ["payload"])
    twice["aliases"] = sorted(
        [twice["aliases"][0], {"person_id": "00" * 16, "user_id": twice["aliases"][0]["user_id"]}],
        key=lambda a: a["person_id"],
    )
    with pytest.raises(HubRefused) as e:
        _build(payload=twice)
    assert e.value.code == "malformed"
