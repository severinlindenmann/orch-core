"""Generate the signed-release test vectors (install-update §4, U0). TEST KEYS ONLY.

Every key here comes from a fixed, public seed: sha256(b"orch-release TEST key <name>; never trust outside tests").
None of them may ever be embedded in a shipped verifier. Ed25519 signatures are deterministic, so running this
script twice gives byte-identical files.

    uv run python tests/vectors/release/gen_release_vectors.py          # rewrite keys.json, core.json, catalog.json
    uv run python tests/vectors/release/gen_release_vectors.py --check  # exit 1 if a committed file differs

The format and verification rules are in CONTRACT.md next to this file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

OUT = Path(__file__).resolve().parent

NOW = "2026-10-22T08:00:00Z"
DOMAIN = {"core": b"orch-release/1\n", "addon-catalog": b"orch-addon-catalog/1\n"}
CERT_DOMAIN = b"orch-release-key/1\n"
REVOCATIONS_DOMAIN = b"orch-revocations/1\n"
PAYLOAD_FORMAT = {"core": "orch.release/1", "addon-catalog": "orch.addon-catalog/1"}
UNVERIFIED, STALE = "update.unverified", "update.stale"
REASON_CODE = {
    "ok": None,
    "malformed": UNVERIFIED,
    "unknown_root": UNVERIFIED,
    "bad_cert_sig": UNVERIFIED,
    "bad_revocations_sig": UNVERIFIED,
    "revocations_rollback": STALE,
    "wrong_purpose": UNVERIFIED,
    "channel_not_allowed": UNVERIFIED,
    "cert_not_yet_valid": UNVERIFIED,
    "cert_expired": UNVERIFIED,
    "key_revoked": UNVERIFIED,
    "key_mismatch": UNVERIFIED,
    "bad_sig": UNVERIFIED,
    "wrong_format": UNVERIFIED,
    "wrong_channel": UNVERIFIED,
    "expired": STALE,
    "rollback": STALE,
    "size_mismatch": UNVERIFIED,
    "hash_mismatch": UNVERIFIED,
}

# --- helpers ------------------------------------------------------------------------------------------------------


def jcs(obj: Any) -> bytes:
    """RFC 8785 for the contract's value domain (ASCII keys, strings, ints, bools, lists, objects; no floats)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


class TestKey:
    def __init__(self, name: str) -> None:
        self.name = name
        self.seed = hashlib.sha256(f"orch-release TEST key {name}; never trust outside tests".encode()).digest()
        self._sk = Ed25519PrivateKey.from_private_bytes(self.seed)
        self.public = self._sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def sign(self, msg: bytes) -> bytes:
        return self._sk.sign(msg)


ROOT = TestKey("root-1")  # the trusted TEST root
ROGUE_ROOT = TestKey("root-9")  # a root nobody trusts
RK3 = TestKey("rk-3")  # current core release key, serial 3
RK1 = TestKey("rk-1")  # serial 1: below min_serial
RK4 = TestKey("rk-4")  # serial 4: on the revoked list
RK5 = TestKey("rk-5")  # serial 5: cert expired
RK6 = TestKey("rk-6")  # serial 6: cert not yet valid
CK1 = TestKey("ck-1")  # add-on catalog key, serial 7
TK1 = TestKey("tk-1")  # test-channel key, serial 8, channels ["test-*"]
KEYS = [ROOT, ROGUE_ROOT, RK3, RK1, RK4, RK5, RK6, CK1, TK1]


def cert_doc(key: TestKey, serial: int, purpose: str, channels: list[str], nb: str, na: str) -> dict[str, Any]:
    return {
        "format": "orch.release-key/1",
        "key_id": key.name,
        "public_key": b64(key.public),
        "serial": serial,
        "purpose": purpose,
        "channels": channels,
        "not_before": nb,
        "not_after": na,
    }


