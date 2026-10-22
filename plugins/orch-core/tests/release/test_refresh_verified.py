"""trusted.trusted_current is the one invariant behind refresh.py and channel_seq.py (see its docstring).

TODO(U2 lands): swap `ref_verify` for orch.update.verify.verify_envelope (the production adapter is
trusted.core_verifier) and drive the same cases with the contract vectors.
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
spec = importlib.util.spec_from_file_location("trusted", SCRIPTS / "trusted.py")
trusted = importlib.util.module_from_spec(spec)
sys.modules["trusted"] = trusted
spec.loader.exec_module(trusted)
refresh = trusted

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


def ref_verify(env: bytes, channel: str, now: datetime, allow_expired: bool) -> dict:
    """Minimal CONTRACT.md section 3 stand-in: signatures, serial, certificate window AT NOW, channel, expiry."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    def ok(pub, sig, msg):
        try:
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
    if not refresh.parse_time(c["not_before"]) <= now < refresh.parse_time(c["not_after"]):
        raise refresh.Refused("cert_expired")
    if c["serial"] < r["min_serial"] or c["serial"] in r["revoked"]:
        raise refresh.Refused("key_revoked")
    if not ok(base64.b64decode(c["public_key"]), e["sig"]["value"], b"orch-release/1\n" + pb):
        raise refresh.Refused("bad_sig")
    doc = json.loads(pb)
    if doc["channel"] != channel:
        raise refresh.Refused("wrong_channel")
    if now >= refresh.parse_time(doc["expires_at"]) and not allow_expired:
        raise refresh.Refused("expired")
    return doc


NOW = datetime(2026, 9, 1, tzinfo=UTC)  # after the manifests' expires_at (2026-06-22): the reason to refresh
TIMES = ("2026-09-01T00:00:00Z", "2026-09-22T00:00:00Z")


def current(pointer, releases, *, revs=None, now=NOW, commit_of=lambda tag: SHA, listed=()):
    return trusted.trusted_current(
        "develop",
        revs or revocations(),
        ref_verify,
        now,
        get_pointer=lambda: pointer,
        get_release=lambda tag: releases.get(tag),
        tag_commit=commit_of,
        releases_for_channel=lambda: list(listed),
    )


def test_refresh_resigns_exactly_the_current_pointer_with_new_times():
    env = envelope(manifest(7))
    t = current(env, {"v2.0.0.dev7": env, "v2.0.0.dev9": envelope(manifest(9))})  # a newer release is ignored
    assert json.loads(trusted.refresh_payload(t, *TIMES)) == {
        **manifest(7),
        "issued_at": TIMES[0],
        "expires_at": TIMES[1],
    }


def test_tampered_or_unsigned_pointer_is_refused():
    env = json.loads(envelope(manifest(7)))
    env["payload"] = b64(jcs({**manifest(7), "commit": "b" * 40}))
    with pytest.raises(refresh.Refused, match="bad_sig"):
        current(jcs(env), {"v2.0.0.dev7": envelope(manifest(7))}, commit_of=lambda t: "b" * 40)
    stranger = Ed25519PrivateKey.from_private_bytes(bytes(range(9, 41)))
    with pytest.raises(refresh.Refused, match="bad_sig"):
        current(envelope(manifest(7), key=stranger), {})
    unsigned = jcs({"format": "orch.release-envelope/1", "payload": b64(jcs(manifest(7)))})
    with pytest.raises(refresh.Refused):
        current(unsigned, {})


def test_lower_seq_rebuild_is_refused():
    t = current(envelope(manifest(7)), {"v2.0.0.dev7": envelope(manifest(7))})
    trusted.check_new_seq(t, 7)
    trusted.check_new_seq(t, 8)
    with pytest.raises(refresh.Refused, match="lower"):
        trusted.check_new_seq(t, 6)
    # the immutable release says an older seq than the pointer: refresh must not rebuild from it
    with pytest.raises(refresh.Refused, match="disagree|agree"):
        current(envelope(manifest(7)), {"v2.0.0.dev7": envelope(manifest(6, version="2.0.0.dev7"))})


def test_deleted_current_version_is_an_error_not_nothing_to_refresh():
    env = envelope(manifest(7))
    with pytest.raises(refresh.Refused, match="missing"):
        current(env, {"v2.0.0.dev9": envelope(manifest(9))})  # newer releases exist, the current one is gone


def test_never_published_is_the_only_exit_3_case():
    with pytest.raises(refresh.NothingToRefresh):
        current(None, {})
    with pytest.raises(refresh.Refused, match="no channel.json"):
        current(None, {}, listed=["v2.0.0.dev7"])


def test_unexpected_exceptions_become_refused():
    def boom(tag):
        raise KeyError("x")

    env = envelope(manifest(7))
    with pytest.raises(refresh.Refused):
        current(env, {"v2.0.0.dev7": env}, commit_of=boom)
    with pytest.raises(refresh.Refused):
        current(b"{}", {})
    with pytest.raises(refresh.Refused):
        trusted.trusted_current(
            "develop",
            revocations(),
            ref_verify,
            NOW,
            get_pointer=lambda: env,
            get_release=lambda t: [][0],
            tag_commit=lambda t: SHA,
            releases_for_channel=list,
        )


def test_backdated_release_from_expired_or_revoked_cert_is_refused():
    env = envelope(manifest(7))  # issued 2026-06-01, inside the cert window
    later = datetime(2028, 1, 1, tzinfo=UTC)  # cert not_after is 2027-01-01
    with pytest.raises(refresh.Refused, match="cert_expired"):
        current(env, {"v2.0.0.dev7": env}, now=later)
    with pytest.raises(refresh.Refused, match="key_revoked"):
        current(env, {"v2.0.0.dev7": env}, revs=revocations(min_serial=2))
    with pytest.raises(refresh.Refused, match="key_revoked"):
        current(env, {"v2.0.0.dev7": env}, revs=revocations(revoked=[1]))


def test_commit_must_match_the_git_tag():
    env = envelope(manifest(7))
    with pytest.raises(refresh.Refused, match="git tag"):
        current(env, {"v2.0.0.dev7": env}, commit_of=lambda t: "c" * 40)


def _run(script, tmp_path, *args):
    return subprocess.run([sys.executable, "-I", str(SCRIPTS / script), *args], capture_output=True, text=True)


@pytest.mark.parametrize("content", [b"{}", b"not json", None])
def test_scripts_fail_closed_with_exit_1(tmp_path, content):
    """A KeyError or unreadable pointer is exit 1 (never 3, never 0) and nothing is written for signing."""
    (tmp_path / "rev.json").write_text(json.dumps(revocations()))
    url = (tmp_path / "channel.json").as_uri()
    if content is not None:
        (tmp_path / "channel.json").write_bytes(content)
    out = tmp_path / "manifest.json"
    common = ["--channel", "develop", "--pointer-url", url, "--revocations", str(tmp_path / "rev.json")]
    r = _run("refresh.py", tmp_path, *common, "--issued-at", TIMES[0], "--expires-at", TIMES[1], "--out", str(out))
    assert r.returncode == 1 and not out.exists()
    r = _run("channel_seq.py", tmp_path, *common, "--new-seq", "99")
    assert r.returncode == 1 and r.stdout == ""
