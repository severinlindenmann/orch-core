"""Company secrets (M3 #1431-#1433, S-1): core replays the WHOLE of ``contracts/hub/vectors_org_secrets.json`` and
fails on any case it does not handle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from orch.crypto import sealing, suite
from orch.hubclient import orgsecrets as O

VEC = Path(__file__).resolve().parents[3] / "contracts" / "hub" / "vectors_org_secrets.json"
DOC = json.loads(VEC.read_text())
KEYS = {
    "wxk": suite.private_key_from_scalar(int(DOC["keys"]["wxk_scalar"], 16)),
    "wrong": suite.private_key_from_scalar(int(DOC["keys"]["wrong_scalar"], 16)),
}
EPH = suite.private_key_from_scalar(int(DOC["keys"]["eph_scalar"], 16))
FIELDS = ("hub_id", "workspace_id", "secret_id", "version", "wxk_version", "expires_ms", "name")


REQUIRED_CASES = {
    "open fails with changed hub_id",
    "open fails with changed workspace_id",
    "open fails with changed secret_id",
    "open fails with changed version",
    "open fails with changed wxk_version",
    "open fails with changed expires_ms",
    "open fails with changed name",
    "open fails with a shorter name",
    "open fails for a wrap sealed under purpose hub-share",
    "open refuses a sealed short value",
    "open refuses a sealed control character value",
    "open refuses a sealed non-ASCII value",
    "open refuses a sealed leading space value",
    "open refuses a sealed trailing space value",
    "open refuses a sealed long value",
    "open refuses more than 366 days after issue",
    "open ok exactly 366 days after issue",
    "open refuses one ms past 366 days",
}


def _run(c: dict) -> dict:
    a = c["args"]
    try:
        if c["kind"] == "aad":
            return {
                "hex": O.wrap_aad_extra(
                    a["hub_id"], a["workspace_id"], a["wxk_version"], a["expires_ms"], a["name"]
                ).hex()
            }
        if c["kind"] == "value":
            return {"problem": O.value_problem(a["value"])}
        if c["kind"] == "seal":
            wxk_pub = bytes.fromhex(DOC["keys"]["wxk_pub"])
            rest = [a[k] for k in FIELDS] + [a["value"]]
            sealed = sealing._seal_with_ephemeral(EPH, wxk_pub, *O.wrap_inputs(*rest))
            return {"sealed": sealed.hex()}
        if c["kind"] == "open":
            value = O.open_org_secret(
                KEYS[a["key"]], *[a[k] for k in FIELDS], bytes.fromhex(a["sealed"]), a["now_ms"], a["issued_ms"]
            )
            return {"value": value}
    except O.OrgSecretRefused as e:
        return {"error": e.code}
    raise AssertionError(f"unhandled kind {c['kind']!r}")


def test_required_cases_are_in_the_file() -> None:
    """Deleting a security-relevant case from the vector file must turn this replay red."""
    missing = REQUIRED_CASES - {c["name"] for c in DOC["cases"]}
    assert not missing, sorted(missing)


def test_every_case_is_replayed() -> None:
    handled = 0
    for c in DOC["cases"]:
        assert _run(c) == c["expect"], c["name"]
        handled += 1
    assert handled == len(DOC["cases"]) >= 60
    assert {c["kind"] for c in DOC["cases"]} == {"aad", "value", "seal", "open"}


def test_constants_match_the_file() -> None:
    k = DOC["consts"]
    assert (k["purpose"], k["value_min"], k["value_max"], k["expiry_max_ms"]) == (
        "org-secret",
        O.VALUE_MIN,
        O.VALUE_MAX,
        O.EXPIRY_MAX_MS,
    )
    assert "org-secret" in sealing.SEAL_PURPOSES


def test_fresh_seal_round_trips_and_refuses_a_bad_value() -> None:
    base = next(c["args"] for c in DOC["cases"] if c["name"] == "seal ok")
    priv = ec.generate_private_key(ec.SECP256R1())
    args = [base[k] for k in FIELDS]
    sealed = O.seal_org_secret(suite.public_bytes(priv), *args, "a-fine-value")
    assert sealed != O.seal_org_secret(suite.public_bytes(priv), *args, "a-fine-value")  # fresh ephemeral per call
    assert O.open_org_secret(priv, *args, sealed, base["expires_ms"] - 1, base["expires_ms"] - 1000) == "a-fine-value"
    with pytest.raises(O.OrgSecretRefused):
        O.seal_org_secret(suite.public_bytes(priv), *args, "short")