VALID = ("2026-10-01T00:00:00Z", "2027-03-30T00:00:00Z")  # 180 days
CERTS = {
    "rk-3": cert_doc(RK3, 3, "core", ["develop", "main"], *VALID),
    "rk-1": cert_doc(RK1, 1, "core", ["develop", "main"], *VALID),
    "rk-4": cert_doc(RK4, 4, "core", ["develop", "main"], *VALID),
    "rk-5": cert_doc(RK5, 5, "core", ["develop", "main"], "2026-04-01T00:00:00Z", "2026-09-28T00:00:00Z"),
    "rk-6": cert_doc(RK6, 6, "core", ["develop", "main"], "2026-11-01T00:00:00Z", "2027-04-30T00:00:00Z"),
    "ck-1": cert_doc(CK1, 7, "addon-catalog", ["develop", "main"], *VALID),
    "tk-1": cert_doc(TK1, 8, "core", ["test-*"], *VALID),
}
SIGNER = {"rk-3": RK3, "rk-1": RK1, "rk-4": RK4, "rk-5": RK5, "rk-6": RK6, "ck-1": CK1, "tk-1": TK1}

REVOCATIONS = {"format": "orch.revocations/1", "min_serial": 2, "revoked": [4], "issued_at": "2026-10-20T00:00:00Z"}


def key_cert(key_id: str, *, root: TestKey = ROOT, root_key_id: str = "root-1", raw: bytes | None = None) -> dict:
    body = jcs(CERTS[key_id]) if raw is None else raw
    return {"cert": b64(body), "root_key_id": root_key_id, "sig": b64(root.sign(CERT_DOMAIN + body))}


def revocations(*, doc: dict | None = None, signer: TestKey = ROOT) -> dict:
    body = jcs(REVOCATIONS if doc is None else doc)
    return {"doc": b64(body), "root_key_id": "root-1", "sig": b64(signer.sign(REVOCATIONS_DOMAIN + body))}


def envelope(
    payload: bytes,
    key_id: str = "rk-3",
    *,
    domain: bytes = DOMAIN["core"],
    sig_key_id: str | None = None,
    cert: dict | None = None,
    revs: dict | None = None,
    tamper_payload: bytes | None = None,
) -> dict[str, Any]:
    sig = SIGNER[key_id].sign(domain + payload)
    return {
        "format": "orch.release-envelope/1",
        "payload": b64(payload if tamper_payload is None else tamper_payload),
        "sig": {"alg": "ed25519", "key_id": sig_key_id or key_id, "value": b64(sig)},
        "key_cert": cert or key_cert(key_id),
        "revocations": revs or revocations(),
    }


def text(env: dict[str, Any]) -> str:
    return jcs(env).decode("utf-8")


# --- payloads -----------------------------------------------------------------------------------------------------

FILE_BYTES = {
    "core": b"TEST orch-core wheel bytes (not a real wheel)\n",
    "dashboard": b"TEST orch-dashboard wheel bytes (not a real wheel)\n",
    "constraints": b"cryptography==46.0.3\njsonschema==4.26.0\nsegno==1.6.6\n",
}


def file_names(version: str) -> dict[str, str]:
    return {
        "core": f"orch_core-{version}-py3-none-any.whl",
        "dashboard": f"orch_dashboard-{version}-py3-none-any.whl",
        "constraints": "constraints.txt",
    }


def manifest(channel: str, seq: int, **over: Any) -> dict[str, Any]:
    version = f"2.0.0.dev{seq}"
    names = file_names(version)
    m = {
        "format": "orch.release/1",
        "channel": channel,
        "version": version,
        "seq": seq,
        "commit": hashlib.sha1(f"TEST commit {channel} {seq}".encode()).hexdigest(),
        "issued_at": "2026-10-21T07:40:00Z",
        "expires_at": "2026-11-11T07:40:00Z",
        "files": [
            {
                "role": role,
                "name": names[role],
                "sha256": hashlib.sha256(FILE_BYTES[role]).hexdigest(),
                "size": len(FILE_BYTES[role]),
                "url": f"https://github.com/severinlindenmann/orch-core/releases/download/v{version}/{names[role]}",
            }
            for role in ("core", "dashboard", "constraints")
        ],
        "changelog": "TEST release. Wiki add-on: page links; fix terminal resize.",
        "min_from": "2.0.0.dev1",
    }
    m.update(over)
    return m


