"""orch-52: the ownership boundary of the implicit walk (:mod:`orch.discovery`). Only the person's own candidates
count, the walk stops at a sticky or other-writable directory, and an explicit ``ORCH_WORKSPACE`` is trusted as named.

Another user cannot be made without root, so "owned by another uid" is simulated by replacing
``orch.discovery._owner`` (the uid discovery compares ``st_uid`` against) with a uid that is not ours."""

from __future__ import annotations

import io
import os
import shutil
from pathlib import Path

import pytest

from orch import discovery
from orch.cli.main import main
from orch.ops import runtime
from orch.ops.caps import Files
from tests.ops.helpers import Cli

FILES = Files.direct()
V1_FIXTURE = Path(__file__).parents[1] / "importer" / "fixtures" / "v1"


def _v2(dest: Path, ws) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "config.json").write_text((ws.root / "config.json").read_text())
    return dest


def _v1(dest: Path) -> Path:
    shutil.copytree(V1_FIXTURE, dest)
    return dest / "orchestrator"


def _sticky(path: Path) -> Path:
    path.mkdir(parents=True)
    os.chmod(path, 0o1777)
    return path


def _check(cwd: Path, env: dict[str, str], monkeypatch) -> tuple[int, str]:
    monkeypatch.chdir(cwd)
    out, err = io.StringIO(), io.StringIO()
    return main(["check"], env=env, stdout=out, stderr=err), err.getvalue()


@pytest.mark.parametrize("layout", ["v2-here", "v2-sub", "v1"])
def test_a_workspace_planted_in_a_sticky_shared_directory_is_ignored(ws, tmp_path, monkeypatch, layout):
    shared = _sticky(tmp_path / "shared")
    if layout == "v2-here":
        _v2(shared, ws)
    elif layout == "v2-sub":
        _v2(shared / "orchestrator", ws)
    else:
        shutil.copytree(V1_FIXTURE / "orchestrator", shared / "orchestrator")
    mine = shared / "mine" / "deep"
    mine.mkdir(parents=True)
    # the plant is owned by us and private: only the shared directory makes it no candidate
    assert discovery._private(os.stat(shared / "orchestrator" if layout != "v2-here" else shared / "config.json"))
    assert runtime.locate_workspace({}, str(mine), FILES) is None
    assert runtime.locate_workspace({}, str(shared), FILES) is None  # the start itself is a stop
    code, err = _check(mine, {"HOME": str(tmp_path / "home")}, monkeypatch)
    assert code == 2 and "no orch workspace here" in err and "v1" not in err
    # a person's own workspace below the shared directory is found
    own = _v2(shared / "proj" / "orchestrator", ws)
    (own.parent / "sub").mkdir()
    assert runtime.find_workspace({}, str(own.parent / "sub"), FILES) == own


def test_a_workspace_above_a_shared_directory_is_never_reached(ws, tmp_path):
    _v2(tmp_path / "top" / "orchestrator", ws)
    _v1(tmp_path / "top" / "old")
    shared = _sticky(tmp_path / "top" / "shared")
    (shared / "x").mkdir()
    assert runtime.locate_workspace({}, str(shared / "x"), FILES) is None
    # other-writable without the sticky bit stops the walk too
    os.chmod(shared, 0o777)
    assert runtime.locate_workspace({}, str(shared / "x"), FILES) is None
    os.chmod(shared, 0o755)
    assert runtime.find_workspace({}, str(shared / "x"), FILES) == tmp_path / "top" / "orchestrator"


def test_a_workspace_owned_by_another_uid_is_ignored(ws, tmp_path, monkeypatch):
    v2 = _v2(tmp_path / "a" / "orchestrator", ws)
    v1 = _v1(tmp_path / "b")
    here = _v2(tmp_path / "c", ws)
    for found, cwd, is_v1 in ((v2, v2.parent, False), (v1, v1.parent, True), (here, here, False)):
        assert runtime.locate_workspace({}, str(cwd), FILES) == (found, is_v1)
    monkeypatch.setattr(discovery, "_owner", lambda: os.getuid() + 1)
    for cwd in (v2.parent, v2, v1.parent, v1 / "tickets", here):
        assert runtime.locate_workspace({}, str(cwd), FILES) is None, cwd
    code, err = _check(v1.parent, {"HOME": str(tmp_path / "home")}, monkeypatch)
    assert code == 2 and "no orch workspace here" in err
    # another uid's workspace is walked past, not a stop: our own further up is found
    monkeypatch.undo()
    mine = _v2(tmp_path / "mine", ws)
    theirs = _v2(mine / "theirs" / "orchestrator", ws)
    real_fstat = os.fstat

    def fstat(fd):  # only the planted folder and its config.json are another uid's
        st = real_fstat(fd)
        if (st.st_dev, st.st_ino) in planted:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1, *tuple(st)[5:]))
        return st

    planted = {(s.st_dev, s.st_ino) for s in (os.stat(theirs), os.stat(theirs / "config.json"))}
    with monkeypatch.context() as m:
        m.setattr(discovery.os, "fstat", fstat)
        assert runtime.find_workspace({}, str(theirs), FILES) == mine


@pytest.mark.parametrize("target", ["dir", "config"])
@pytest.mark.parametrize("bits", [0o020, 0o002])
def test_a_group_or_other_writable_workspace_is_ignored(ws, tmp_path, target, bits):
    v2 = _v2(tmp_path / "a" / "orchestrator", ws)
    v1 = _v1(tmp_path / "b")
    here = _v2(tmp_path / "c", ws)
    for found, cwd in ((v2, v2.parent), (v1, v1.parent), (here, here)):
        path = found if target == "dir" else found / "config.json"
        os.chmod(path, os.stat(path).st_mode & 0o777 | bits)
        assert runtime.locate_workspace({}, str(cwd), FILES) is None, (cwd, oct(bits))


def test_an_explicit_orch_workspace_skips_the_ownership_rule(ws, tmp_path, monkeypatch):
    os.chmod(ws.root / "config.json", 0o666)
    assert runtime.find_workspace({}, str(ws.root), FILES) is None
    assert runtime.find_workspace({"ORCH_WORKSPACE": str(ws.root)}, None, FILES) == ws.root
    monkeypatch.setattr(discovery, "_owner", lambda: os.getuid() + 1)
    assert runtime.find_workspace({"ORCH_WORKSPACE": str(ws.root)}, None, FILES) == ws.root
    shared = _sticky(tmp_path / "shared")
    v1 = _v1(shared / "old")
    assert runtime.find_v1_workspace({"ORCH_WORKSPACE": str(v1.parent)}, None, FILES) == v1
    run = Cli(ws)("new", "A")  # Cli names the workspace with ORCH_WORKSPACE: the command runs
    assert run.code == 0, run.err
    # still O_NOFOLLOW inside an explicit path
    lnk = tmp_path / "lnk"
    lnk.mkdir()
    (lnk / "config.json").symlink_to(ws.root / "config.json")
    assert runtime.find_workspace({"ORCH_WORKSPACE": str(lnk)}, None, FILES) is None
