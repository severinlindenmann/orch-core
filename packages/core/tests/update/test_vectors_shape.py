"""Signed-release vectors (install-update §4, U0): files are current, well formed, and a reference verifier
written from CONTRACT.md reaches exactly the expected reason for every case."""

from __future__ import annotations

import base64
import binascii
import hashlib
import importlib.util
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

DIR = Path(__file__).resolve().parent.parent / "vectors" / "release"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_release_vectors", DIR / "gen_release_vectors.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GEN = _gen()
KEYS = json.loads((DIR / "keys.json").read_text(encoding="utf-8"))
CORE = json.loads((DIR / "core.json").read_text(encoding="utf-8"))["cases"]
CATALOG = json.loads((DIR / "catalog.json").read_text(encoding="utf-8"))["cases"]
ALL = [("core", c) for c in CORE] + [("catalog", c) for c in CATALOG]

REQUIRED_CORE = {
    "ok": 1,
    "malformed": 4,
    "unknown_root": 1,
    "bad_cert_sig": 1,
    "bad_revocations_sig": 1,
    "revocations_rollback": 1,
    "wrong_purpose": 1,
    "channel_not_allowed": 1,
    "cert_expired": 1,
    "cert_not_yet_valid": 1,
    "key_revoked": 2,
    "key_mismatch": 1,
    "bad_sig": 2,
    "wrong_format": 1,
    "wrong_channel": 1,
    "expired": 1,
    "rollback": 1,
    "hash_mismatch": 1,
    "size_mismatch": 1,
}
REQUIRED_CATALOG = {"ok", "wrong_purpose", "bad_sig", "wrong_format", "wrong_channel", "expired", "rollback"}


@pytest.mark.parametrize("name", sorted(GEN.all_vectors()))
def test_committed_files_equal_the_generator(name):
    assert (DIR / name).read_text(encoding="utf-8") == GEN.render(GEN.all_vectors()[name])


def test_keys_are_labelled_test_only_and_seeds_match_the_rule():
    assert KEYS["test_only"] is True and "TEST KEYS ONLY" in KEYS["WARNING"]
    for name, k in KEYS["keys"].items():
        seed = hashlib.sha256(f"orch-release TEST key {name}; never trust outside tests".encode()).digest()
        assert k["seed_hex_TEST_ONLY"] == seed.hex()
    assert KEYS["trusted_roots"] == {"test-root-1": KEYS["keys"]["root-1"]["public_key"]}


def test_case_shape_and_coverage():
    codes = KEYS["reason_code"]
    names = set()
    for kind, c in ALL:
        assert set(c) <= {"name", "pins", "purpose", "channel", "now", "state", "envelope", "expect", "files"}
        assert (kind, c["name"]) not in names
        names.add((kind, c["name"]))
        assert c["purpose"] == ("core" if kind == "core" else "addon-catalog")
        assert set(c["state"]) == {"trusted_roots", "seq_seen", "revocations_issued_at"}
        assert isinstance(c["envelope"], str) and c["pins"]
        assert c["expect"]["reason"] in codes
        assert c["expect"]["code"] == codes[c["expect"]["reason"]]
        assert ("accept" in c["expect"]) == (c["expect"]["reason"] == "ok")
    counts: dict[str, int] = {}
    for c in CORE:
        counts[c["expect"]["reason"]] = counts.get(c["expect"]["reason"], 0) + 1
    for reason, n in REQUIRED_CORE.items():
        assert counts.get(reason, 0) >= n, reason
    assert set(REQUIRED_CORE) == set(codes)
    assert REQUIRED_CATALOG <= {c["expect"]["reason"] for c in CATALOG}
    assert {c["channel"] for c in CORE if c["expect"]["reason"] == "ok"} >= {"main", "develop", "test-ci"}


# --- reference verifier (CONTRACT.md §3), independent of the generator ---------------------------------------------


class Refuse(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
CHANNEL = re.compile(r"(main|develop|test-[a-z0-9][a-z0-9-]{0,30})\Z")
KEY_ID = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
VERSION = re.compile(r"\d+\.\d+\.\d+(\.dev\d+)?\Z")
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
MAX_INT = 2**53 - 1


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise Refuse("malformed")
        out[k] = v
    return out


def _no(_):
    raise Refuse("malformed")


def strict_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_float=_no, parse_constant=_no)
    except (ValueError, UnicodeDecodeError) as e:
        raise Refuse("malformed") from e


