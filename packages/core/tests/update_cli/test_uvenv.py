"""uv runs the way install.sh runs it: same flags, same environment policy (orch.update.uvenv)."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

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
    for n in ("c.whl", "constraints.txt"):
        (tmp_path / n).write_text("x==1\n")
    rec = Record("main", "2.0.0", {}, None, 1, "r", "k", [], "t", "/x", {"path": str(uv), "version": "0.13.3"}, [])
    uv_install(lay, "2.0.0", {"core": tmp_path / "c.whl", "constraints": tmp_path / "constraints.txt"}, rec)
    out = log.read_text()
    assert "--no-build" in out and "--require-hashes" in out
    assert "UV_PYTHON_DOWNLOADS_JSON_URL" not in out and "UV_NO_VERIFY_HASHES" not in out and "PIP_INDEX_URL" not in out
    assert "UV_PYTHON_PREFERENCE=only-managed" in out
    assert (lay.tool_dir("2.0.0") / ".complete").is_file() and os.access(uv, os.X_OK)
