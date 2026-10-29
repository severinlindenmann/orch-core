"""Company secrets (M3 #1431-#1433, S-1): the committed ``vectors_org_secrets.json`` is what the generator builds, and
the hub's mirror (``orch_hub.contract.org_secrets``) replays the WHOLE file, failing on any case it does not handle."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec

from orch_hub.contract import org_secrets as O

VEC = Path(__file__).resolve().parents[3] / "contracts" / "hub"
DOC = json.loads((VEC / "vectors_org_secrets.json").read_text())
FIELDS = ("hub_id", "workspace_id", "secret_id", "version", "wxk_version", "expires_ms", "name")


def _priv(scalar_hex: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int(scalar_hex, 16), ec.SECP256R1())


KEYS = {"wxk": _priv(DOC["keys"]["wxk_scalar"]), "wrong": _priv(DOC["keys"]["wrong_scalar"])}
EPH = _priv(DOC["keys"]["eph_scalar"])


def test_file_is_generated() -> None:
    spec = importlib.util.spec_from_file_location("make_vectors_org_secrets", VEC / "make_vectors_org_secrets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert json.loads(json.dumps(mod.build())) == DOC


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
            rest = [a[k] for k in FIELDS] + [a["value"]]
            return {"sealed": O._seal_with_ephemeral(EPH, bytes.fromhex(DOC["keys"]["wxk_pub"]), *rest).hex()}
        if c["kind"] == "open":
            return {
                "value": O.open_org_secret(
                    KEYS[a["key"]], *[a[k] for k in FIELDS], bytes.fromhex(a["sealed"]), a["now_ms"], a["issued_ms"]
                )
            }
    except O.OrgSecretRefused as e:
        return {"error": e.code}
    raise AssertionError(f"unhandled kind {c['kind']!r}")


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
        O.PURPOSE,
        O.VALUE_MIN,
        O.VALUE_MAX,
        O.EXPIRY_MAX_MS,
    )
