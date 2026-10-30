"""uv runs the way install.sh runs it: same flags, same environment policy (orch.update.uvenv)."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from orch.ops.errors import OrchError
from orch.update import uvenv
from orch.update.layout import InstallLayout, Record
from orch.update.services import uv_install

INSTALL_SH = Path(__file__).resolve().parents[4] / "install.sh"


def test_the_environment_starts_empty_and_drops_uv_and_pip_variables():
    src = {
        "HOME": "/h", "PATH": "/bin", "UV_PYTHON_DOWNLOADS_JSON_URL": "x", "UV_NO_VERIFY_HASHES": "1",
        "UV_INDEX_URL": "x", "PIP_INDEX_URL": "x", "PYTHONPATH": "/evil", "UV_PYTHON_PREFERENCE": "system",
    }  # fmt: skip
    env = uvenv.uv_env(src)
    assert env == {"HOME": "/h", "PATH": "/bin", "UV_PYTHON_PREFERENCE": "only-managed"}


def test_flags():
    assert "--no-build" in uvenv.install_argv("uv", Path("p"), Path("r"))
    for flag in ("--require-hashes", "--no-deps", "--no-config"):
        assert flag in uvenv.install_argv("uv", Path("p"), Path("r"))
    assert "--no-config" in uvenv.venv_argv("uv", "py", Path("v"))


@pytest.mark.skipif(not INSTALL_SH.exists(), reason="install.sh lives on feat/pilot-p2c-install until it merges")
def test_parity_with_install_sh():
    text = INSTALL_SH.read_text()
    sh_install = re.search(r'"\$UV" pip install ([^\n]*?) --python', text)
    sh_venv = re.search(r'"\$UV" venv ([^\n]*?) --python', text)
    assert sh_install and sh_venv
    ours = uvenv.install_argv("uv", Path("p"), Path("r"))
    assert sh_install[1].split() == ours[3 : 3 + len(sh_install[1].split())]
    assert sh_venv[1].split() == uvenv.venv_argv("uv", "py", Path("v"))[2 : 2 + len(sh_venv[1].split())]
    assert "UV_PYTHON_PREFERENCE=only-managed" in text.replace("export ", "")
    lines = text.splitlines()
    i = next(n for n, line in enumerate(lines) if line.strip().startswith("unset UV_"))
    chunk = [lines[i].strip().removeprefix("unset ")]
    while chunk[-1].endswith("\\"):
        i += 1
        chunk.append(lines[i].strip())
    unset = " ".join(chunk).replace("\\", " ").split()
    assert unset and all(n.startswith(("UV_", "PIP_")) for n in unset)
    assert not any(k.startswith(("UV_", "PIP_")) for k in uvenv.KEEP_ENV)


def SIGNED(tmp_path):
    import hashlib

    return [
        {"role": r, "name": n, "size": 5, "sha256": hashlib.sha256((tmp_path / n).read_bytes()).hexdigest()}
        for r, n in (("core", "c.whl"), ("constraints", "constraints.txt"))
    ]


def test_uv_install_runs_a_stub_uv_with_the_pinned_argv_and_env(tmp_path, monkeypatch):
    log = tmp_path / "calls.txt"
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {log}\n'
        f"env | sort >> {log}\n"
        'if [ "$1" = python ]; then echo /usr/bin/python3; exit 0; fi\n'
        'if [ "$1" = venv ]; then mkdir -p "${@: -1}/bin"; printf "#!/bin/sh\\nexit 0\\n" > "${@: -1}/bin/orch";'
        ' chmod +x "${@: -1}/bin/orch"; fi\n'
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS_JSON_URL", "http://evil")
    monkeypatch.setenv("UV_NO_VERIFY_HASHES", "1")
    monkeypatch.setenv("PIP_INDEX_URL", "http://evil")
    lay = InstallLayout.of({"HOME": str(tmp_path), "ORCH_STATE_DIR": str(tmp_path / "s")})
    lay.ensure()
    monkeypatch.setattr("orch.update.services.check_uv", lambda *a, **k: str(uv))
    for n in ("c.whl", "constraints.txt"):
        (tmp_path / n).write_text("x==1\n")
    rec = Record("main", "2.0.0", {}, None, 1, "r", "k", [], "t", "/x", {"path": str(uv), "version": "0.13.3"}, [])
    uv_install(
        lay, "2.0.0", {"core": tmp_path / "c.whl", "constraints": tmp_path / "constraints.txt"}, rec, SIGNED(tmp_path)
    )
    out = log.read_text()
    assert "--no-build" in out and "--require-hashes" in out
    assert "UV_PYTHON_DOWNLOADS_JSON_URL" not in out and "UV_NO_VERIFY_HASHES" not in out and "PIP_INDEX_URL" not in out
    assert "UV_PYTHON_PREFERENCE=only-managed" in out
    assert (lay.tool_dir("2.0.0") / ".complete").is_file() and os.access(uv, os.X_OK)


def test_check_uv_accepts_only_the_pinned_binary_at_the_pinned_path(tmp_path, monkeypatch):
    data = tmp_path / "orch"
    good = data / "uv" / f"uv-{uvenv.UV_VERSION}" / "uv"
    good.parent.mkdir(parents=True)
    good.write_text("#!/bin/sh\necho uv 0.13.3\n")
    good.chmod(0o755)
    other = tmp_path / "evil-uv"
    other.write_text(good.read_text())
    other.chmod(0o755)
    monkeypatch.setitem(
        uvenv.UV_BIN_SHA256, uvenv.triple(), __import__("hashlib").sha256(good.read_bytes()).hexdigest()
    )
    assert uvenv.check_uv(data, str(good)) == str(good)
    for recorded in (str(other), "/usr/bin/true", ""):  # a record pointing at a stub / another path
        with pytest.raises(OrchError) as e:
            uvenv.check_uv(data, recorded)
        assert e.value.code == "update.no_record"
    good.write_text("#!/bin/sh\necho uv 0.13.3 changed\n")  # same path, other bytes
    with pytest.raises(OrchError):
        uvenv.check_uv(data, str(good))


def test_check_uv_wrong_version_and_link(tmp_path, monkeypatch):
    data = tmp_path / "orch"
    good = data / "uv" / f"uv-{uvenv.UV_VERSION}" / "uv"
    good.parent.mkdir(parents=True)
    good.write_text("x")
    good.chmod(0o755)
    monkeypatch.setitem(uvenv.UV_BIN_SHA256, uvenv.triple(), __import__("hashlib").sha256(b"x").hexdigest())
    with pytest.raises(OrchError):
        uvenv.check_uv(data, str(good), version_of=lambda p: "uv 0.99.0")
    real = tmp_path / "real"
    real.write_text("x")
    real.chmod(0o755)
    good.unlink()
    good.symlink_to(real)
    with pytest.raises(OrchError):
        uvenv.check_uv(data, str(good), version_of=lambda p: "uv 0.13.3")


@pytest.mark.skipif(not INSTALL_SH.exists(), reason="install.sh lives on feat/pilot-p2c-install until it merges")
def test_pinned_uv_constants_equal_install_sh():
    text = INSTALL_SH.read_text()
    assert re.search(rf'^UV_VERSION="{re.escape(uvenv.UV_VERSION)}"$', text, re.M)
    for t, sha in uvenv.UV_BIN_SHA256.items():
        assert re.search(rf'^UV_BIN_SHA256_{t.replace("-", "_")}="{sha}"$', text, re.M), t


def test_a_wheel_swapped_after_the_download_is_refused(tmp_path, monkeypatch):
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)
    monkeypatch.setattr("orch.update.services.check_uv", lambda *a, **k: str(uv))
    lay = InstallLayout.of({"HOME": str(tmp_path), "ORCH_STATE_DIR": str(tmp_path / "s")})
    lay.ensure()
    for n in ("c.whl", "constraints.txt"):
        (tmp_path / n).write_text("x==1\n")
    signed = SIGNED(tmp_path)
    (tmp_path / "c.whl").write_text("EVIL\n")  # swapped on disk after verification
    rec = Record("main", "2.0.0", {}, None, 1, "r", "k", [], "t", "/x", {"path": str(uv), "version": "0.13.3"}, [])
    with pytest.raises(OrchError) as e:
        uv_install(lay, "2.0.0", {"core": tmp_path / "c.whl", "constraints": tmp_path / "constraints.txt"}, rec, signed)
    assert e.value.code == "update.unverified" and not lay.tool_dir("2.0.0").exists()
