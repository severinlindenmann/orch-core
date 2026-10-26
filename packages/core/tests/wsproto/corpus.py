"""The orch-relay protocol corpus (11A.10): ``contracts/relay/vectors_v2.json`` and ``contracts/relay/http_fixtures/``.
Loader helpers only; the drift guard is ``test_corpus.py``. The corpus lives once, in ``contracts/``; relay, core and
the phone read it from there."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from orch.crypto import P256_N

DIR = Path(__file__).resolve().parents[4] / "contracts" / "relay"
FIXTURES = DIR / "http_fixtures"
VECTORS_PATH = DIR / "vectors_v2.json"


def vectors_bytes() -> bytes:
    return VECTORS_PATH.read_bytes()


VECTORS = json.loads(vectors_bytes())
S2 = VECTORS["suites"]["2"]
KEYS = S2["keys"]
IDS = S2["ids"]


def priv(name: str):
    """The vector key ``name`` (seed mapping ``seed mod (n-1) + 1``, test-only)."""
    d = int.from_bytes(bytes.fromhex(KEYS[name]["seed"]), "big") % (P256_N - 1) + 1
    return ec.derive_private_key(d, ec.SECP256R1())


def pub(name: str) -> bytes:
    return bytes.fromhex(KEYS[name]["pub"])


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def case(name: str, case_name: str) -> dict[str, Any]:
    for c in fixture(name)["cases"]:
        if c["name"] == case_name:
            return c
    raise KeyError(f"{name}:{case_name}")


def request_body(req: dict[str, Any]) -> bytes | None:
    """The exact request bytes of a fixture case (README: ``cj(json)``, decoded ``body_b64u`` or ``body_recipe``)."""
    from orch import canon

    if "json" in req:
        return canon.dumps(req["json"])
    if "body_b64u" in req:
        return _b64(req["body_b64u"])
    if "body_recipe" in req:
        r = req["body_recipe"]
        return _b64(r["prefix_b64u"]) + bytes([r["byte"]]) * r["count"] + _b64(r["suffix_b64u"])
    return None


def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
