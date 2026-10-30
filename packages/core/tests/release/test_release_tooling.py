"""U3 release tooling: regenerates the TEST vectors' envelopes with scripts/release/*.py and compares bytes."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
TOOLS = ROOT / "scripts" / "release"
VEC = Path(__file__).resolve().parents[1] / "vectors" / "release"
sys.path.insert(0, str(TOOLS))

import check_wheels  # noqa: E402
import manifest  # noqa: E402
import rootkey  # noqa: E402
import sign  # noqa: E402

KEYS = json.loads((VEC / "keys.json").read_text())
CORE = {c["name"]: c for c in json.loads((VEC / "core.json").read_text())["cases"]}
CATALOG = {c["name"]: c for c in json.loads((VEC / "catalog.json").read_text())["cases"]}
NOW = "2026-10-22T08:00:00Z"
FILE_BYTES = {
    "core": b"TEST orch-core wheel bytes (not a real wheel)\n",
    "dashboard": b"TEST orch-dashboard wheel bytes (not a real wheel)\n",
    "hub": b"TEST orch-hub wheel bytes (not a real wheel)\n",
    "relay": b"TEST orch-relay wheel bytes (not a real wheel)\n",
    "publish": b"TEST orch-publish wheel bytes (not a real wheel)\n",
    "constraints": b"cryptography==46.0.3\njsonschema==4.26.0\nsegno==1.6.6\n",
}


def seed_b64(name: str) -> str:
    return base64.b64encode(bytes.fromhex(KEYS["keys"][name]["seed_hex_TEST_ONLY"])).decode()


def key(name: str, monkeypatch, env="ORCH_TEST_KEY"):
    monkeypatch.setenv(env, seed_b64(name))
    return sign.load_key(env)


def reference_verify(case: dict) -> str:
    """Verify with the real verifier (orch.update.verify) against the TEST root; returns the reason."""
    from datetime import datetime

    from orch.update.keys import enable_test_root_for_tests
    from orch.update.verify import UpdateRefused, verify_envelope

    st = case["state"]
    roots = {k: base64.b64decode(v) for k, v in KEYS["trusted_roots"].items() if k in st["trusted_roots"]}
    files = {f["name"]: base64.b64decode(f["content_b64"]) for f in case["files"]} if "files" in case else None
    try:
        with enable_test_root_for_tests():  # test-* channels are refused by a production verifier
            verify_envelope(
                case["envelope"].encode(),
                purpose=case["purpose"],
                channel=case["channel"],
                now=datetime.fromisoformat(case["now"].replace("Z", "+00:00")),
                seq_seen=st["seq_seen"],
                revocations_issued_at=st["revocations_issued_at"],
                files=files,
                roots=roots,
            )
    except UpdateRefused as e:
        return e.reason.value
    return "ok"


def test_vectors_still_the_test_set():
    assert KEYS["test_only"] is True


def wheels(tmp: Path, version: str) -> dict[str, Path]:
    names = {
        "core": f"orch_core-{version}-py3-none-any.whl",
        "dashboard": f"orch_dashboard-{version}-py3-none-any.whl",
        "hub": f"orch_hub-{version}-py3-none-any.whl",
        "relay": f"orch_relay-{version}-py3-none-any.whl",
        "publish": f"orch_publish-{version}-py3-none-any.whl",
        "constraints": "constraints.txt",
    }
    out = {}
    for role, name in names.items():
        out[role] = tmp / name
        out[role].write_bytes(FILE_BYTES[role])
    return out


def build(tmp: Path, channel: str, seq: int) -> bytes:
    version = f"2.0.0.dev{seq}"
    w = wheels(tmp, version)
    doc = manifest.build_manifest(
        channel=channel,
        version=version,
        seq=seq,
        commit=hashlib.sha1(f"TEST commit {channel} {seq}".encode()).hexdigest(),
        issued_at="2026-10-21T07:40:00Z",
        expires_at="2026-11-11T07:40:00Z",
        files=w,
        base_url=f"https://github.com/severinlindenmann/orch-core/releases/download/v{version}",
        changelog="TEST release. Wiki add-on: page links; fix terminal resize.",
        min_from="2.0.0.dev1",
    )
    return manifest.jcs(doc)


def ceremony(key_id: str, monkeypatch):
    """key_cert and revocations made by rootkey.py from the TEST root key."""
    root = key("root-1", monkeypatch, "ORCH_TEST_ROOT")
    c = KEYS["certs"][key_id]
    cert = rootkey.build_cert(
        key_id=c["key_id"],
        public_key=base64.b64decode(c["public_key"]),
        serial=c["serial"],
        purpose=c["purpose"],
        channels=c["channels"],
        not_before=c["not_before"],
        not_after=c["not_after"],
    )
    r = KEYS["revocations"]
    revs = rootkey.build_revocations(min_serial=r["min_serial"], revoked=r["revoked"], issued_at=r["issued_at"])
    return rootkey.certify(root, "root-1", cert), rootkey.revoke(root, "root-1", revs)


@pytest.mark.parametrize(
    "vector,purpose,key_id,channel,seq",
    [
        ("ok_develop", "core", "rk-3", "develop", 42),
        ("ok_main", "core", "rk-3", "main", 40),
        ("ok_test_channel", "core", "tk-1", "test-ci", 43),
    ],
)
def test_core_envelope_reproduces_vector_bytes(tmp_path, monkeypatch, vector, purpose, key_id, channel, seq):
    payload = build(tmp_path, channel, seq)
    kc, rv = ceremony(key_id, monkeypatch)
    k = key(key_id, monkeypatch)
    out = sign.sign_envelope(payload, purpose=purpose, key=k, key_id=key_id, key_cert=kc, revocations=rv)
    assert out.decode() == CORE[vector]["envelope"]
    assert reference_verify({**CORE[vector], "envelope": out.decode()}) == "ok"


def test_catalog_envelope_reproduces_vector_bytes(monkeypatch):
    sample = json.loads(base64.b64decode(json.loads(CATALOG["ok_catalog_develop"]["envelope"])["payload"]))
    payload = manifest.jcs(
        manifest.build_catalog(
            channel="develop",
            seq=311,
            issued_at=sample["issued_at"],
            expires_at=sample["expires_at"],
            addons=sample["addons"],
        )
    )
    kc, rv = ceremony("ck-1", monkeypatch)
    out = sign.sign_envelope(
        payload, purpose="addon-catalog", key=key("ck-1", monkeypatch), key_id="ck-1", key_cert=kc, revocations=rv
    )
    assert out.decode() == CATALOG["ok_catalog_develop"]["envelope"]
    assert reference_verify({**CATALOG["ok_catalog_develop"], "envelope": out.decode()}) == "ok"


def test_cli_round_trip_and_files_match_manifest(tmp_path, monkeypatch):
    w = wheels(tmp_path, "2.0.0.dev42")
    (tmp_path / "log.txt").write_text("TEST release. Wiki add-on: page links; fix terminal resize.\n")
    env = {
        "PATH": "/usr/bin:/bin",
        "ORCH_RELEASE_SIGNING_KEY": seed_b64("rk-3"),
        "ORCH_ROOT_SIGNING_KEY": seed_b64("root-1"),
    }
    py = [sys.executable]

    def run(script, *args):
        r = subprocess.run([*py, str(TOOLS / script), *args], env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert seed_b64("rk-3") not in r.stdout + r.stderr
        return r

    c = KEYS["certs"]["rk-3"]
    run(
        "rootkey.py",
        "certify",
        "--root-key-id",
        "root-1",
        "--key-id",
        "rk-3",
        "--public-key",
        c["public_key"],
        "--serial",
        "3",
        "--purpose",
        "core",
        "--channels",
        "develop,main",
        "--not-before",
        c["not_before"],
        "--not-after",
        c["not_after"],
        "--out",
        str(tmp_path / "kc.json"),
    )
    run(
        "rootkey.py",
        "revoke",
        "--root-key-id",
        "root-1",
        "--min-serial",
        "2",
        "--revoked",
        "4",
        "--initial",
        "--issued-at",
        KEYS["revocations"]["issued_at"],
        "--out",
        str(tmp_path / "rv.json"),
    )
    run(
        "manifest.py",
        "core",
        "--channel",
        "develop",
        "--seq",
        "42",
        "--version",
        "2.0.0.dev42",
        "--commit",
        hashlib.sha1(b"TEST commit develop 42").hexdigest(),
        "--issued-at",
        "2026-10-21T07:40:00Z",
        "--expires-at",
        "2026-11-11T07:40:00Z",
        *[a for role, path in w.items() for a in ("--file", f"{role}={path}")],
        "--base-url",
        "https://github.com/severinlindenmann/orch-core/releases/download/v2.0.0.dev42",
        "--changelog-file",
        str(tmp_path / "log.txt"),
        "--min-from",
        "2.0.0.dev1",
        "--out",
        str(tmp_path / "m.json"),
    )
    run(
        "sign.py",
        "--purpose",
        "core",
        "--payload",
        str(tmp_path / "m.json"),
        "--key-id",
        "rk-3",
        "--key-cert",
        str(tmp_path / "kc.json"),
        "--revocations",
        str(tmp_path / "rv.json"),
        "--out",
        str(tmp_path / "e.json"),
    )
    assert (tmp_path / "e.json").read_text() == CORE["ok_develop"]["envelope"]


def test_sign_has_no_key_argument_and_needs_the_env_var(tmp_path):
    r = subprocess.run([sys.executable, str(TOOLS / "sign.py"), "--help"], capture_output=True, text=True)
    assert "--key-file" not in r.stdout and "--key " not in r.stdout
    r = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "sign.py"),
            "--purpose",
            "core",
            "--payload",
            "x",
            "--key-id",
            "rk-3",
            "--key-cert",
            "x",
            "--revocations",
            "x",
            "--out",
            str(tmp_path / "o"),
        ],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1 and "ORCH_RELEASE_SIGNING_KEY" in r.stderr


def test_bad_key_value_is_not_echoed(monkeypatch):
    monkeypatch.setenv("ORCH_TEST_KEY", "SECRETSECRET!!")
    with pytest.raises(ValueError) as e:
        sign.load_key("ORCH_TEST_KEY")
    assert "SECRET" not in str(e.value)


def test_sign_refuses_wrong_key_cert_and_channel(tmp_path, monkeypatch):
    payload = build(tmp_path, "develop", 42)
    kc, rv = ceremony("rk-3", monkeypatch)
    other = key("rk-1", monkeypatch)
    with pytest.raises(ValueError, match="another key"):
        sign.sign_envelope(payload, purpose="core", key=other, key_id="rk-3", key_cert=kc, revocations=rv)
    kc4, rv4 = ceremony("tk-1", monkeypatch)
    with pytest.raises(ValueError, match="channel"):
        sign.sign_envelope(
            payload, purpose="core", key=key("tk-1", monkeypatch), key_id="tk-1", key_cert=kc4, revocations=rv4
        )
    with pytest.raises(ValueError, match="canonical"):
        sign.sign_envelope(
            b'{"format": "orch.release/1"}',
            purpose="core",
            key=key("rk-3", monkeypatch),
            key_id="rk-3",
            key_cert=kc,
            revocations=rv,
        )


def test_manifest_validation(tmp_path):
    w = wheels(tmp_path, "2.0.0.dev42")
    good = dict(
        channel="develop",
        version="2.0.0.dev42",
        seq=42,
        commit="a" * 40,
        issued_at="2026-10-21T07:40:00Z",
        expires_at="2026-11-11T07:40:00Z",
        files=w,
        base_url="https://x.example/d",
        changelog="c",
        min_from="2.0.0.dev1",
    )
    assert manifest.build_manifest(**good)["files"][0]["name"] == w["core"].name
    for bad in (
        {"channel": "Main"},
        {"seq": 0},
        {"commit": "abc"},
        {"version": "2"},
        {"base_url": "http://x"},
        {"changelog": "é" * 3000},
        {"expires_at": "2026-10-01T00:00:00Z"},
        {"issued_at": "2026-10-21 07:40:00"},
    ):
        with pytest.raises(ValueError):
            manifest.build_manifest(**{**good, **bad})


def test_rootkey_validation():
    for bad in (
        {"serial": 0},
        {"purpose": "x"},
        {"channels": ["main", "main"]},
        {"channels": ["foo"]},
        {"key_id": "Rk"},
        {"not_after": "2026-01-01T00:00:00Z"},
    ):
        args = dict(
            key_id="rk-3",
            public_key=b"\0" * 32,
            serial=3,
            purpose="core",
            channels=["main"],
            not_before="2026-10-01T00:00:00Z",
            not_after="2027-03-30T00:00:00Z",
        )
        with pytest.raises(ValueError):
            rootkey.build_cert(**{**args, **bad})
    assert rootkey.build_revocations(min_serial=2, revoked=[5, 4, 4], issued_at="2026-10-20T00:00:00Z")["revoked"] == [
        4,
        5,
    ]


REL = ["--version", "2.0.0", "--channel", "main", "--commit", "a" * 7]


def make_wheel(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return path


def test_check_wheels(tmp_path):
    build_json = json.dumps(
        {"version": "2.0.0", "channel": "main", "commit": "a" * 7, "built_at": "2026-10-22T00:00:00Z"}
    )
    ok_core = make_wheel(tmp_path / "c.whl", {"orch/__init__.py": b"", "orch/_build.json": build_json.encode()})
    no_core = make_wheel(tmp_path / "c2.whl", {"orch/__init__.py": b""})
    ok_dash = make_wheel(
        tmp_path / "d.whl", {"orch_dashboard/static/index.html": b"<html>", "orch_dashboard/orch-addon.json": b"{}"}
    )
    no_dash = make_wheel(tmp_path / "d2.whl", {"orch_dashboard/orch-addon.json": b"{}"})
    assert check_wheels.check_core(ok_core) == [] and check_wheels.check_dashboard(ok_dash) == []
    assert "_build.json" in check_wheels.check_core(no_core)[0]
    assert "index.html" in check_wheels.check_dashboard(no_dash)[0]
    assert check_wheels.main(["--core", str(ok_core), "--dashboard", str(ok_dash), *REL]) == 0
    assert check_wheels.main(["--core", str(no_core), "--dashboard", str(ok_dash), *REL]) == 1
    assert check_wheels.main(["--core", str(ok_core), "--dashboard", str(ok_dash), *REL[:4], "--commit", "b" * 7]) == 1
    assert (
        check_wheels.main(
            ["--core", str(ok_core), "--dashboard", str(ok_dash), *REL[:2], "--channel", "develop", *REL[4:]]
        )
        == 1
    )
    assert check_wheels.main(["--core", str(ok_core), "--dashboard", str(ok_dash), "--version", "2.0.1", *REL[2:]]) == 1
    assert check_wheels.check_core(tmp_path / "missing.whl")


# --- the manifest file list ---------------------------------------------------------------------------------------


def _build(tmp_path, files):
    return manifest.build_manifest(
        channel="main",
        version="2.0.0",
        seq=1,
        commit="a" * 40,
        issued_at="2026-10-21T07:40:00Z",
        expires_at="2026-11-11T07:40:00Z",
        files=files,
        base_url="https://example.invalid/download/v2.0.0",
        changelog="TEST",
        min_from="2.0.0.dev0",
    )


def test_file_list_holds_every_role_in_role_order(tmp_path):
    doc = _build(tmp_path, wheels(tmp_path, "2.0.0"))
    assert [f["role"] for f in doc["files"]] == list(manifest.ROLES)
    assert all(set(f) == {"role", "name", "sha256", "size", "url"} for f in doc["files"])


def test_file_list_may_omit_optional_roles(tmp_path):
    w = wheels(tmp_path, "2.0.0")
    doc = _build(tmp_path, {r: w[r] for r in ("core", "constraints")})
    assert [f["role"] for f in doc["files"]] == ["core", "constraints"]


@pytest.mark.parametrize(
    "mutate, why",
    [
        (lambda w: w.pop("core"), "core"),
        (lambda w: w.pop("constraints"), "constraints"),
        (lambda w: w.update(installer=w["core"]), "unknown file role"),
    ],
)
def test_builder_refuses_missing_and_unknown_roles(tmp_path, mutate, why):
    w = wheels(tmp_path, "2.0.0")
    mutate(w)
    with pytest.raises(ValueError, match=why):
        _build(tmp_path, w)


def test_cli_refuses_a_duplicate_role(tmp_path):
    w = wheels(tmp_path, "2.0.0")
    (tmp_path / "log.txt").write_text("TEST\n")
    args = [
        "core", "--channel", "main", "--seq", "1", "--issued-at", "2026-10-21T07:40:00Z",
        "--expires-at", "2026-11-11T07:40:00Z", "--version", "2.0.0", "--commit", "a" * 40,
        "--base-url", "https://example.invalid/d", "--changelog-file", str(tmp_path / "log.txt"),
        "--min-from", "2.0.0.dev0", "--out", str(tmp_path / "m.json"),
        *[a for r, p in w.items() for a in ("--file", f"{r}={p}")],
        "--file", f"hub={w['hub']}",
    ]  # fmt: skip
    assert manifest.main(args) == 1
    assert not (tmp_path / "m.json").exists()
