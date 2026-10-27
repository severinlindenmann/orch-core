"""B2 (run 11): ~/.config/orch/person ended up 0744 on a clean Mac (launchd made it for the timer's log) and custody
refused. Every orch-owned private dir is 0700 whatever the umask, and custody tightens a loose dir of its own."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from orch.custody.convenience import secure_dir
from orch.privdir import make_private_dir
from orch.records import timer as rtimer

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_make_private_dir_is_0700_under_umask_022(tmp_path):
    old = os.umask(0o022)
    try:
        d = make_private_dir(tmp_path / "a" / "b" / "person")
    finally:
        os.umask(old)
    assert [_mode(tmp_path / "a"), _mode(tmp_path / "a" / "b"), _mode(d)] == [0o700] * 3


def test_timer_install_leaves_person_private_under_umask_022(tmp_path):
    state = tmp_path / "state"
    unit = rtimer.plan({"HOME": str(tmp_path)}, state, platform="darwin", python=Path("/usr/bin/python3"), dev=True)
    old = os.umask(0o022)
    try:
        rtimer.TimerManager(unit).install()
    finally:
        os.umask(old)
    assert _mode(state / "person") == 0o700


def test_custody_tightens_a_loose_person_dir_it_owns(tmp_path):
    root = tmp_path / "person"
    root.mkdir()
    root.chmod(0o744)  # what launchd made
    fd = secure_dir(root, ("ws",), create=True)
    os.close(fd)
    assert _mode(root) == 0o700


def test_custody_refuses_and_names_the_fix_when_others_can_write(tmp_path):
    from orch.custody.base import CustodyError

    root = tmp_path / "person"
    root.mkdir()
    root.chmod(0o770)
    with pytest.raises(CustodyError, match=f"chmod 700 {root}"):
        secure_dir(root, ("ws",), create=True)
    assert _mode(root) == 0o770  # untouched

    root.chmod(0o700)
    (root / "ws").mkdir()
    (root / "ws").chmod(0o757)
    with pytest.raises(CustodyError, match=f"chmod 700 {root / 'ws'}"):
        secure_dir(root, ("ws",), create=True)


def test_timer_install_leaves_a_group_writable_person_dir_alone_and_custody_refuses(tmp_path):
    from orch.custody.base import CustodyError

    state = tmp_path / "state"
    (state / "person").mkdir(parents=True)
    (state / "person").chmod(0o770)
    unit = rtimer.plan({"HOME": str(tmp_path)}, state, platform="darwin", python=Path("/usr/bin/python3"), dev=True)
    rtimer.TimerManager(unit).install()
    assert _mode(state / "person") == 0o770
    with pytest.raises(CustodyError, match="chmod 700"):
        secure_dir(state / "person", (), create=True)
