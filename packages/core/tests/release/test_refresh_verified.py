"""trusted.trusted_current is the one invariant behind refresh.py and channel_seq.py (see its docstring).

Envelopes are signed with the public TEST keys and checked by the real verifier (orch.update.verify).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from orch.update import keys as keys_mod

SCRIPTS = Path(__file__).resolve().parents[4] / "scripts" / "release"
spec = importlib.util.spec_from_file_location("trusted", SCRIPTS / "trusted.py")
trusted = importlib.util.module_from_spec(spec)
sys.modules["trusted"] = trusted
spec.loader.exec_module(trusted)
refresh = trusted

ROOT = Ed25519PrivateKey.from_private_bytes(
    hashlib.sha256(b"orch-release TEST key root-1; never trust outside tests").digest()
)  # the public TEST root
RK = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
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
    return {"doc": b64(body), "root_key_id": "test-root-1", "sig": b64(ROOT.sign(b"orch-revocations/1\n" + body))}


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
    return {"cert": b64(body), "root_key_id": "test-root-1", "sig": b64(ROOT.sign(b"orch-release-key/1\n" + body))}


FILES = [
    {"role": r, "name": n, "sha256": "d" * 64, "size": 1, "url": f"https://example.invalid/{n}"}
    for r, n in (("core", "c.whl"), ("dashboard", "d.whl"), ("constraints", "constraints.txt"))
]


def manifest(seq=7, channel="develop", commit=SHA, version=None):
    return {
        "format": "orch.release/1",
        "channel": channel,
        "version": version or f"2.0.0.dev{seq}",
        "seq": seq,
        "commit": commit,
        "issued_at": "2026-06-01T00:00:00Z",
        "expires_at": "2026-06-22T00:00:00Z",
        "files": FILES,
        "changelog": "x",
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


@pytest.fixture(autouse=True)
def _test_root():
    with keys_mod.enable_test_root_for_tests():
        yield


def ref_verify(env: bytes, channel: str, now: datetime, allow_expired: bool) -> dict:
    """The real verifier (orch.update.verify, U2) against the TEST root, via the production adapter."""
    return trusted.core_verifier()(env, channel, now, allow_expired)


NOW = datetime(2026, 9, 1, tzinfo=UTC)  # after the manifests' expires_at (2026-06-22): the reason to refresh
TIMES = ("2026-09-01T00:00:00Z", "2026-09-22T00:00:00Z")


def current(pointer, releases, *, revs=None, now=NOW, commit_of=lambda tag: SHA, listed=None):
    return trusted.trusted_current(
        "develop",
        revs or revocations(),
        ref_verify,
        now,
        get_pointer=lambda: pointer,
        get_release=lambda tag: releases.get(tag),
        tag_commit=commit_of,
        releases_for_channel=lambda: list(releases if listed is None else listed),
    )


def test_refresh_resigns_exactly_the_current_pointer_with_new_times():
    env = envelope(manifest(7))
    t = current(env, {"v2.0.0.dev7": env, "v2.0.0.dev5": envelope(manifest(5))})  # an older release is ignored
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
    with pytest.raises(refresh.Refused, match="disagree|agree|rollback"):
        current(envelope(manifest(7)), {"v2.0.0.dev7": envelope(manifest(6, version="2.0.0.dev7"))})


def test_deleted_current_version_is_an_error_not_nothing_to_refresh():
    env = envelope(manifest(7))
    with pytest.raises(refresh.Refused, match="missing"):
        current(env, {"v2.0.0.dev9": envelope(manifest(9))})  # newer releases exist, the current one is gone


def test_never_published_is_the_only_exit_3_case():
    with pytest.raises(refresh.NothingToRefresh):
        current(None, {})
    with pytest.raises(refresh.Refused, match="no channel.json"):
        current(None, {"v2.0.0.dev7": envelope(manifest(7))}, listed=["v2.0.0.dev7"])


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


def test_expired_pointer_with_a_bad_signature_or_revoked_or_expired_cert_is_refused():
    """allow_expired skips only the manifest expiry: every other rule still refuses (no fail-open on 'expired')."""
    assert NOW >= trusted.parse_time(manifest(7)["expires_at"])  # the pointer is expired in all cases below
    stranger = Ed25519PrivateKey.from_private_bytes(bytes(range(9, 41)))
    good = envelope(manifest(7))
    cases = [
        (envelope(manifest(7), key=stranger), {}, NOW, "bad_sig"),
        (good, {}, NOW, None),  # control: valid but expired is accepted as the reason to refresh
        (good, {"revs": revocations(min_serial=2)}, NOW, "key_revoked"),
        (good, {"revs": revocations(revoked=[1])}, NOW, "key_revoked"),
        (good, {}, datetime(2028, 1, 1, tzinfo=UTC), "cert_expired"),
    ]
    for env, kw, now, reason in cases:
        if reason is None:
            assert current(env, {"v2.0.0.dev7": env}, now=now, **kw).seq == 7
        else:
            with pytest.raises(trusted.Refused, match=reason):
                current(env, {"v2.0.0.dev7": env}, now=now, **kw)
    with pytest.raises(trusted.Refused, match="bad_sig"):  # a bad-sig release behind a good pointer is refused too
        current(good, {"v2.0.0.dev7": envelope(manifest(7), key=stranger)})


def test_a_stable_release_version_is_a_valid_tag():
    """Pilot releases on main are plain 2.0.0 (no .dev<seq>): the tag still has to name the manifest's version."""
    env = envelope(manifest(7, version="2.0.0"))
    assert current(env, {"v2.0.0": env}).tag == "v2.0.0"
    with pytest.raises(trusted.Refused):
        current(env, {})  # the immutable release of that version is missing


