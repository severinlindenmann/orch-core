"""Shared builders for the marketplace tests: a signed official index and a fake fetch."""

from __future__ import annotations

import json
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from orch.crypto.encoding import b64u
from orch.market import constants
from tests.addons import market_ref as ref

OFFICIAL = constants.OFFICIAL_URL
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
KEY = ec.generate_private_key(ec.SECP256R1())
KEY_B64 = b64u(KEY.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))
ENTRY = {
    "id": "standup", "version": "1.0.0", "summary": "Daily standups",
    "source": {"git": "https://github.com/acme/standup", "subdir": "pkg"},
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "package_sha256": "sha256:d2c8be3f4051ae57c32bb2c2ff6b46bff268f1e2626217d565bed43acc123595",
    "capabilities": ["network"], "min_orch": "2.0.0",
}  # fmt: skip


def index(
    version: int = 1,
    *,
    addons: list[dict[str, Any]] | None = None,
    signed: bool = True,
    expires: int = 4_000_000_000,
    revoked: list[str] | None = None,
    key=None,
    url: str = OFFICIAL,
    title: str = "The official orch add-on marketplace",
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "format": "orch.market/1", "name": "orch-marketplace", "title": title, "version": version,
        "source": {"git": url}, "addons": addons if addons is not None else [dict(ENTRY)],
        "expires_at": expires,
    }  # fmt: skip
    if revoked:
        doc["revoked"] = revoked
    if signed:
        ref.sign_index(doc, key or KEY, KEY_B64)
    return doc


def raw(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True).encode()
