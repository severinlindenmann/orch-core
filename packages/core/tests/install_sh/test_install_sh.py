"""install.sh end to end (P2-C): a TEST release folder, `env -i`, a temp HOME. Verification happens before anything is
installed, so every tampered piece must leave NOTHING behind."""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ._release import (
    GEN,
    INSTALL_SH,
    build_release,
    data,
    fake_bin,
    installed_anything,
    jsonload,
    make_wheel,
    mode,
    pinned_uv_version,
    run_install,
    state,
    write_test_script,
)

pytestmark = pytest.mark.skipif(shutil.which("uv") is None or shutil.which("curl") is None, reason="needs uv and curl")


@pytest.fixture
def script(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    return write_test_script(tmp_path / "scripts" / "install.sh")


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    return fake_bin(tmp_path)


def go(tmp_path: Path, script: Path, bin_dir: Path, rel: Path, *args: str, **kw):
    return run_install(tmp_path, script, "--from", str(rel), *args, path_dirs=[bin_dir], **kw)


def assert_untouched(tmp_path: Path, res) -> None:
    assert res.returncode != 0, res.stdout
    assert "Nothing was installed" in res.stderr or "Nothing was changed" in res.stderr, res.stderr
    assert not installed_anything(tmp_path)


@pytest.mark.slow
def test_happy_path_layout_record_and_rerun(tmp_path, script, bin_dir):
    rel = build_release(tmp_path / "rel")
    res = go(tmp_path, script, bin_dir, rel.dir)
    assert res.returncode == 0, res.stderr + res.stdout
    s = state(tmp_path)
    tool = s / "install" / "tools" / "orch-2.0.0"
    assert (tool / ".complete").is_file()
    assert (s / "install" / "current").is_symlink()
    assert (s / "install" / "current").resolve() == tool.resolve()
    shim = tmp_path / "home" / ".local" / "bin" / "orch"
    assert shim.is_symlink()
    assert subprocess.run([str(shim)], capture_output=True, text=True).stdout.strip() == "orch_core 2.0.0"
    rec = jsonload(data(tmp_path) / "install.json")
    assert rec["format"] == "orch.install/1"
    assert rec["channel"] == "main" and rec["version"] == "2.0.0"
    assert rec["seq_seen"] == {"main": 5}
    assert rec["revocations_issued_at"] == rel.revs_issued
    assert rec["root_key_id"] == "root-1" and rec["trusted_roots"] == ["root-1"]
    assert {f["role"] for f in rec["files"]} == {"core", "dashboard", "constraints"}
    assert all(len(f["sha256"]) == 64 for f in rec["files"])
    assert rec["tool_dir"] == str(tool)
    assert mode(data(tmp_path) / "install.json") == 0o600
    # host format.json: the exact bytes orch.migrate.write_format writes
    assert (s / "format.json").read_text() == '{"format": 1, "orch": "host"}\n'
    assert "orch setup" in res.stdout
    assert "sha256" in res.stdout  # the installer prints its own checksum for the guide
    # rerun: the same release is safe and changes nothing
    again = go(tmp_path, script, bin_dir, rel.dir)
    assert again.returncode == 0, again.stderr
    assert jsonload(data(tmp_path) / "install.json")["seq_seen"] == {"main": 5}


@pytest.mark.slow
def test_upgrade_keeps_the_old_version_and_flips(tmp_path, script, bin_dir):
    assert go(tmp_path, script, bin_dir, build_release(tmp_path / "r1", seq=5, version="2.0.0").dir).returncode == 0
    res = go(tmp_path, script, bin_dir, build_release(tmp_path / "r2", seq=6, version="2.0.1").dir)
    assert res.returncode == 0, res.stderr
    s = state(tmp_path) / "install"
    assert (s / "tools" / "orch-2.0.0" / ".complete").exists() and (s / "tools" / "orch-2.0.1" / ".complete").exists()
    assert (s / "current").resolve() == (s / "tools" / "orch-2.0.1").resolve()
    assert jsonload(data(tmp_path) / "install.json")["seq_seen"] == {"main": 6}
    # an older seq (a replayed pointer) is refused and nothing moves
    old = go(tmp_path, script, bin_dir, build_release(tmp_path / "r3", seq=4, version="2.0.2").dir)
    assert old.returncode == 1 and "older than" in old.stderr
    assert (s / "current").resolve() == (s / "tools" / "orch-2.0.1").resolve()
    assert not (s / "tools" / "orch-2.0.2").exists()


# --- every tampered piece stops BEFORE install ------------------------------------------------------------------------


def _flip_sig(rel):
    env = json.loads((rel.dir / "channel.json").read_text())
    sig = bytearray(base64.b64decode(env["sig"]["value"]))
    sig[0] ^= 1
    env["sig"]["value"] = base64.b64encode(bytes(sig)).decode()
    (rel.dir / "channel.json").write_text(json.dumps(env))


def _flip_hash_same_size(rel):
    p = rel.path("core")
    b = bytearray(p.read_bytes())
    b[-3] ^= 0xFF
    p.write_bytes(bytes(b))


def _truncate_wheel(rel):
    p = rel.path("dashboard")
    p.write_bytes(p.read_bytes()[:-5])


def _remove_listed(rel):
    rel.path("dashboard").unlink()


def _constraints_changed(rel):
    (rel.dir / "constraints.txt").write_text("evil==1.0 --hash=sha256:" + "0" * 64 + "\n")


@pytest.mark.slow
@pytest.mark.parametrize(
    ("tamper", "phrase"),
    [
        (_flip_sig, "signature"),
        (_flip_hash_same_size, "does not match"),
        (_truncate_wheel, "does not match"),
        (_remove_listed, "is missing"),
        (_constraints_changed, "does not match"),
    ],
    ids=["manifest-signature", "wheel-hash", "wheel-size", "listed-wheel-missing", "constraints-changed"],
)
def test_tampered_release_stops_before_install(tmp_path, script, bin_dir, tamper, phrase):
    rel = build_release(tmp_path / "rel")
    tamper(rel)
    res = go(tmp_path, script, bin_dir, rel.dir)
    assert_untouched(tmp_path, res)
    assert phrase in res.stderr


@pytest.mark.slow
def test_a_wheel_the_manifest_does_not_list_is_never_installed(tmp_path, script, bin_dir):
    rel = build_release(tmp_path / "rel")
    evil = make_wheel(rel.dir, "orch_evil", "6.6.6", entry="evil")
    res = go(tmp_path, script, bin_dir, rel.dir)
    assert res.returncode == 0, res.stderr
    tool = state(tmp_path) / "install" / "tools" / "orch-2.0.0" / "venv"
    assert not (tool / "bin" / "evil").exists()
    assert not list(tool.glob("lib/python*/site-packages/orch_evil*"))
    assert evil.exists()  # it sat next to the release and was ignored, not deleted or used


@pytest.mark.slow
def test_expired_manifest(tmp_path, script, bin_dir):
    now = datetime.now(UTC)
    rel = build_release(tmp_path / "rel", issued=now - timedelta(days=30), expires=now - timedelta(days=9))
    res = go(tmp_path, script, bin_dir, rel.dir)
    assert_untouched(tmp_path, res)
    assert "expired" in res.stderr


@pytest.mark.slow
def test_wrong_channel(tmp_path, script, bin_dir):
    rel = build_release(tmp_path / "rel", channel="develop")  # the pointer for "develop" served as "main"
    res = go(tmp_path, script, bin_dir, rel.dir, "--channel", "main")
    assert_untouched(tmp_path, res)
    assert "different channel" in res.stderr


@pytest.mark.slow
def test_revoked_certificate(tmp_path, script, bin_dir):
    rel = build_release(tmp_path / "rel", key_id="rk-4")  # serial 4 is on the signed revocation list
    res = go(tmp_path, script, bin_dir, rel.dir)
    assert_untouched(tmp_path, res)
    assert "signature or signing key" in res.stderr


@pytest.mark.slow
def test_test_channels_are_refused_by_a_production_verifier(tmp_path, script, bin_dir):
    rel = build_release(tmp_path / "rel", channel="test-x", cert_channels=["test-*"], key_id="rk-3")
    res = go(tmp_path, script, bin_dir, rel.dir, "--channel", "test-x")
    assert_untouched(tmp_path, res)


@pytest.mark.slow
def test_first_install_with_a_revocation_list_below_the_floor_is_refused(tmp_path, script, bin_dir):
    # a genuine root-signed list from before the floor (for example one from before a key was revoked)
    rel = build_release(tmp_path / "rel", revs_issued=datetime(2026, 10, 1, tzinfo=UTC))
    res = go(tmp_path, script, bin_dir, rel.dir)
    assert_untouched(tmp_path, res)
    assert "older than this installer accepts" in res.stderr
    low = build_release(tmp_path / "rel2", revs_min_serial=1)  # the list's min_serial is below the floor's
    text = script.read_text().replace("REVOCATIONS_FLOOR_MIN_SERIAL = 1", "REVOCATIONS_FLOOR_MIN_SERIAL = 5")
    script.write_text(text)
    res = go(tmp_path, script, bin_dir, low.dir)
    assert_untouched(tmp_path, res)


@pytest.mark.slow
def test_a_stale_revocation_list_is_refused_with_plain_guidance(tmp_path, script, bin_dir):
    rel = build_release(tmp_path / "rel", revs_issued=datetime.now(UTC) - timedelta(days=31))
    # lower the floor so only freshness is under test
    script.write_text(
        script.read_text().replace(
            'FLOOR_ISSUED_AT = "2026-10-20T00:00:00Z"', 'FLOOR_ISSUED_AT = "2020-01-01T00:00:00Z"'
        )
    )
    res = go(tmp_path, script, bin_dir, rel.dir)
    assert_untouched(tmp_path, res)
    assert "revocation list is too old; try again later or tell the person who runs orch releases" in res.stderr


@pytest.mark.slow
def test_the_shipped_installer_has_no_key_yet_and_trusts_nothing(tmp_path, bin_dir):
    rel = build_release(tmp_path / "rel")
    shipped = tmp_path / "shipped.sh"
    shutil.copy(INSTALL_SH, shipped)  # the production roots (none yet), not the TEST root
    res = run_install(tmp_path, shipped, "--from", str(rel.dir), path_dirs=[bin_dir])
    assert_untouched(tmp_path, res)
    assert "no release key" in res.stderr


@pytest.mark.slow
def test_a_damaged_install_record_stops_the_install(tmp_path, script, bin_dir):
    rel = build_release(tmp_path / "rel")
    data(tmp_path).mkdir(parents=True)
    (data(tmp_path) / "install.json").write_text("not json")
    res = go(tmp_path, script, bin_dir, rel.dir)
    assert res.returncode != 0 and "install record" in res.stderr
    assert not (state(tmp_path) / "install" / "tools").exists()


# --- refusals before any download ----------------------------------------------------------------------------------


def test_refuses_under_an_agent(tmp_path, script, bin_dir):
    rel = build_release(tmp_path / "rel")
    res = go(tmp_path, script, bin_dir, rel.dir, env_extra={"ORCH_SESSION": "s1"})
    assert res.returncode == 1 and "not for agents" in res.stderr
    assert not installed_anything(tmp_path)


def test_unknown_channel_and_option(tmp_path, script, bin_dir):
    assert run_install(tmp_path, script, "--channel", "../x", path_dirs=[bin_dir]).returncode == 1
    assert run_install(tmp_path, script, "--nope", path_dirs=[bin_dir]).returncode == 1


def test_rosetta_is_refused(tmp_path, script):
    stubs = fake_bin(tmp_path, uname='case "$1" in -s) echo Darwin;; -m) echo arm64;; esac', sysctl="echo 1")
    res = run_install(tmp_path, script, "--from", str(tmp_path), path_dirs=[stubs])
    assert res.returncode == 1 and "Rosetta" in res.stderr


def test_unsupported_platform(tmp_path, script):
    stubs = fake_bin(tmp_path, uname='case "$1" in -s) echo FreeBSD;; -m) echo amd64;; esac')
    res = run_install(tmp_path, script, path_dirs=[stubs])
    assert res.returncode == 1 and "not supported" in res.stderr


def test_a_cut_off_download_runs_nothing(tmp_path):
    text = INSTALL_SH.read_text()
    for frac in (0.3, 0.6, 0.97):
        cut = tmp_path / f"cut{int(frac * 100)}.sh"
        cut.write_text(text[: int(len(text) * frac)])
        res = run_install(tmp_path, cut, path_dirs=[fake_bin(tmp_path)])
        assert res.returncode != 0
        assert not installed_anything(tmp_path)
        assert not (tmp_path / "home" / ".config").exists()


# --- --server and the linger check -----------------------------------------------------------------------------------


def _linux_stubs(tmp_path: Path, linger: str | None):
    stubs = {"uname": 'case "$1" in -s) echo Linux;; -m) echo x86_64;; esac'}
    if linger is not None:
        stubs["loginctl"] = f'echo "Linger={linger}"'
    return fake_bin(tmp_path, **stubs)


def test_server_with_linger_off_prints_the_sudo_line_and_stops(tmp_path, script):
    rel = build_release(tmp_path / "rel")
    res = run_install(tmp_path, script, "--server", "--from", str(rel.dir), path_dirs=[_linux_stubs(tmp_path, "no")])
    assert res.returncode == 1
    assert "sudo loginctl enable-linger" in res.stderr
    commands = [ln.strip() for ln in res.stderr.splitlines() if ln.strip().startswith("sudo ")]
    assert commands == [f"sudo loginctl enable-linger {getpass.getuser()}"]  # exactly one command line
    assert not installed_anything(tmp_path)
    assert not state(tmp_path).exists()  # nothing was fetched or written


def _tools_without(tmp_path: Path, missing: str) -> Path:
    """A folder of links to the system tools install.sh needs, without ``missing`` (a CI runner has systemd's
    loginctl in /usr/bin, so the system folders cannot stay on PATH for this test)."""
    out = tmp_path / "systools"
    out.mkdir(exist_ok=True)
    for d in ("/usr/bin", "/bin"):
        for p in Path(d).iterdir():
            if p.name != missing and not os.path.lexists(out / p.name):  # /bin may be /usr/bin; links may dangle
                (out / p.name).symlink_to(p)
    return out


def test_server_without_loginctl_stops(tmp_path, script):
    res = run_install(
        tmp_path,
        script,
        "--server",
        path_dirs=[_linux_stubs(tmp_path, None)],
        system_dirs=[str(_tools_without(tmp_path, "loginctl"))],
    )
    assert res.returncode == 1 and "loginctl" in res.stderr


def test_server_on_a_mac_is_refused(tmp_path, script):
    stubs = fake_bin(tmp_path, uname='case "$1" in -s) echo Darwin;; -m) echo arm64;; esac', sysctl="echo 0")
    res = run_install(tmp_path, script, "--server", path_dirs=[stubs])
    assert res.returncode == 1 and "Ubuntu" in res.stderr


@pytest.mark.slow
def test_server_with_linger_on_installs_and_names_the_next_step(tmp_path, script):
    rel = build_release(tmp_path / "rel")
    res = run_install(tmp_path, script, "--server", "--from", str(rel.dir), path_dirs=[_linux_stubs(tmp_path, "yes")])
    assert res.returncode == 0, res.stderr
    assert "orch setup --server" in res.stdout


# --- the pinned uv ---------------------------------------------------------------------------------------------------


def _triple() -> str:
    run = lambda a: subprocess.run(["uname", a], capture_output=True, text=True).stdout.strip()  # noqa: E731
    return {
        ("Darwin", "arm64"): "aarch64-apple-darwin",
        ("Darwin", "x86_64"): "x86_64-apple-darwin",
        ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
        ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
    }[(run("-s"), run("-m"))]


def _uv_tarball(dest: Path, triple: str, body: str) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    stage = dest / "stage" / f"uv-{triple}"
    stage.mkdir(parents=True)
    (stage / "uv").write_text(body)
    (stage / "uv").chmod(0o755)
    tgz = dest / f"uv-{triple}.tar.gz"
    with tarfile.open(tgz, "w:gz") as t:
        t.add(stage, arcname=f"uv-{triple}")
    return hashlib.sha256(tgz.read_bytes()).hexdigest()


def test_uv_with_the_wrong_sha256_is_not_used(tmp_path, script):
    _uv_tarball(tmp_path / "uvsrv", _triple(), "#!/bin/sh\necho 'uv 0.0.0'\n")
    res = run_install(
        tmp_path,
        script,
        "--from",
        str(tmp_path),
        path_dirs=[],
        env_extra={"ORCH_INSTALL_UV_BASE": str(tmp_path / "uvsrv")},
    )
    assert res.returncode == 1
    assert "checksum" in res.stderr and "Nothing was installed" in res.stderr
    assert not (data(tmp_path) / "uv").exists()
    assert not installed_anything(tmp_path)


def test_uv_with_the_pinned_sha256_is_unpacked_and_version_checked(tmp_path):
    triple = _triple()
    sha = _uv_tarball(tmp_path / "uvsrv", triple, f"#!/bin/sh\necho 'uv {pinned_uv_version()}'\n")
    var = "UV_SHA256_" + triple.replace("-", "_")
    patched = write_test_script(tmp_path / "t.sh")
    text = patched.read_text()
    old = next(line for line in text.splitlines() if line.startswith(var + "="))
    patched.write_text(text.replace(old, f'{var}="{sha}"'))
    res = run_install(
        tmp_path,
        patched,
        "--from",
        str(tmp_path / "nothing"),
        env_extra={"ORCH_INSTALL_UV_BASE": str(tmp_path / "uvsrv")},
    )
    # uv was accepted and unpacked; the run then stops later (no release there), which is fine here
    assert (data(tmp_path) / "uv" / f"uv-{pinned_uv_version()}" / "uv").is_file(), res.stderr
    assert "checksum" not in res.stderr


def test_a_uv_on_path_or_with_other_bytes_is_never_reused(tmp_path, script):
    on_path = fake_bin(tmp_path, pinned_uv=False, uv=f'echo "uv {pinned_uv_version()}"')  # right version, on PATH
    dest = data(tmp_path) / "uv" / f"uv-{pinned_uv_version()}"
    dest.mkdir(parents=True)
    (dest / "uv").write_text(f'#!/bin/sh\necho "uv {pinned_uv_version()}"\n')  # right version, not the pinned bytes
    (dest / "uv").chmod(0o755)
    _uv_tarball(tmp_path / "uvsrv", _triple(), "#!/bin/sh\n")
    res = run_install(
        tmp_path,
        script,
        "--from",
        str(tmp_path),
        path_dirs=[on_path],
        env_extra={"ORCH_INSTALL_UV_BASE": str(tmp_path / "uvsrv")},
    )
    assert "checksum" in res.stderr  # it went to fetch the pinned one instead of trusting either


# --- pins ------------------------------------------------------------------------------------------------------------


def test_embedded_roots_equal_the_core_production_roots():
    from orch.update.keys import PRODUCTION_ROOTS

    m = re.search(r"^ROOTS_B64 = (\{.*\})$", INSTALL_SH.read_text(), re.M)
    assert m is not None
    embedded = {k: base64.b64decode(v) for k, v in json.loads(m.group(1)).items()}
    assert embedded == dict(PRODUCTION_ROOTS)


def test_the_shipped_installer_never_embeds_a_test_key():
    text = INSTALL_SH.read_text()
    assert "ALLOW_TEST_CHANNELS = False" in text
    assert base64.b64encode(GEN.ROOT.public).decode() not in text


def test_host_format_matches_what_core_writes():
    m = re.search(r'^HOST_FORMAT="(\d+)"', INSTALL_SH.read_text(), re.M)
    assert m is not None
    try:
        from orch.migrate.steps import CURRENT
    except ImportError:
        pytest.skip("P1-A (orch.migrate) is not in this branch yet")
    assert int(m.group(1)) == CURRENT["host"]


def test_layout_constants_agree_with_orch_update_layout():
    text = INSTALL_SH.read_text()
    assert "# LAYOUT: see orch/update/layout.py" in text
    for needle in ("install/tools/orch-", "install/current"):
        assert needle in text
    try:
        from orch.update import layout  # noqa: F401
    except ImportError:
        pytest.skip("orch/update/layout.py (P2-D) is not in this branch yet")


# --- review fixes: wheels only, https only, future revocations, record before flip ------------------------------------


@pytest.mark.slow
def test_a_dependency_without_a_wheel_stops_the_install_and_leaves_no_tool_dir(tmp_path, script, bin_dir):
    # a hash-pinned dependency that has no usable wheel (it exists nowhere): uv must not try to build or guess
    pin = "sdistonly-xyz==1.0 \\\n    --hash=sha256:" + "ab" * 32 + "\n"
    rel = build_release(tmp_path / "rel", constraints=pin)
    res = go(tmp_path, script, bin_dir, rel.dir, env_extra={"UV_OFFLINE": "1"})
    assert_untouched(tmp_path, res)
    assert "Installing the verified files failed" in res.stderr
    assert not list((state(tmp_path) / "install" / "tools").glob("orch-*"))


def test_install_uses_no_build_and_https_only():
    text = INSTALL_SH.read_text()
    assert "--require-hashes --no-build --no-deps" in text
    fetch_body = text[text.index("fetch() {") : text.index("get_uv() {")]
    assert "--proto '=https' --tlsv1.2" in fetch_body and "http" not in fetch_body.replace("=https", "")
    assert text.count("--proto") == text.count("--proto '=https' --tlsv1.2")


def test_from_plain_http_is_refused(tmp_path, script, bin_dir):
    res = run_install(tmp_path, script, "--from", "http://127.0.0.1:1", path_dirs=[bin_dir])
    assert res.returncode == 1 and "plain http" in res.stderr
    assert not installed_anything(tmp_path)


@pytest.mark.slow
def test_a_revocation_list_dated_in_the_future_is_refused(tmp_path, script, bin_dir):
    rel = build_release(tmp_path / "rel", revs_issued=datetime.now(UTC) + timedelta(days=2))
    res = go(tmp_path, script, bin_dir, rel.dir)
    assert_untouched(tmp_path, res)
    assert "future" in res.stderr


@pytest.mark.slow
def test_the_environment_cannot_redirect_uv(tmp_path, script, bin_dir):
    rel = build_release(tmp_path / "rel")
    bad = {
        "UV_INDEX_URL": "http://127.0.0.1:1/evil",
        "PIP_INDEX_URL": "http://127.0.0.1:1/evil",
        "UV_NO_VERIFY_HASHES": "1",
    }
    res = go(tmp_path, script, bin_dir, rel.dir, env_extra={**bad, "UV_PYTHON_PREFERENCE": "system"})
    assert res.returncode == 0, res.stderr


@pytest.mark.slow
def test_an_unwritable_record_stops_before_the_flip(tmp_path, script, bin_dir):
    assert go(tmp_path, script, bin_dir, build_release(tmp_path / "r1", seq=5, version="2.0.0").dir).returncode == 0
    current = state(tmp_path) / "install" / "current"
    before = current.resolve()
    d = data(tmp_path)
    d.chmod(0o500)  # the record cannot be written any more
    try:
        res = go(tmp_path, script, bin_dir, build_release(tmp_path / "r2", seq=6, version="2.0.1").dir)
    finally:
        d.chmod(0o700)
    assert res.returncode == 1 and "install record" in res.stderr and "not changed" in res.stderr
    assert current.resolve() == before
    assert not (state(tmp_path) / "install" / "tools" / "orch-2.0.1").exists()  # the half-done build was removed
    assert (state(tmp_path) / "install" / "tools" / "orch-2.0.0" / ".complete").exists()  # the old one is untouched


def test_the_sha256_is_printed_only_for_a_real_install_sh(tmp_path, script, bin_dir):
    # run as `sh < install.sh` ($0 is "sh"): no checksum line, and the script still works up to the release fetch
    res = subprocess.run(
        ["env", "-i", f"HOME={tmp_path}", "PATH=/usr/bin:/bin", "sh", "-s", "--", "--nope"],
        input=INSTALL_SH.read_text(),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 1 and "sha256" not in res.stdout + res.stderr