def catalog(channel: str, seq: int, **over: Any) -> dict[str, Any]:
    c = {
        "format": "orch.addon-catalog/1",
        "channel": channel,
        "seq": seq,
        "issued_at": "2026-10-21T07:40:00Z",
        "expires_at": "2026-11-11T07:40:00Z",
        "addons": {
            "wiki": {
                "title": "Wiki",
                "versions": [
                    {
                        "version": "0.2.0",
                        "api": ">=1.1,<2",
                        "min_core": "2.0.0.dev60",
                        "capabilities": [],
                        "package_sha256": "sha256:" + hashlib.sha256(b"TEST wiki package").hexdigest(),
                        "archive": {
                            "url": "https://github.com/severinlindenmann/orch-core/releases/download/"
                            "addon-wiki-v0.2.0/wiki-0.2.0.tar",
                            "sha256": hashlib.sha256(b"TEST wiki tar").hexdigest(),
                            "size": 5120,
                        },
                        "changelog": "TEST. Page links in tickets.",
                        "revoked": False,
                    }
                ],
            }
        },
    }
    c.update(over)
    return c


def files_for(m: dict[str, Any], mutate: dict[str, bytes] | None = None) -> list[dict[str, str]]:
    roles = {f["name"]: f["role"] for f in m["files"]}
    out = []
    for name, role in roles.items():
        data = (mutate or {}).get(role, FILE_BYTES[role])
        out.append({"name": name, "content_b64": b64(data)})
    return out


# --- cases --------------------------------------------------------------------------------------------------------

STATE = {"trusted_roots": ["root-1"], "seq_seen": 41, "revocations_issued_at": "2026-10-20T00:00:00Z"}


def case(name: str, pins: str, env: str, reason: str, *, purpose="core", channel="develop", state=None, files=None):
    c: dict[str, Any] = {
        "name": name,
        "pins": pins,
        "purpose": purpose,
        "channel": channel,
        "now": NOW,
        "state": {**STATE, **(state or {})},
        "envelope": env,
        "expect": {"reason": reason, "code": REASON_CODE[reason]},
    }
    if files is not None:
        c["files"] = files
    return c


