"""Opus review of U3 (rev-release): sign.py content checks, revocation successor rule, stamp comparison."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests.release.test_release_tooling import (
    CORE,
    KEYS,
    TOOLS,
    build,
    ceremony,
    key,
    manifest,
    rootkey,
    seed_b64,
    sign,
)


def _payload(**over):
    m = {
        "format": "orch.release/1",
        "channel": "develop",
        "version": "2.0.0.dev42",
        "seq": 42,
        "commit": "a" * 40,
        "issued_at": "2026-10-21T07:40:00Z",
        "expires_at": "2026-11-11T07:40:00Z",
        "files": [],
        "changelog": "",
        "min_from": "2.0.0.dev1",
    }
    m.update(over)
    return manifest.jcs(m)


def _kc_rv():
    env = json.loads(CORE["ok_develop"]["envelope"])
    return env["key_cert"], env["revocations"]


def test_sign_refuses_a_payload_the_verifier_would_refuse(monkeypatch):
    kc, rv = _kc_rv()
    k = key("rk-3", monkeypatch)
    for bad in (_payload(), _payload(seq=0), _payload(extra=1), _payload(expires_at="2020-01-01T00:00:00Z")):
        with pytest.raises(ValueError):
            sign.sign_envelope(bad, purpose="core", key=k, key_id="rk-3", key_cert=kc, revocations=rv)


def test_sign_refuses_duplicate_and_path_names_and_bad_files(tmp_path, monkeypatch):
    kc, rv = _kc_rv()
    k = key("rk-3", monkeypatch)
    good = json.loads(build(tmp_path, "develop", 42))
    dup = json.loads(json.dumps(good))
    dup["files"][1]["name"] = dup["files"][0]["name"]
    trav = json.loads(json.dumps(good))
    trav["files"][0]["name"] = "../x.whl"
    for bad in (dup, trav):
        with pytest.raises(ValueError):
            sign.sign_envelope(manifest.jcs(bad), purpose="core", key=k, key_id="rk-3", key_cert=kc, revocations=rv)
    # sizes and hashes against the real files
    ok = manifest.jcs(good)
    sign.sign_envelope(ok, purpose="core", key=k, key_id="rk-3", key_cert=kc, revocations=rv, files_dir=tmp_path)
    (tmp_path / good["files"][0]["name"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="size"):
        sign.sign_envelope(ok, purpose="core", key=k, key_id="rk-3", key_cert=kc, revocations=rv, files_dir=tmp_path)


@pytest.mark.parametrize("value", ["not base64 !!", "AAAA", "x" * 80])
def test_bad_key_never_echoed_on_cli(tmp_path, value):
    kc, rv = _kc_rv()
    (tmp_path / "kc.json").write_text(json.dumps(kc))
    (tmp_path / "rv.json").write_text(json.dumps(rv))
    (tmp_path / "m.json").write_bytes(_payload())
    r = subprocess.run(
        [sys.executable, "-I", str(TOOLS / "sign.py"), "--purpose", "core", "--payload", str(tmp_path / "m.json"),
         "--key-id", "rk-3", "--key-cert", str(tmp_path / "kc.json"), "--revocations", str(tmp_path / "rv.json"),
         "--out", str(tmp_path / "o.json")],
        capture_output=True, text=True, env={"ORCH_RELEASE_SIGNING_KEY": value, "PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    assert r.returncode != 0 and value not in r.stdout + r.stderr and "Traceback" not in r.stderr


def test_revocation_successor_rule(monkeypatch):
    _, prev_obj = ceremony("rk-3", monkeypatch)
    prev = rootkey.build_revocations(**{k: KEYS["revocations"][k] for k in ("min_serial", "revoked", "issued_at")})
    ok = rootkey.build_revocations(min_serial=2, revoked=[4, 9], issued_at="2026-10-20T00:00:00Z")
    rootkey.check_successor(ok, prev)
    for bad in (
        dict(min_serial=2, revoked=[4, 9], issued_at="2000-01-01T00:00:00Z"),
        dict(min_serial=2, revoked=[9], issued_at="2026-10-21T00:00:00Z"),
        dict(min_serial=1, revoked=[4], issued_at="2026-10-21T00:00:00Z"),
    ):
        with pytest.raises(ValueError):
            rootkey.check_successor(rootkey.build_revocations(**bad), prev)


def test_revoke_cli_needs_previous_or_initial(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "ORCH_ROOT_SIGNING_KEY": seed_b64("root-1")}
    base = [sys.executable, str(TOOLS / "rootkey.py"), "revoke", "--root-key-id", "test-root-1", "--min-serial", "2",
            "--revoked", "4", "--issued-at", "2000-01-01T00:00:00Z", "--out", str(tmp_path / "o.json")]  # fmt: skip
    assert subprocess.run(base, env=env, capture_output=True).returncode == 1
    (tmp_path / "prev.json").write_text(json.dumps(json.loads(CORE["ok_develop"]["envelope"])["revocations"]))
    r = subprocess.run([*base, "--previous", str(tmp_path / "prev.json")], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "older" in r.stderr and not (tmp_path / "o.json").exists()