def test_a_rolled_back_pointer_is_refused():
    """The pointer names seq 7 but an immutable release of seq 9 exists: never re-sign the old one as fresh."""
    old = envelope(manifest(7))
    with pytest.raises(trusted.Refused, match="rollback"):
        current(old, {"v2.0.0.dev7": old, "v2.0.0.dev9": envelope(manifest(9))})


def test_refresh_takes_the_highest_verified_release_not_the_pointers_choice():
    new = envelope(manifest(9))
    t = current(new, {"v2.0.0.dev9": new, "v2.0.0.dev7": envelope(manifest(7)), "v2.0.0.dev8": envelope(manifest(8))})
    assert t.seq == 9 and t.tag == "v2.0.0.dev9"


def test_an_unverifiable_listed_release_stops_the_refresh():
    new = envelope(manifest(9))
    stranger = Ed25519PrivateKey.from_private_bytes(bytes(range(40, 72)))
    with pytest.raises(trusted.Refused):
        current(new, {"v2.0.0.dev9": new, "v2.0.0.dev7": envelope(manifest(7), key=stranger)})


def test_a_kept_tag_whose_release_is_retitled_or_deleted_is_refused():
    """The anchor list is git tags: hiding the highest release (retitle/delete) while its tag stays must not help."""
    old = envelope(manifest(7))
    # tag v2.0.0.dev9 exists (listed) but its release is gone; the pointer was rolled back to seq 7
    with pytest.raises(trusted.Refused, match="missing"):
        current(old, {"v2.0.0.dev7": old}, listed=["v2.0.0.dev7", "v2.0.0.dev9"])


def test_main_skips_test_candidate_tags_that_a_production_verifier_cannot_check():
    env = envelope(manifest(7, channel="main", version="2.0.0"))
    cand = envelope(manifest(3, channel="test-x"))  # the test cert lists no test-*: unverifiable here, like in prod
    t = trusted.trusted_current(
        "main", revocations(), ref_verify, NOW,
        get_pointer=lambda: env, get_release=lambda tag: {"v2.0.0": env, "v2.0.0.dev3": cand}.get(tag),
        tag_commit=lambda tag: SHA, releases_for_channel=lambda: ["v2.0.0", "v2.0.0.dev3"],
    )  # fmt: skip
    assert t.tag == "v2.0.0"


def test_a_verified_release_of_another_channel_is_skipped():
    other = envelope(manifest(9, channel="develop"))
    mine = envelope(manifest(7, channel="main", version="2.0.0"))
    t = trusted.trusted_current(
        "main", revocations(), ref_verify, NOW,
        get_pointer=lambda: mine, get_release=lambda tag: {"v2.0.0": mine, "v2.0.0.dev9": other}.get(tag),
        tag_commit=lambda tag: SHA, releases_for_channel=lambda: ["v2.0.0", "v2.0.0.dev9"],
    )  # fmt: skip
    assert t.seq == 7  # the higher seq belongs to develop: not an anchor of main


def test_the_first_publish_of_a_new_channel_works_after_a_main_release():
    """Tags of other channels exist, this channel has no pointer yet: NothingToRefresh, not a refusal."""
    main = envelope(manifest(7, channel="main", version="2.0.0"))
    with pytest.raises(trusted.NothingToRefresh):
        trusted.trusted_current(
            "develop", revocations(), ref_verify, NOW,
            get_pointer=lambda: None, get_release=lambda tag: {"v2.0.0": main}.get(tag),
            tag_commit=lambda tag: SHA, releases_for_channel=lambda: ["v2.0.0"],
        )  # fmt: skip


def test_an_unverifiable_release_still_refuses():
    bad = envelope(manifest(9, channel="develop"), key=Ed25519PrivateKey.from_private_bytes(bytes(range(40, 72))))
    mine = envelope(manifest(7))
    with pytest.raises(trusted.Refused):
        current(mine, {"v2.0.0.dev7": mine, "v2.0.0.dev9": bad})
