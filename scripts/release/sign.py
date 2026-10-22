#!/usr/bin/env python3
"""Sign a release payload (manifest or catalog) into an `orch.release-envelope/1` (CONTRACT.md §2).

    ORCH_RELEASE_SIGNING_KEY=<base64 32-byte Ed25519 seed> sign.py --purpose core --payload manifest.json \
        --key-id rk-3 --key-cert key_cert.json --revocations revocations.json --out envelope.json

The private key is read from an environment variable ONLY (`--key-env` names the variable; there is no file or
value argument) and is never printed. `key_cert.json` and `revocations.json` are the root-signed objects that
`rootkey.py certify` and `rootkey.py revoke` write. Output is canonical JSON.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manifest import b64, jcs, need  # noqa: E402

DEFAULT_KEY_ENV = "ORCH_RELEASE_SIGNING_KEY"
DOMAIN = {"core": b"orch-release/1\n", "addon-catalog": b"orch-addon-catalog/1\n"}
PAYLOAD_FORMAT = {"core": "orch.release/1", "addon-catalog": "orch.addon-catalog/1"}


def load_key(env_name: str) -> Ed25519PrivateKey:
    """The Ed25519 key from the environment variable `env_name` (base64 of the 32-byte seed). Errors never echo it."""
    raw = os.environ.get(env_name)
    need(bool(raw), f"environment variable {env_name} is not set")
    try:
        seed = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{env_name} is not base64") from None
    need(len(seed) == 32, f"{env_name} must be the base64 of a 32-byte Ed25519 seed")
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_bytes(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def sign_envelope(
    payload: bytes, *, purpose: str, key: Ed25519PrivateKey, key_id: str, key_cert: dict, revocations: dict
) -> bytes:
    need(purpose in DOMAIN, f"bad purpose: {purpose!r}")
    doc = json.loads(payload)
    need(doc.get("format") == PAYLOAD_FORMAT[purpose], f"payload is not a {PAYLOAD_FORMAT[purpose]} document")
    need(jcs(doc) == payload, "payload is not canonical (build it with manifest.py)")
    cert = json.loads(base64.b64decode(key_cert["cert"], validate=True))
    need(cert["key_id"] == key_id, "key_cert is for another key id")
    need(cert["purpose"] == purpose, "key_cert purpose does not match --purpose")
    need(base64.b64decode(cert["public_key"], validate=True) == public_bytes(key), "key_cert is for another key")
    need(
        doc["channel"] in cert["channels"] or ("test-*" in cert["channels"] and doc["channel"].startswith("test-")),
        "the certificate does not allow this channel",
    )
    envelope: dict[str, Any] = {
        "format": "orch.release-envelope/1",
        "payload": b64(payload),
        "sig": {"alg": "ed25519", "key_id": key_id, "value": b64(key.sign(DOMAIN[purpose] + payload))},
        "key_cert": key_cert,
        "revocations": revocations,
    }
    return jcs(envelope)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--purpose", choices=sorted(DOMAIN), required=True)
    ap.add_argument("--payload", type=Path, required=True)
    ap.add_argument("--key-id", required=True)
    ap.add_argument("--key-env", default=DEFAULT_KEY_ENV, help="NAME of the environment variable holding the key")
    ap.add_argument("--key-cert", type=Path, required=True)
    ap.add_argument("--revocations", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        key = load_key(a.key_env)
        out = sign_envelope(
            a.payload.read_bytes(),
            purpose=a.purpose,
            key=key,
            key_id=a.key_id,
            key_cert=json.loads(a.key_cert.read_text(encoding="utf-8")),
            revocations=json.loads(a.revocations.read_text(encoding="utf-8")),
        )
    except (ValueError, KeyError, OSError) as exc:
        print(f"sign: {exc}", file=sys.stderr)
        return 1
    a.out.write_bytes(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
