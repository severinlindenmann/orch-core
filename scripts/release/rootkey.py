#!/usr/bin/env python3
"""Root-key operations for the key ceremony (CONTRACT.md §2): `certify` and `revoke`.

    pass-cli run --env-file release/keys/root.env -- python3 -I scripts/release/rootkey.py certify \
        --root-key-id root-1 --key-id rk-3 --public-key <b64 32> --serial 3 --purpose core --channels main \
        --not-before … --not-after … --out key_cert.json
    pass-cli run --env-file release/keys/root.env -- python3 -I scripts/release/rootkey.py revoke \
        --root-key-id root-1 --min-serial 2 --revoked 4 --initial --issued-at … --out revocations.json

(`root.env` holds only a secret REFERENCE to the vault item, never the key; it is not committed.)
The root key comes from an environment variable ONLY (`--root-key-env` names it) and is never printed; the ceremony
runs these through `pass-cli run`. The variable is removed from the process environment right after
the key is loaded. The outputs are the `key_cert` / `revocations` objects of the envelope.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent))
from manifest import CHANNEL, TIME, b64, jcs, need  # noqa: E402
from sign import load_key  # noqa: E402

DEFAULT_ROOT_ENV = "ORCH_ROOT_SIGNING_KEY"
CERT_DOMAIN = b"orch-release-key/1\n"
REVOCATIONS_DOMAIN = b"orch-revocations/1\n"
KEY_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
PURPOSES = ("core", "addon-catalog")
MAX_INT = 2**53 - 1


def build_cert(
    *, key_id: str, public_key: bytes, serial: int, purpose: str, channels: list[str], not_before: str, not_after: str
) -> dict[str, Any]:
    need(KEY_ID.fullmatch(key_id) is not None, f"bad key id: {key_id!r}")
    need(len(public_key) == 32, "public key must be 32 bytes")
    need(type(serial) is int and 1 <= serial <= MAX_INT, f"bad serial: {serial!r}")
    need(purpose in PURPOSES, f"bad purpose: {purpose!r}")
    need(len(channels) == len(set(channels)) and bool(channels), "channels must be a non-empty unique list")
    for ch in channels:
        need(CHANNEL.fullmatch(ch) is not None or ch == "test-*", f"bad channel: {ch!r}")
    for label, value in (("not_before", not_before), ("not_after", not_after)):
        need(TIME.fullmatch(value) is not None, f"bad {label}: {value!r}")
    need(not_before < not_after, "not_after must be after not_before")
    return {
        "format": "orch.release-key/1",
        "key_id": key_id,
        "public_key": b64(public_key),
        "serial": serial,
        "purpose": purpose,
        "channels": channels,
        "not_before": not_before,
        "not_after": not_after,
    }


def build_revocations(*, min_serial: int, revoked: list[int], issued_at: str) -> dict[str, Any]:
    need(type(min_serial) is int and 1 <= min_serial <= MAX_INT, f"bad min_serial: {min_serial!r}")
    need(all(type(n) is int and 1 <= n <= MAX_INT for n in revoked), "revoked must be integers >= 1")
    need(TIME.fullmatch(issued_at) is not None, f"bad issued_at: {issued_at!r}")
    return {
        "format": "orch.revocations/1",
        "min_serial": min_serial,
        "revoked": sorted(set(revoked)),
        "issued_at": issued_at,
    }


def check_successor(new: dict[str, Any], previous: dict[str, Any]) -> None:
    """A new revocation list may only add to the previous one and may not be older (clients refuse an older list)."""
    need(new["issued_at"] >= previous["issued_at"], "issued_at is older than the previous revocation list")
    need(new["min_serial"] >= previous["min_serial"], "min_serial is lower than in the previous list")
    dropped = set(previous["revoked"]) - set(new["revoked"])
    need(not dropped, f"the new list drops revoked serials {sorted(dropped)}")


def load_previous(path: Path) -> dict[str, Any]:
    """The doc inside a `revocations` object (as written by `revoke`), or a bare doc."""
    data = json.loads(path.read_text(encoding="utf-8"))
    doc = json.loads(base64.b64decode(data["doc"], validate=True)) if "doc" in data else data
    return build_revocations(min_serial=doc["min_serial"], revoked=doc["revoked"], issued_at=doc["issued_at"])


def certify(root: Ed25519PrivateKey, root_key_id: str, cert: dict[str, Any]) -> dict[str, str]:
    body = jcs(cert)
    return {"cert": b64(body), "root_key_id": root_key_id, "sig": b64(root.sign(CERT_DOMAIN + body))}


def revoke(root: Ed25519PrivateKey, root_key_id: str, doc: dict[str, Any]) -> dict[str, str]:
    body = jcs(doc)
    return {"doc": b64(body), "root_key_id": root_key_id, "sig": b64(root.sign(REVOCATIONS_DOMAIN + body))}


def take_key(env_name: str):
    """load_key, then pop the variable so nothing later in this process (or a child) can read the root seed."""
    key = load_key(env_name)
    os.environ.pop(env_name, None)
    return key


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("certify", "revoke"):
        p = sub.add_parser(name)
        p.add_argument("--root-key-id", required=True)
        p.add_argument("--root-key-env", default=DEFAULT_ROOT_ENV, help="NAME of the environment variable")
        p.add_argument("--out", type=Path, required=True)
    c = sub.choices["certify"]
    c.add_argument("--key-id", required=True)
    c.add_argument("--public-key", required=True, help="base64 of the 32-byte Ed25519 public key")
    c.add_argument("--serial", type=int, required=True)
    c.add_argument("--purpose", choices=PURPOSES, required=True)
    c.add_argument("--channels", required=True, help="comma separated")
    c.add_argument("--not-before", required=True)
    c.add_argument("--not-after", required=True)
    r = sub.choices["revoke"]
    r.add_argument("--min-serial", type=int, required=True)
    r.add_argument("--revoked", default="", help="comma separated serials")
    r.add_argument("--issued-at", required=True)
    r.add_argument("--previous", type=Path, help="the previous revocations object (file); required")
    r.add_argument("--initial", action="store_true", help="the very first list: there is no previous one")
    a = ap.parse_args(argv)
    try:
        need(KEY_ID.fullmatch(a.root_key_id) is not None, f"bad root key id: {a.root_key_id!r}")
        if a.cmd == "certify":
            doc = build_cert(
                key_id=a.key_id,
                public_key=base64.b64decode(a.public_key, validate=True),
                serial=a.serial,
                purpose=a.purpose,
                channels=a.channels.split(","),
                not_before=a.not_before,
                not_after=a.not_after,
            )
            out = certify(take_key(a.root_key_env), a.root_key_id, doc)
        else:
            doc = build_revocations(
                min_serial=a.min_serial,
                revoked=[int(x) for x in a.revoked.split(",") if x],
                issued_at=a.issued_at,
            )
            need((a.previous is None) != (not a.initial), "give exactly one of --previous FILE or --initial")
            if a.previous is not None:
                check_successor(doc, load_previous(a.previous))
            out = revoke(take_key(a.root_key_env), a.root_key_id, doc)
    except (ValueError, KeyError, OSError) as exc:
        print(f"rootkey: {exc}", file=sys.stderr)
        return 1
    a.out.write_text(json.dumps(out, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