def canonical_doc(data: bytes) -> dict:
    obj = strict_json(data)
    if json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") != data:
        raise Refuse("malformed")
    if not isinstance(obj, dict):
        raise Refuse("malformed")
    return obj


def need(cond: bool) -> None:
    if not cond:
        raise Refuse("malformed")


def keys(obj: Any, want: set[str]) -> dict:
    need(isinstance(obj, dict) and set(obj) == want)
    return obj


def b64d(s: Any, size: int | None = None) -> bytes:
    need(isinstance(s, str))
    try:
        raw = base64.b64decode(s, validate=True)
    except binascii.Error as e:
        raise Refuse("malformed") from e
    need(base64.b64encode(raw).decode("ascii") == s and (size is None or len(raw) == size))
    return raw


def ts(s: Any) -> datetime:
    need(isinstance(s, str) and bool(TIME.match(s)))
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def uint(v: Any, lo: int = 1) -> int:
    need(type(v) is int and lo <= v <= MAX_INT)
    return v


def text_field(v: Any, max_bytes: int) -> str:
    need(isinstance(v, str) and len(v.encode("utf-8")) <= max_bytes)
    return v


def ed25519_ok(pub: bytes, sig: bytes, msg: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
        return True
    except InvalidSignature:
        return False


def check_cert(c: dict) -> None:
    keys(c, {"format", "key_id", "public_key", "serial", "purpose", "channels", "not_before", "not_after"})
    need(c["format"] == "orch.release-key/1" and bool(KEY_ID.match(c["key_id"])))
    b64d(c["public_key"], 32)
    uint(c["serial"])
    need(c["purpose"] in ("core", "addon-catalog"))
    ch = c["channels"]
    need(isinstance(ch, list) and ch and len(set(ch)) == len(ch))
    need(all(isinstance(x, str) and (x == "test-*" or CHANNEL.match(x)) for x in ch))
    need(ts(c["not_before"]) < ts(c["not_after"]))


def check_revocations(d: dict) -> None:
    keys(d, {"format", "min_serial", "revoked", "issued_at"})
    need(d["format"] == "orch.revocations/1")
    uint(d["min_serial"])
    r = d["revoked"]
    need(isinstance(r, list) and all(type(x) is int and 1 <= x <= MAX_INT for x in r) and r == sorted(set(r)))
    ts(d["issued_at"])


def check_manifest(m: dict) -> None:
    keys(
        m,
        {"format", "channel", "version", "seq", "commit", "issued_at", "expires_at", "files", "changelog", "min_from"},
    )
    need(isinstance(m["channel"], str) and bool(CHANNEL.match(m["channel"])))
    need(isinstance(m["version"], str) and bool(VERSION.match(m["version"])))
    need(isinstance(m["min_from"], str) and bool(VERSION.match(m["min_from"])))
    uint(m["seq"])
    need(isinstance(m["commit"], str) and bool(HEX40.match(m["commit"])))
    need(ts(m["issued_at"]) < ts(m["expires_at"]))
    text_field(m["changelog"], 4096)
    fs = m["files"]
    need(isinstance(fs, list) and 1 <= len(fs) <= 6)
    for f in fs:
        keys(f, {"role", "name", "sha256", "size", "url"})
        need(isinstance(f["name"], str) and bool(FILE_NAME.match(f["name"])))
        need(isinstance(f["sha256"], str) and bool(HEX64.match(f["sha256"])))
        uint(f["size"])
        need(isinstance(f["url"], str) and f["url"].startswith("https://") and len(f["url"]) <= 512)
        need(f["url"].isprintable() and " " not in f["url"])
    roles = [f["role"] for f in fs]
    known = ("core", "dashboard", "hub", "relay", "publish", "constraints")
    need(all(isinstance(r, str) and r in known for r in roles))
    need(len(set(roles)) == len(roles))
    need("core" in roles and "constraints" in roles)
    need(len({f["name"].lower() for f in fs}) == len(fs))


def check_catalog(m: dict) -> None:
    keys(m, {"format", "channel", "seq", "issued_at", "expires_at", "addons"})
    need(isinstance(m["channel"], str) and bool(CHANNEL.match(m["channel"])))
    uint(m["seq"])
    need(ts(m["issued_at"]) < ts(m["expires_at"]))
    need(isinstance(m["addons"], dict))


def channel_allowed(allowed: list[str], channel: str) -> bool:
    return channel in allowed or (channel.startswith("test-") and "test-*" in allowed)


def verify(c: dict) -> dict:
    trusted = {k: b64d(v, 32) for k, v in KEYS["trusted_roots"].items() if k in c["state"]["trusted_roots"]}
    purpose, channel, now = c["purpose"], c["channel"], ts(c["now"])
    st = c["state"]
    # 1 outer envelope
    env = keys(strict_json(c["envelope"].encode("utf-8")), {"format", "payload", "sig", "key_cert", "revocations"})
    need(env["format"] == "orch.release-envelope/1")
    payload = b64d(env["payload"])
    sig = keys(env["sig"], {"alg", "key_id", "value"})
    need(sig["alg"] == "ed25519" and isinstance(sig["key_id"], str) and bool(KEY_ID.match(sig["key_id"])))
    sig_value = b64d(sig["value"], 64)
    kc = keys(env["key_cert"], {"cert", "root_key_id", "sig"})
    rv = keys(env["revocations"], {"doc", "root_key_id", "sig"})
    cert_bytes, cert_sig = b64d(kc["cert"]), b64d(kc["sig"], 64)
    rev_bytes, rev_sig = b64d(rv["doc"]), b64d(rv["sig"], 64)
    need(isinstance(kc["root_key_id"], str) and isinstance(rv["root_key_id"], str))
    # 2 certificate
    if kc["root_key_id"] not in trusted:
        raise Refuse("unknown_root")
    if not ed25519_ok(trusted[kc["root_key_id"]], cert_sig, b"orch-release-key/1\n" + cert_bytes):
        raise Refuse("bad_cert_sig")
    cert = canonical_doc(cert_bytes)
    check_cert(cert)
    # 3 revocations
    if rv["root_key_id"] not in trusted:
        raise Refuse("unknown_root")
    if not ed25519_ok(trusted[rv["root_key_id"]], rev_sig, b"orch-revocations/1\n" + rev_bytes):
        raise Refuse("bad_revocations_sig")
    revs = canonical_doc(rev_bytes)
    check_revocations(revs)
    if st["revocations_issued_at"] is not None and ts(revs["issued_at"]) < ts(st["revocations_issued_at"]):
        raise Refuse("revocations_rollback")
    # 4 certificate rules
    if cert["purpose"] != purpose:
        raise Refuse("wrong_purpose")
    if not channel_allowed(cert["channels"], channel):
        raise Refuse("channel_not_allowed")
    if now < ts(cert["not_before"]):
        raise Refuse("cert_not_yet_valid")
    if now >= ts(cert["not_after"]):
        raise Refuse("cert_expired")
    if cert["serial"] < revs["min_serial"] or cert["serial"] in revs["revoked"]:
        raise Refuse("key_revoked")
    # 5 envelope signature
    if sig["key_id"] != cert["key_id"]:
        raise Refuse("key_mismatch")
    domain = {"core": b"orch-release/1\n", "addon-catalog": b"orch-addon-catalog/1\n"}[purpose]
    if not ed25519_ok(b64d(cert["public_key"], 32), sig_value, domain + payload):
        raise Refuse("bad_sig")
    # 6 payload
    doc = canonical_doc(payload)
    want_format = {"core": "orch.release/1", "addon-catalog": "orch.addon-catalog/1"}[purpose]
    if doc.get("format") != want_format:
        raise Refuse("wrong_format")
    (check_manifest if purpose == "core" else check_catalog)(doc)
    if doc["channel"] != channel:
        raise Refuse("wrong_channel")
    if now >= ts(doc["expires_at"]):
        raise Refuse("expired")
    if st["seq_seen"] is not None and doc["seq"] < st["seq_seen"]:
        raise Refuse("rollback")
    # 7 files (only when the case provides downloaded files)
    if "files" in c:
        got = {f["name"]: base64.b64decode(f["content_b64"]) for f in c["files"]}
        for f in doc["files"]:
            data = got.get(f["name"])
            if data is None or len(data) != f["size"]:
                raise Refuse("size_mismatch")
            if hashlib.sha256(data).hexdigest() != f["sha256"]:
                raise Refuse("hash_mismatch")
    return {
        "channel": doc["channel"],
        "seq": doc["seq"],
        "version": doc.get("version"),
        "min_serial": revs["min_serial"],
        "revocations_issued_at": revs["issued_at"],
    }


@pytest.mark.parametrize("kind,case", ALL, ids=[f"{k}:{c['name']}" for k, c in ALL])
def test_reference_verifier_reaches_the_expected_reason(kind, case):
    try:
        got, reason = verify(case), "ok"
    except Refuse as e:
        got, reason = None, e.reason
    assert reason == case["expect"]["reason"]
    if reason == "ok":
        assert got == case["expect"]["accept"]
