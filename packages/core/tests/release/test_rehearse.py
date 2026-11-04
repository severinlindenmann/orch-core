"""Offline rehearsal CLI, with a fake gh and public TEST signing keys only."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from tests.install_sh._release import trusting_test_root
from tests.release.test_release_tooling import TOOLS, ceremony, key, manifest, sign, wheels

TAG = "test-v2.0.0.dev43"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    scripts = repo / "scripts" / "release"
    scripts.mkdir(parents=True)
    for name in ("rehearse.py", "wheel_hashes.py"):
        shutil.copyfile(TOOLS / name, scripts / name)
    source = (TOOLS.parents[1] / "install.sh").read_text()
    (repo / "install.sh").write_text(trusting_test_root(source))
    assets = tmp_path / "assets"
    assets.mkdir()
    files = wheels(assets, "2.0.0.dev43")
    now = datetime.now(UTC).replace(microsecond=0)

    def stamp(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    doc = manifest.build_manifest(
        channel="test-cut",
        version="2.0.0.dev43",
        seq=43,
        commit="a" * 40,
        issued_at=stamp(now - timedelta(minutes=1)),
        expires_at=stamp(now + timedelta(days=1)),
        files=files,
        base_url=f"https://example.invalid/{TAG}",
        changelog="TEST",
        min_from="2.0.0.dev0",
    )
    cert, _ = ceremony("tk-1", monkeypatch)
    import rootkey

    revs = rootkey.revoke(
        key("root-1", monkeypatch),
        "test-root-1",
        rootkey.build_revocations(min_serial=1, revoked=[], issued_at=stamp(now)),
    )
    (assets / "release.json").write_bytes(
        sign.sign_envelope(
            manifest.jcs(doc),
            purpose="core",
            key=key("tk-1", monkeypatch),
            key_id="tk-1",
            key_cert=cert,
            revocations=revs,
        )
    )
    (assets / "changelog.txt").write_text("TEST\n")
    (assets / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
            for p in sorted(assets.iterdir())
            if p.name != "release.json"
        )
    )
    fake = tmp_path / "bin"
    fake.mkdir()
    gh = fake / "gh"
    gh.write_text(
        f"#!{sys.executable}\nimport os, pathlib, shutil, sys\n"
        f"assert sys.argv[1:4] == ['release', 'download', {TAG!r}]\n"
        "assert sys.argv[4] == '-D' and len(sys.argv) == 6\n"
        "shutil.copytree(os.environ['FAKE_ASSETS'], sys.argv[5])\n"
    )
    gh.chmod(0o700)
    monkeypatch.setenv("PATH", str(fake) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_ASSETS", str(assets))
    return repo, scripts / "rehearse.py", assets, tmp_path / "download with spaces"


def run(lab, *args):
    return subprocess.run([sys.executable, "-I", str(lab[1]), *map(str, args)], capture_output=True, text=True)


def test_fetch(lab):
    result = run(lab, "fetch", TAG, lab[3])
    assert result.returncode == 0, result.stderr
    assert result.stdout == "SHA256SUMS: ok\nrelease.json: ok\n"


@pytest.mark.parametrize("damage", ["wheel", "extra", "missing", "signature", "tag"])
def test_fetch_refuses(lab, damage):
    assets = lab[2]
    if damage == "wheel":
        next(assets.glob("*.whl")).write_bytes(b"tampered")
    elif damage == "extra":
        (assets / "extra.txt").write_text("extra")
    elif damage == "missing":
        (assets / "constraints.txt").unlink()
    elif damage == "signature":
        import json

        envelope = json.loads((assets / "release.json").read_bytes())
        envelope["sig"]["value"] = "A" * 86 + "=="
        (assets / "release.json").write_bytes(manifest.jcs(envelope))
    result = run(lab, "fetch", "v2.0.0" if damage == "tag" else TAG, lab[3])
    assert result.returncode == 1
    assert "release.json: ok" not in result.stdout


def test_installer(lab):
    original = (lab[0] / "install.sh").read_text()
    result = run(lab, "installer", lab[3])
    assert result.returncode == 0, result.stderr
    target = lab[3] / "install-rehearsal.sh"
    assert target.read_text() == (
        "# REHEARSAL COPY: accepts test-* channels; never distribute\n"
        + original.replace("ALLOW_TEST_CHANNELS = False", "ALLOW_TEST_CHANNELS = True", 1)
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert (lab[0] / "install.sh").read_text() == original
    commands = result.stdout.splitlines()
    assert len(commands) == 2 and "--server" in commands[0] and "--server" not in commands[1]


@pytest.mark.parametrize("count", [0, 2])
def test_installer_requires_one_allowance(lab, count):
    (lab[0] / "install.sh").write_text("ALLOW_TEST_CHANNELS = False\n" * count)
    result = run(lab, "installer", lab[3])
    assert result.returncode == 1
    assert "exactly one" in result.stderr
    assert not (lab[3] / "install-rehearsal.sh").exists()