def accept(c: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    c["expect"]["accept"] = {
        "channel": payload["channel"],
        "seq": payload["seq"],
        "version": payload.get("version"),
        "min_serial": REVOCATIONS["min_serial"],
        "revocations_issued_at": REVOCATIONS["issued_at"],
    }
    return c


def core_cases() -> list[dict[str, Any]]:
    dev = manifest("develop", 42)
    dev_b = jcs(dev)
    main = manifest("main", 40)
    test = manifest("test-ci", 43)
    ok_env = text(envelope(dev_b))
    ok_files = files_for(dev)
    cs = []

    def add(c):
        cs.append(c)

    # positives
    add(accept(case("ok_develop", "a valid develop release", ok_env, "ok", files=ok_files), dev))
    add(
        accept(
            case(
                "ok_main",
                "a valid main release, first check ever (no seq seen, no stored revocations)",
                text(envelope(jcs(main))),
                "ok",
                channel="main",
                state={"seq_seen": None, "revocations_issued_at": None},
                files=files_for(main),
            ),
            main,
        )
    )
    add(
        accept(
            case(
                "ok_seq_equal",
                "seq equal to the last seen is accepted (rerunning install.sh or a weekly refresh)",
                ok_env,
                "ok",
                state={"seq_seen": 42},
                files=ok_files,
            ),
            dev,
        )
    )
    add(
        accept(
            case(
                "ok_test_channel",
                "the test key (channels ['test-*']) signs a test-<name> channel",
                text(envelope(jcs(test), "tk-1")),
                "ok",
                channel="test-ci",
                state={"seq_seen": None},
                files=files_for(test),
            ),
            test,
        )
    )
    add(
        accept(
            case(
                "ok_revocations_older_stored",
                "a newer revocation list than the stored one is accepted (and then stored)",
                ok_env,
                "ok",
                state={"revocations_issued_at": "2026-10-01T00:00:00Z"},
                files=ok_files,
            ),
            dev,
        )
    )
    add(
        accept(
            case(
                "ok_outer_not_canonical",
                "the outer envelope is not signed, so whitespace and key order there do not matter",
                json.dumps(envelope(dev_b), indent=2),
                "ok",
            ),
            dev,
        )
    )

    # parsing: strict JSON, exact key sets, canonical signed bytes
    add(case("malformed_not_json", "not JSON at all", ok_env[:-7], "malformed"))
    add(
        case(
            "malformed_duplicate_key",
            "a duplicate key in the outer envelope",
            ok_env[:-1] + ',"format":"orch.release-envelope/1"}',
            "malformed",
        )
    )
    add(
        case(
            "malformed_unknown_field_envelope",
            "an unknown top-level envelope field",
            text({**envelope(dev_b), "note": "x"}),
            "malformed",
        )
    )
    add(
        case(
            "malformed_unknown_field_manifest",
            "a correctly signed, canonical manifest with an unknown field",
            text(envelope(jcs({**dev, "extra": 1}))),
            "malformed",
        )
    )
    add(
        case(
            "malformed_noncanonical_manifest",
            "correctly signed manifest bytes that are not JCS (indented)",
            text(envelope(json.dumps(dev, indent=1, sort_keys=True).encode())),
            "malformed",
        )
    )
    add(
        case(
            "malformed_noncanonical_cert",
            "a root-signed certificate whose bytes are not JCS (key order)",
            text(envelope(dev_b, cert=key_cert("rk-3", raw=json.dumps(CERTS["rk-3"]).encode()))),
            "malformed",
        )
    )
    add(
        case(
            "malformed_float_seq",
            "seq is a JSON number with a fraction (floats are never allowed)",
            text(envelope(dev_b.replace(b'"seq":42,', b'"seq":42.0,'))),
            "malformed",
        )
    )
    add(
        case(
            "malformed_base64_linebreak",
            "payload base64 with a MIME line break (only canonical RFC 4648 base64, no whitespace, is accepted)",
            text({**envelope(dev_b), "payload": b64(dev_b)[:76] + "\n" + b64(dev_b)[76:]}),
            "malformed",
        )
    )
    traversal = manifest("develop", 42)
    traversal["files"][0]["name"] = "../../.zshrc"
    add(
        case(
            "malformed_file_name_traversal",
            "a signed manifest whose file name is a path (names are bare file names)",
            text(envelope(jcs(traversal))),
            "malformed",
        )
    )
    add(
        case(
            "malformed_wrong_alg",
            "sig.alg other than ed25519",
            text({**envelope(dev_b), "sig": {**envelope(dev_b)["sig"], "alg": "ed448"}}),
            "malformed",
        )
    )

    # certificate chain
    add(
        case(
            "unknown_root",
            "certificate signed by a root that is not in the trust set",
            text(envelope(dev_b, cert=key_cert("rk-3", root=ROGUE_ROOT, root_key_id="root-9"))),
            "unknown_root",
        )
    )
    add(
        case(
            "bad_cert_sig",
            "certificate claims root-1 but is signed by another key",
            text(envelope(dev_b, cert=key_cert("rk-3", root=ROGUE_ROOT))),
            "bad_cert_sig",
        )
    )
    add(
        case(
            "bad_revocations_sig",
            "revocation list claims root-1 but is signed by the release key",
            text(envelope(dev_b, revs=revocations(signer=RK3))),
            "bad_revocations_sig",
        )
    )
    add(
        case(
            "revocations_rollback",
            "the revocation list is older than the stored one (a held-back revocation)",
            ok_env,
            "revocations_rollback",
            state={"revocations_issued_at": "2026-10-21T00:00:00Z"},
        )
    )
    add(
        case(
            "wrong_purpose_catalog_key",
            "the add-on catalog key (purpose addon-catalog) signs a core manifest with the core domain",
            text(envelope(dev_b, "ck-1")),
            "wrong_purpose",
        )
    )
    add(
        case(
            "channel_not_allowed",
            "the test key (channels ['test-*']) signs a develop manifest",
            text(envelope(dev_b, "tk-1")),
            "channel_not_allowed",
        )
    )
    add(case("cert_expired", "now >= cert not_after", text(envelope(dev_b, "rk-5")), "cert_expired"))
    add(case("cert_not_yet_valid", "now < cert not_before", text(envelope(dev_b, "rk-6")), "cert_not_yet_valid"))
    add(case("key_revoked_min_serial", "cert serial 1 < min_serial 2", text(envelope(dev_b, "rk-1")), "key_revoked"))
    add(
        case("key_revoked_listed", "cert serial 4 is on the revoked list", text(envelope(dev_b, "rk-4")), "key_revoked")
    )

    # envelope signature
    add(
        case(
            "key_mismatch",
            "sig.key_id names another key than the certificate",
            text(envelope(dev_b, sig_key_id="rk-9")),
            "key_mismatch",
        )
    )
    tampered = jcs(manifest("develop", 42, changelog="TEST release. Wiki add-on: page links; fix terminal resizE."))
    add(
        case(
            "bad_sig_tampered_payload",
            "payload changed after signing",
            text(envelope(dev_b, tamper_payload=tampered)),
            "bad_sig",
        )
    )
    add(
        case(
            "bad_sig_no_domain",
            "signature over the payload without the domain prefix",
            text(envelope(dev_b, domain=b"")),
            "bad_sig",
        )
    )
    add(
        case(
            "bad_sig_catalog_domain",
            "the core key signs a core manifest under the catalog domain",
            text(envelope(dev_b, domain=DOMAIN["addon-catalog"])),
            "bad_sig",
        )
    )
    add(
        case(
            "wrong_format_catalog_payload",
            "the core key signs a catalog payload under the core domain; verified as core",
            text(envelope(jcs(catalog("develop", 311)))),
            "wrong_format",
        )
    )

    # manifest rules
    add(
        case(
            "wrong_channel", "a valid develop release fetched for channel main", ok_env, "wrong_channel", channel="main"
        )
    )
    add(
        case(
            "expired",
            "now >= manifest expires_at",
            text(
                envelope(
                    jcs(manifest("develop", 42, issued_at="2026-09-20T07:40:00Z", expires_at="2026-10-11T07:40:00Z"))
                )
            ),
            "expired",
        )
    )
    add(case("rollback", "seq 42 < seq_seen 43", ok_env, "rollback", state={"seq_seen": 43}))
    mutated = bytearray(FILE_BYTES["core"])
    mutated[0] ^= 1
    add(
        case(
            "hash_mismatch",
            "a downloaded file has the right size but another sha256",
            ok_env,
            "hash_mismatch",
            files=files_for(dev, {"core": bytes(mutated)}),
        )
    )
    add(
        case(
            "size_mismatch",
            "a downloaded file has another size",
            ok_env,
            "size_mismatch",
            files=files_for(dev, {"dashboard": FILE_BYTES["dashboard"] + b"x"}),
        )
    )
    return cs


def catalog_cases() -> list[dict[str, Any]]:
    cat = catalog("develop", 311)
    cat_b = jcs(cat)
    cd = DOMAIN["addon-catalog"]
    st = {"seq_seen": 310}
    cs = [
        accept(
            case(
                "ok_catalog_develop",
                "a valid develop add-on catalog signed by the catalog key",
                text(envelope(cat_b, "ck-1", domain=cd)),
                "ok",
                purpose="addon-catalog",
                state=st,
            ),
            cat,
        ),
        case(
            "wrong_purpose_core_key",
            "the core release key signs a catalog under the catalog domain",
            text(envelope(cat_b, "rk-3", domain=cd)),
            "wrong_purpose",
            purpose="addon-catalog",
            state=st,
        ),
        case(
            "bad_sig_core_domain",
            "the catalog key signs the catalog under the core domain",
            text(envelope(cat_b, "ck-1")),
            "bad_sig",
            purpose="addon-catalog",
            state=st,
        ),
        case(
            "wrong_format_core_payload",
            "the catalog key signs a core manifest under the catalog domain; verified as catalog",
            text(envelope(jcs(manifest("develop", 42)), "ck-1", domain=cd)),
            "wrong_format",
            purpose="addon-catalog",
            state=st,
        ),
        case(
            "wrong_channel",
            "a develop catalog fetched for channel main",
            text(envelope(cat_b, "ck-1", domain=cd)),
            "wrong_channel",
            purpose="addon-catalog",
            channel="main",
            state=st,
        ),
        case(
            "expired",
            "now >= catalog expires_at",
            text(
                envelope(
                    jcs(catalog("develop", 311, issued_at="2026-09-20T07:40:00Z", expires_at="2026-10-11T07:40:00Z")),
                    "ck-1",
                    domain=cd,
                )
            ),
            "expired",
            purpose="addon-catalog",
            state=st,
        ),
        case(
            "rollback",
            "catalog seq 311 < seq_seen 312",
            text(envelope(cat_b, "ck-1", domain=cd)),
            "rollback",
            purpose="addon-catalog",
            state={"seq_seen": 312},
        ),
        case(
            "malformed_unknown_field_catalog",
            "a correctly signed catalog with an unknown top-level field",
            text(envelope(jcs({**cat, "extra": True}), "ck-1", domain=cd)),
            "malformed",
            purpose="addon-catalog",
            state=st,
        ),
    ]
    return cs


def keys_doc() -> dict[str, Any]:
    return {
        "WARNING": "TEST KEYS ONLY. Seeds are public (see gen_release_vectors.py). Never embed in a shipped verifier.",
        "test_only": True,
        "seed_rule": 'sha256(b"orch-release TEST key <name>; never trust outside tests")',
        "keys": {k.name: {"seed_hex_TEST_ONLY": k.seed.hex(), "public_key": b64(k.public)} for k in KEYS},
        "trusted_roots": {"root-1": b64(ROOT.public)},
        "certs": CERTS,
        "revocations": REVOCATIONS,
        "domains": {
            "payload": {p: d.decode() for p, d in DOMAIN.items()},
            "cert": CERT_DOMAIN.decode(),
            "revocations": REVOCATIONS_DOMAIN.decode(),
        },
        "payload_format": PAYLOAD_FORMAT,
        "reason_code": REASON_CODE,
    }


def all_vectors() -> dict[str, dict[str, Any]]:
    head = {"format": "orch.release-vectors/1", "contract": "CONTRACT.md", "test_only": True}
    return {
        "keys.json": keys_doc(),
        "core.json": {**head, "cases": core_cases()},
        "catalog.json": {**head, "cases": catalog_cases()},
    }


def render(v: dict[str, Any]) -> str:
    return json.dumps(v, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv: list[str]) -> int:
    bad = 0
    for name, v in all_vectors().items():
        path = OUT / name
        want = render(v)
        if "--check" in argv:
            if not path.exists() or path.read_text(encoding="utf-8") != want:
                print(f"stale: {path}")
                bad = 1
        else:
            path.write_text(want, encoding="utf-8")
    return bad


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
