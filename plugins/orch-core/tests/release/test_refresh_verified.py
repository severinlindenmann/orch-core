"""refresh.py / channel_seq.py never trust the published channel.json: only verified immutable releases count.

TODO(U2 lands): swap `ref_verify` for orch.update.verify.verify_envelope (the production adapter is
refresh.core_verifier) and drive the same cases with the contract vectors.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

SCRIPTS = Path(__file__).resolve().parents[4] / "scripts" / "release"
spec = importlib.util.spec_from_file_location("refresh", SCRIPTS / "refresh.py")
refresh = importlib.util.module_from_spec(spec)
sys.modules["refresh"] = refresh
spec.loader.exec_module(refresh)

ROOT = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
RK = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
ROOTS = {"root-1": ROOT.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
SHA = "a" * 40
b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
jcs = refresh.jcs


def raw_pub(k):
    return k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def revocations(min_serial=1, revoked=()):
    body = jcs(
        {
            "format": "orch.revocations/1",
            "min_serial": min_serial,
            "revoked": list(revoked),
            "issued_at": "2026-01-01T00:00:00Z",
        }
    )
    return {"doc": b64(body), "root_key_id": "root-1", "sig": b64(ROOT.sign(b"orch-revocations/1\n" + body))}


def cert(serial=1):
    body = jcs(
        {
            "format": "orch.release-key/1",
            "key_id": "rk-1",
            "public_key": b64(raw_pub(RK)),
            "serial": serial,
            "purpose": "core",
            "channels": ["main", "develop"],
            "not_before": "2026-01-01T00:00:00Z",
            "not_after": "2027-01-01T00:00:00Z",
        }
    )
    return {"cert": b64(body), "root_key_id": "root-1", "sig": b64(ROOT.sign(b"orch-release-key/1\n" + body))}


def manifest(seq=7, channel="develop", commit=SHA, version=None):
    return {
        "format": "orch.release/1",
        "channel": channel,
        "version": version or f"2.0.0.dev{seq}",
        "seq": seq,
        "commit": commit,
        "issued_at": "2026-06-01T00:00:00Z",
        "expires_at": "2026-06-22T00:00:00Z",
        "files": [],
        "changelog": "",
        "min_from": "2.0.0.dev1",
    }


def envelope(doc, key=RK, serial=1) -> bytes:
    payload = jcs(doc)
    return jcs(
        {
            "format": "orch.release-envelope/1",
            "payload": b64(payload),
            "sig": {"alg": "ed25519", "key_id": "rk-1", "value": b64(key.sign(b"orch-release/1\n" + payload))},
            "key_cert": cert(serial),
            "revocations": revocations(),
        }
    )


def ref_verify(env: bytes, channel: str, now: datetime) -> dict:
    """Minimal CONTRACT.md section 3 stand-in: cert and revocation signatures, serial, validity, payload signature."""
    from cryptography.exceptions import InvalidSignature

    def ok(pub, sig, msg):
        try:
            Ed25519PrivateKey  # noqa: B018
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            Ed25519PublicKey.from_public_bytes(pub).verify(base64.b64decode(sig), msg)
            return True
        except InvalidSignature:
            return False

    e = json.loads(env)
    kc, rv = e["key_cert"], e["revocations"]
    cb, rb, pb = base64.b64decode(kc["cert"]), base64.b64decode(rv["doc"]), base64.b64decode(e["payload"])
    if not ok(ROOTS[kc["root_key_id"]], kc["sig"], b"orch-release-key/1\n" + cb):
        raise refresh.Refused("bad_cert_sig")
    if not ok(ROOTS[rv["root_key_id"]], rv["sig"], b"orch-revocations/1\n" + rb):
        raise refresh.Refused("bad_revocations_sig")
    c, r = json.loads(cb), json.loads(rb)
    if c["serial"] < r["min_serial"] or c["serial"] in r["revoked"]:
        raise refresh.Refused("key_revoked")
    if not ok(base64.b64decode(c["public_key"]), e["sig"]["value"], b"orch-release/1\n" + pb):
        raise refresh.Refused("bad_sig")
    doc = json.loads(pb)
    if doc["channel"] != channel:
        raise refresh.Refused("wrong_channel")
    return doc


def put(tmp: Path, tag: str, data: bytes) -> None:
    (tmp / tag).mkdir(parents=True, exist_ok=True)
    (tmp / tag / "release.json").write_bytes(data)


def run(tmp, commit_of=lambda tag: SHA, revs=None, channel="develop"):
    return refresh.rebuild_payload(
        tmp, channel, revs or revocations(), "2026-09-01T00:00:00Z", "2026-09-22T00:00:00Z", ref_verify, commit_of
    )


def test_rebuilds_from_the_newest_verified_release_and_changes_only_times(tmp_path):
    put(tmp_path, "v2.0.0.dev6", envelope(manifest(6)))
    put(tmp_path, "v2.0.0.dev7", envelope(manifest(7)))
    put(tmp_path, "v2.0.0.dev9", envelope(manifest(9, channel="main")))  # other channel: skipped
    new = json.loads(run(tmp_path))
    assert new == {**manifest(7), "issued_at": "2026-09-01T00:00:00Z", "expires_at": "2026-09-22T00:00:00Z"}


def test_tampered_release_is_refused(tmp_path):
    env = json.loads(envelope(manifest(7)))
    evil = jcs({**manifest(7), "commit": "b" * 40})
    env["payload"] = b64(evil)  # signature no longer matches
    put(tmp_path, "v2.0.0.dev7", jcs(env))
    with pytest.raises(refresh.Refused, match="bad_sig"):
        run(tmp_path, commit_of=lambda tag: "b" * 40)


def test_unsigned_or_foreign_key_is_refused(tmp_path):
    stranger = Ed25519PrivateKey.from_private_bytes(bytes(range(9, 41)))
    put(tmp_path, "v2.0.0.dev7", envelope(manifest(7), key=stranger))
    with pytest.raises(refresh.Refused, match="bad_sig"):
        run(tmp_path)


@pytest.mark.parametrize(
    ("tag", "commit_of"),
    [
        ("v2.0.0.dev8", lambda t: SHA),  # tag says 8, manifest says 7
        ("v2.0.0.dev7", lambda t: "c" * 40),  # tag points at another commit
    ],
)
def test_mismatch_with_the_git_tag_is_refused(tmp_path, tag, commit_of):
    put(tmp_path, tag, envelope(manifest(7)))
    with pytest.raises(refresh.Refused, match="does not match"):
        run(tmp_path, commit_of=commit_of)


def test_key_revoked_by_the_current_list_is_refused(tmp_path):
    put(tmp_path, "v2.0.0.dev7", envelope(manifest(7), serial=1))
    with pytest.raises(refresh.Refused, match="key_revoked"):
        run(tmp_path, revs=revocations(min_serial=2))


def test_no_release_for_channel_is_nothing_to_refresh(tmp_path):
    put(tmp_path, "v2.0.0.dev9", envelope(manifest(9, channel="main")))
    with pytest.raises(LookupError):
        run(tmp_path)


def test_main_fails_closed_and_writes_nothing(tmp_path):
    """No signing happens without a payload file: a refusal leaves no output (and exits 1, not 0 or 3)."""
    put(tmp_path / "c", "v2.0.0.dev7", b"not json")
    (tmp_path / "rev.json").write_text(json.dumps(revocations()))
    out = tmp_path / "manifest.json"
    res = subprocess.run(
        [
            sys.executable,
            "-I",
            str(SCRIPTS / "refresh.py"),
            "--candidates",
            str(tmp_path / "c"),
            "--channel",
            "develop",
            "--revocations",
            str(tmp_path / "rev.json"),
            "--issued-at",
            "2026-09-01T00:00:00Z",
            "--expires-at",
            "2026-09-22T00:00:00Z",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1 and not out.exists()


def test_published_pointer_must_verify_before_its_seq_counts(tmp_path):
    good = envelope(manifest(7))
    assert (
        refresh.verify_published(good, "develop", revocations(), ref_verify, now=datetime(2026, 9, 1, tzinfo=UTC))[
            "seq"
        ]
        == 7
    )
    unsigned = jcs({"format": "orch.release-envelope/1", "payload": b64(jcs(manifest(99)))})
    for bad in (
        unsigned,
        b"garbage",
        envelope(manifest(99), key=Ed25519PrivateKey.from_private_bytes(bytes(range(9, 41)))),
    ):
        with pytest.raises((refresh.Refused, KeyError)):
            refresh.verify_published(bad, "develop", revocations(), ref_verify, now=datetime(2026, 9, 1, tzinfo=UTC))


def test_channel_seq_script_rejects_an_unsigned_file(tmp_path):
    f = tmp_path / "prev.json"
    f.write_bytes(jcs({"format": "orch.release-envelope/1", "payload": b64(jcs(manifest(99)))}))
    (tmp_path / "rev.json").write_text(json.dumps(revocations()))
    res = subprocess.run(
        [
            sys.executable,
            "-I",
            str(SCRIPTS / "channel_seq.py"),
            str(f),
            "--channel",
            "develop",
            "--revocations",
            str(tmp_path / "rev.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1 and res.stdout == ""
