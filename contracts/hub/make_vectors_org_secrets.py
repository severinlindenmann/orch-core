"""Builds ``contracts/hub/vectors_org_secrets.json``: sealed company secrets (M3, #1431-#1433, protocol.md 7.5).

Test code, deterministic (FAKE keys from fixed labels). Core (``orch.hubclient.orgsecrets``), the hub mirror
(``orch_hub.contract.org_secrets``) and the browser module (``web/seal.js``, node) replay EVERY case and fail on any
case they do not handle. ``uv run python contracts/hub/make_vectors_org_secrets.py`` (from packages/hub) rewrites the file.

Case kinds: ``aad`` (the AAD extra bytes), ``value`` (the value rule), ``seal`` (fixed-ephemeral seal, or a refusal),
``open`` (open under the given fields and key; ``expect`` is ``{"value": ...}`` or ``{"error": code}``)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from orch_hub.contract import org_secrets as O
from orch_hub.contract import suite as S

P256_N = S.P256_N


def scalar(label: str) -> int:
    seed = hashlib.sha256(b"FAKE orch v2 test vector: org-secrets/" + label.encode()).digest()
    return int.from_bytes(seed, "big") % (P256_N - 1) + 1


def key(label: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(scalar(label), ec.SECP256R1())


def hx(n: int) -> str:
    return n.to_bytes(32, "big").hex()


WXK, EPH, WRONG = key("wxk"), key("eph"), key("wrong")
WXK_PUB = S.public_bytes(WXK)
NOW = 1_800_000_000_000
DAY = 24 * 3600 * 1000
BASE: dict[str, Any] = {
    "hub_id": hashlib.sha256(b"FAKE org-secrets/hub").hexdigest()[:32],
    "workspace_id": hashlib.sha256(b"FAKE org-secrets/workspace").hexdigest()[:32],
    "secret_id": hashlib.sha256(b"FAKE org-secrets/secret").hexdigest()[:32],
    "version": 3,
    "wxk_version": 2,
    "expires_ms": NOW + 90 * DAY,
    "name": "crm-token",
    "value": "sk_live_Abc123-xyz.789",
}
FIELDS = ("hub_id", "workspace_id", "secret_id", "version", "wxk_version", "expires_ms", "name")
OPEN_CTX = {"now_ms": NOW, "issued_ms": NOW - 60_000}


def sealed_hex(value: str | None = None, check: bool = True, **over: Any) -> str:
    a = {**BASE, **over}
    return O._seal_with_ephemeral(
        EPH,
        WXK_PUB,
        a["hub_id"],
        a["workspace_id"],
        a["secret_id"],
        a["version"],
        a["wxk_version"],
        a["expires_ms"],
        a["name"],
        a["value"] if value is None else value,
        check,
    ).hex()


GOOD = sealed_hex()


def other_hex(label: str) -> str:
    return hashlib.sha256(b"FAKE org-secrets/other/" + label.encode()).hexdigest()[:32]


def flip(h: str) -> str:
    raw = bytearray.fromhex(h)
    raw[-1] ^= 1
    return raw.hex()


def flip_ct(h: str) -> str:
    raw = bytearray.fromhex(h)
    raw[70] ^= 1  # inside the ciphertext, after the 65-byte ephemeral key
    return raw.hex()


def seal_case(title: str, expect: dict[str, Any], **over: Any) -> dict[str, Any]:
    return {"kind": "seal", "name": title, "args": {**BASE, **over}, "expect": expect}


def open_case(
    title: str, expect: dict[str, Any], sealed: str = GOOD, using: str = "wxk", **over: Any
) -> dict[str, Any]:
    args = {k: BASE[k] for k in FIELDS}
    args.update(OPEN_CTX)
    args.update(over)
    return {"kind": "open", "name": title, "args": {**args, "sealed": sealed, "key": using}, "expect": expect}


def build() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    # the AAD extra
    cases.append(
        {
            "kind": "aad",
            "name": "aad base",
            "args": {k: BASE[k] for k in FIELDS},
            "expect": {
                "hex": O.wrap_aad_extra(
                    *[BASE[k] for k in FIELDS[:2]], BASE["wxk_version"], BASE["expires_ms"], BASE["name"]
                ).hex()
            },
        }
    )
    for bad in ("", "Crm", "1crm", "crm_token", "a" * 41, "crm token", "crm-tokén"):
        cases.append(
            {
                "kind": "aad",
                "name": f"aad bad name {bad!r}"[:60],
                "args": {**{k: BASE[k] for k in FIELDS}, "name": bad},
                "expect": {"error": "malformed"},
            }
        )
    for f, bad in (
        ("hub_id", "AB" * 16),
        ("workspace_id", "ab" * 15),
        ("wxk_version", -1),
        ("wxk_version", 2**32),
        ("expires_ms", 2**64),
    ):
        cases.append(
            {
                "kind": "aad",
                "name": f"aad bad {f} {bad!r}"[:60],
                "args": {**{k: BASE[k] for k in FIELDS}, f: bad},
                "expect": {"error": "malformed"},
            }
        )
    cases.append(
        {
            "kind": "aad",
            "name": "aad name of 40",
            "args": {**{k: BASE[k] for k in FIELDS}, "name": "a" * 40},
            "expect": {
                "hex": O.wrap_aad_extra(
                    BASE["hub_id"], BASE["workspace_id"], BASE["wxk_version"], BASE["expires_ms"], "a" * 40
                ).hex()
            },
        }
    )
    # the value rule
    values = {
        "ok": ("sk_live_Abc123", None),
        "ok with inner space": ("pass word 1", None),
        "ok 8 characters": ("abcdefgh", None),
        "ok punctuation": ("~!@#$%^&*()_+{}|:\"<>?`-=[]\\;',./", None),
        "ok 4096 characters": ("a" * 4096, None),
        "too short 7": ("abcdefg", "too_short"),
        "empty": ("", "too_short"),
        "too long 4097": ("a" * 4097, "too_long"),
        "control character": ("abcd\x07efgh", "bad_character"),
        "newline": ("abcd\nefgh", "bad_character"),
        "carriage return": ("abcd\refgh", "bad_character"),
        "nul": ("abcd\x00efgh", "bad_character"),
        "tab": ("abcd\tefgh", "bad_character"),
        "delete 0x7f": ("abcd\x7fefgh", "bad_character"),
        "non-ascii letter": ("p\u00e4ssword1", "bad_character"),
        "emoji": ("passw\U0001f600rd1", "bad_character"),
        "zero width space": ("passw\u200bord1", "bad_character"),
        "leading space": (" abcdefgh", "edge_space"),
        "trailing space": ("abcdefgh ", "edge_space"),
        "only spaces": ("        ", "edge_space"),
    }
    for n, (v, p) in values.items():
        cases.append({"kind": "value", "name": f"value {n}", "args": {"value": v}, "expect": {"problem": p}})
    # sealing
    cases.append(seal_case("seal ok", {"sealed": GOOD}))
    cases.append(seal_case("seal ok other version", {"sealed": sealed_hex(version=4)}, version=4))
    cases.append(seal_case("seal refuses a short value", {"error": "value"}, value="abcdefg"))
    cases.append(seal_case("seal refuses a control character", {"error": "value"}, value="abcd\x07efgh"))
    cases.append(seal_case("seal refuses a non-ASCII value", {"error": "value"}, value="p\u00e4ssword1"))
    cases.append(seal_case("seal refuses a leading space", {"error": "value"}, value=" abcdefgh"))
    cases.append(seal_case("seal refuses a bad name", {"error": "malformed"}, name="Crm"))
    # open
    cases.append(open_case("open ok", {"value": BASE["value"]}))
    changed: dict[str, Any] = {
        "hub_id": other_hex("hub"),
        "workspace_id": other_hex("workspace"),
        "secret_id": other_hex("secret"),
        "version": BASE["version"] + 1,
        "wxk_version": BASE["wxk_version"] + 1,
        "expires_ms": BASE["expires_ms"] + 1,
        "name": "crm-tokem",
    }
    for f, v in changed.items():
        cases.append(open_case(f"open fails with changed {f}", {"error": "aead"}, **{f: v}))
    cases.append(open_case("open fails with a shorter name", {"error": "aead"}, name="crm"))
    cases.append(open_case("open fails with a flipped tag byte", {"error": "aead"}, sealed=flip(GOOD)))
    cases.append(open_case("open fails with a flipped ciphertext byte", {"error": "aead"}, sealed=flip_ct(GOOD)))
    cases.append(open_case("open fails with a truncated blob", {"error": "aead"}, sealed=GOOD[:150]))
    cases.append(open_case("open fails with the wrong key", {"error": "aead"}, using="wrong"))
    cases.append(
        open_case("open fails for a wrap sealed under purpose hub-share", {"error": "aead"}, sealed=purpose_swapped())
    )
    # sealed bad values: the host refuses after unsealing
    for n, v in (
        ("short", "abcdefg"),
        ("control character", "abcd\x07efgh"),
        ("non-ASCII", "p\u00e4ssword1"),
        ("leading space", " abcdefgh"),
        ("trailing space", "abcdefgh "),
        ("long", "a" * 4097),
    ):
        cases.append(
            open_case(f"open refuses a sealed {n} value", {"error": "value"}, sealed=sealed_hex(v, check=False))
        )
    # expiry
    ex = BASE["expires_ms"]
    cases.append(open_case("open ok one ms before expiry", {"value": BASE["value"]}, now_ms=ex - 1))
    cases.append(open_case("open refuses at expiry", {"error": "expired"}, now_ms=ex))
    cases.append(open_case("open refuses after expiry", {"error": "expired"}, now_ms=ex + 1))
    far = NOW + 400 * DAY
    cases.append(
        open_case(
            "open refuses more than 366 days after issue",
            {"error": "horizon"},
            sealed=sealed_hex(expires_ms=far),
            expires_ms=far,
        )
    )
    at = NOW - 60_000 + O.EXPIRY_MAX_MS
    cases.append(
        open_case(
            "open ok exactly 366 days after issue",
            {"value": BASE["value"]},
            sealed=sealed_hex(expires_ms=at),
            expires_ms=at,
        )
    )
    cases.append(
        open_case(
            "open refuses one ms past 366 days",
            {"error": "horizon"},
            sealed=sealed_hex(expires_ms=at + 1),
            expires_ms=at + 1,
        )
    )
    return {
        "description": "Company secrets sealed to a workspace WXK (purpose org-secret); FAKE keys, test-only. Replay EVERY case.",
        "consts": {
            "purpose": O.PURPOSE,
            "value_min": O.VALUE_MIN,
            "value_max": O.VALUE_MAX,
            "expiry_max_ms": O.EXPIRY_MAX_MS,
        },
        "keys": {
            "wxk_scalar": hx(scalar("wxk")),
            "wxk_pub": WXK_PUB.hex(),
            "eph_scalar": hx(scalar("eph")),
            "wrong_scalar": hx(scalar("wrong")),
        },
        "cases": cases,
    }


def purpose_swapped() -> str:
    """The same bytes sealed under the ``hub-share`` purpose label (hub-side construction by hand)."""
    import struct

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    from orch_hub.contract.labels import L

    p = b"hub-share"
    eph_pub = S.public_bytes(EPH)
    ss = EPH.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), WXK_PUB))
    info = b"orch/v2/seal|" + p + b"|" + BASE["workspace_id"].encode()
    k = HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_pub + WXK_PUB, info=info).derive(ss)
    extra = O.wrap_aad_extra(
        BASE["hub_id"], BASE["workspace_id"], BASE["wxk_version"], BASE["expires_ms"], BASE["name"]
    )
    aad = (
        L["aad_seal"]
        + bytes([S.SUITE_ID, len(p)])
        + p
        + bytes.fromhex(BASE["secret_id"])
        + struct.pack(">I", BASE["version"])
        + extra
    )
    return (eph_pub + AESGCM(k).encrypt(bytes(12), BASE["value"].encode(), aad)).hex()


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "vectors_org_secrets.json"
    out.write_text(json.dumps(build(), indent=1, ensure_ascii=True) + "\n")
    print(f"wrote {out} ({len(build()['cases'])} cases)", file=sys.stderr)
