"""RealOsFacts against the real kernel: Linux only (runs in GitHub CI on ubuntu), nothing is created system-wide."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orch.setup.osfacts import RealOsFacts

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux kernel facts")

NOBODY = 65534


def _real(monkeypatch, tmp_path: Path) -> RealOsFacts:
    from orch.setup.osfacts import UserInfo

    real = RealOsFacts()
    # the account 65534 owns nothing here and has only its own gid; pytest's base dir above tmp_path is private to us
    monkeypatch.setattr(
        real, "user_by_uid", lambda uid: UserInfo("n", uid, NOBODY, "/", "/bin/sh", frozenset(), frozenset({NOBODY}))
    )
    root = str(tmp_path.resolve())
    monkeypatch.setattr(real, "_ancestors", lambda r: [a for a in reversed(Path(r).parents) if str(a).startswith(root)])
    tmp_path.resolve().chmod(0o755)
    return real


def test_x_only_directory_with_a_world_readable_key_is_readable(monkeypatch, tmp_path) -> None:
    real = _real(monkeypatch, tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    key = state / "k"
    key.write_text("secret")
    key.chmod(0o644)
    state.chmod(0o711)
    assert real.can_read(NOBODY, str(key)) is True
    state.chmod(0o700)
    assert real.can_read(NOBODY, str(key)) is False


@pytest.mark.skipif(shutil.which("setfacl") is None, reason="setfacl missing")
def test_acl_entry_grants_and_removal_revokes(monkeypatch, tmp_path) -> None:
    real = _real(monkeypatch, tmp_path)
    key = tmp_path / "k"
    key.write_text("secret")
    key.chmod(0o600)
    assert real.can_read(NOBODY, str(key)) is False
    done = subprocess.run(["setfacl", "-m", f"u:{NOBODY}:r", str(key)], capture_output=True)
    if done.returncode != 0:
        pytest.skip("filesystem without ACL support")
    assert real.stat(str(key)).has_acl is True
    assert real.can_read(NOBODY, str(key)) is True
    subprocess.run(["setfacl", "-b", str(key)], check=True)
    assert real.can_read(NOBODY, str(key)) is False
    assert real.stat(str(key)).has_acl is False


def test_proc_status_of_this_process_parses() -> None:
    status = RealOsFacts._proc_status(os.getpid())
    assert {int(x) for x in status["Uid"].split()} == {os.getuid()}
    for k in ("Gid", "NoNewPrivs", "CapEff", "CapAmb", "CapPrm", "CapInh", "CapBnd"):
        assert k in status
    int(status["CapEff"].strip(), 16)


def test_user_gids_come_from_getgrouplist_and_cover_the_process_groups() -> None:
    import pwd

    me = pwd.getpwuid(os.getuid())
    info = RealOsFacts().user(me.pw_name)
    assert info is not None and info.uid == os.getuid()
    assert me.pw_gid in info.gids
    assert (
        set(os.getgroups()) <= info.gids or os.getuid() == 0
    )  # login groups at start; getgrouplist reads the database
