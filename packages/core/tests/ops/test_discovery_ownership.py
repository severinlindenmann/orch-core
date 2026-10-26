"""orch-52: the ownership boundary of the implicit walk (:mod:`orch.discovery`). Only the person's own candidates
count, the walk stops at a sticky or other-writable directory, and an explicit ``ORCH_WORKSPACE`` is trusted as named.

Another user cannot be made without root, so "owned by another uid" is simulated by replacing
``orch.discovery._owner`` (the uid discovery compares ``st_uid`` against) with a uid that is not ours."""

from __future__ import annotations

import io
import json
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
    """A v2 store at ``dest`` (a folder named ``.orch``); any other name is a project: its ``.orch`` is returned."""
    if dest.name != ".orch":
        dest = dest / ".orch"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "config.json").write_text((ws.store_dir / "config.json").read_text())
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
    if layout == "v2-here":  # an old-version root store planted in the shared folder: not a candidate either
        shared.joinpath("config.json").write_text((ws.store_dir / "config.json").read_text())
    elif layout == "v2-sub":
        _v2(shared / ".orch", ws)
    else:
        shutil.copytree(V1_FIXTURE / "orchestrator", shared / "orchestrator")
    mine = shared / "mine" / "deep"
    mine.mkdir(parents=True)
    # the plant is owned by us and private: only the shared directory makes it no candidate
    plant = {"v2-sub": ".orch", "v1": "orchestrator", "v2-here": "config.json"}[layout]
    assert discovery._private(os.stat(shared / plant))
    assert runtime.locate_workspace({}, str(mine), FILES) is None
    assert runtime.locate_workspace({}, str(shared), FILES) is None  # the start itself is a stop
    code, err = _check(mine, {"HOME": str(tmp_path / "home")}, monkeypatch)
    assert code == 2 and "no orch workspace here" in err and "v1" not in err
    # a person's own workspace below the shared directory is found
    own = _v2(shared / "proj" / ".orch", ws)
    (own.parent / "sub").mkdir()
    assert runtime.find_workspace({}, str(own.parent / "sub"), FILES) == own.parent


def test_a_workspace_above_a_shared_directory_is_never_reached(ws, tmp_path):
    _v2(tmp_path / "top" / ".orch", ws)
    _v1(tmp_path / "top" / "old")
    shared = _sticky(tmp_path / "top" / "shared")
    (shared / "x").mkdir()
    assert runtime.locate_workspace({}, str(shared / "x"), FILES) is None
    # other-writable without the sticky bit stops the walk too
    os.chmod(shared, 0o777)
    assert runtime.locate_workspace({}, str(shared / "x"), FILES) is None
    os.chmod(shared, 0o755)
    assert runtime.find_workspace({}, str(shared / "x"), FILES) == tmp_path / "top"


def test_a_workspace_owned_by_another_uid_is_ignored(ws, tmp_path, monkeypatch):
    v2 = _v2(tmp_path / "a" / ".orch", ws)
    v1 = _v1(tmp_path / "b")
    here = _v2(tmp_path / "c", ws)
    for found, cwd, is_v1 in ((v2.parent, v2.parent, False), (v1, v1.parent, True), (here.parent, here.parent, False)):
        assert runtime.locate_workspace({}, str(cwd), FILES) == (found, is_v1)
    monkeypatch.setattr(discovery, "_owner", lambda: os.getuid() + 1)
    for cwd in (v2.parent, v2, v1.parent, v1 / "tickets", here):
        assert runtime.locate_workspace({}, str(cwd), FILES) is None, cwd
    code, err = _check(v1.parent, {"HOME": str(tmp_path / "home")}, monkeypatch)
    assert code == 2 and "no orch workspace here" in err
    # another uid's workspace is walked past, not a stop: our own further up is found
    monkeypatch.undo()
    mine = _v2(tmp_path / "mine", ws).parent
    theirs = _v2(mine / "theirs" / ".orch", ws)
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
    v2 = _v2(tmp_path / "a" / ".orch", ws)
    v1 = _v1(tmp_path / "b")
    here = _v2(tmp_path / "c", ws)
    for found, cwd in ((v2, v2.parent), (v1, v1.parent), (here, here)):
        path = found if target == "dir" else found / "config.json"
        os.chmod(path, os.stat(path).st_mode & 0o777 | bits)
        assert runtime.locate_workspace({}, str(cwd), FILES) is None, (cwd, oct(bits))


def test_an_explicit_orch_workspace_skips_the_ownership_rule(ws, tmp_path, monkeypatch):
    os.chmod(ws.store_dir / "config.json", 0o666)
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
    (lnk / "config.json").symlink_to(ws.store_dir / "config.json")
    assert runtime.find_workspace({"ORCH_WORKSPACE": str(lnk)}, None, FILES) is None


def test_config_json_swapped_between_the_check_and_the_read_is_not_read(ws, tmp_path, monkeypatch):
    """The ownership check and the read are on the same fd: a ``config.json`` replaced right after its ``fstat`` (by
    one another uid would own and could rewrite) is not what is read; no path lookup sits in between."""
    import stat as _stat

    proj = _v2(tmp_path / "proj" / ".orch", ws)
    original = json.loads((proj / "config.json").read_text())
    evil = dict(original, workspace=dict(original["workspace"], id="ws_evil"))
    real_fstat = os.fstat
    swaps = []

    def fstat(fd):
        st = real_fstat(fd)
        if _stat.S_ISREG(st.st_mode) and not swaps:  # config.json was just checked: replace it on disk
            tmp = proj / "evil.json"
            tmp.write_text(json.dumps(evil))
            os.chmod(tmp, 0o666)
            os.replace(tmp, proj / "config.json")
            swaps.append(fd)
        return st

    with monkeypatch.context() as m:
        m.setattr(discovery.os, "fstat", fstat)
        found = discovery.locate(proj.parent, explicit=False)
    assert swaps and json.loads((proj / "config.json").read_text())["workspace"]["id"] == "ws_evil"
    assert (
        found is not None
        and found.path == proj.parent
        and found.config["workspace"]["id"] == original["workspace"]["id"]
    )


def test_an_ancestor_swapped_for_a_symlink_after_the_resolve_is_never_entered(ws, tmp_path, monkeypatch):
    """The walk goes up through ``..`` from the open start: an ancestor renamed away and replaced by a symlink to a
    workspace after the start was resolved and opened is not followed (by path it would be found)."""
    start = tmp_path / "a" / "b" / "c"
    start.mkdir(parents=True)
    evil = _v2(tmp_path / "evil", ws)
    real_open = os.open
    swapped = []

    def hooked(path, flags, mode=0o777, *, dir_fd=None):
        if path == ".." and dir_fd is not None and not swapped:
            (tmp_path / "a" / "b").rename(tmp_path / "a" / "was")
            (tmp_path / "a" / "b").symlink_to(evil, target_is_directory=True)
            swapped.append(True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as m:
        m.setattr(discovery.os, "open", hooked)
        found = discovery.locate(start, explicit=False)
    assert swapped and (tmp_path / "a" / "b").is_symlink()
    assert found is None
    # the same layout without the race: by the symlink the start resolves into evil's tree and that is found
    assert runtime.find_workspace({}, str(tmp_path / "a" / "b"), FILES) == evil.parent


@pytest.mark.parametrize("mode", [0o1111, 0o111])
def test_an_ancestor_that_cannot_be_opened_stops_the_walk(ws, tmp_path, mode):
    """Fail closed: when a directory on the way up cannot be opened (sticky but unreadable ``0o1111``, or just
    unreadable), its boundary cannot be established, so the walk stops instead of climbing above it."""
    _v2(tmp_path / "top" / ".orch", ws)
    shut = tmp_path / "top" / "shut"
    (shut / "x").mkdir(parents=True)
    os.chmod(shut, mode)
    try:
        assert runtime.locate_workspace({}, str(shut / "x"), FILES) is None
    finally:
        os.chmod(shut, 0o755)
    assert runtime.find_workspace({}, str(shut / "x"), FILES) == tmp_path / "top"


def _no_path_open_of_config(monkeypatch):
    """Any open of ``config.json`` by path (``os.open`` without ``dir_fd``, ``open``, ``io.open``) fails the test."""
    import builtins
    import io as _io

    real_os_open, real_open, real_io_open = os.open, builtins.open, _io.open

    def bad(path) -> bool:
        return isinstance(path, (str, os.PathLike)) and os.fspath(path).endswith("config.json")

    def os_open(path, flags, mode=0o777, *, dir_fd=None):
        assert dir_fd is not None or not bad(path), f"config.json opened by path: {path}"
        return real_os_open(path, flags, mode, dir_fd=dir_fd)

    def py_open(file, *a, **kw):
        assert not bad(file), f"config.json opened by path: {file}"
        return real_open(file, *a, **kw)

    def io_open(file, *a, **kw):
        assert not bad(file), f"config.json opened by path: {file}"
        return real_io_open(file, *a, **kw)

    monkeypatch.setattr(os, "open", os_open)
    monkeypatch.setattr(builtins, "open", py_open)
    monkeypatch.setattr(_io, "open", io_open)


@pytest.mark.parametrize("explicit", [False, True])
def test_after_discovery_opening_the_store_reads_no_config_json_by_path(ws, monkeypatch, explicit):
    """The config discovery read from the checked fd is what ``Workspace._open`` (and ``Store.open``'s workspace id
    check) uses: no path open of ``config.json`` after discovery."""
    env = ws.env()
    if not explicit:
        env.pop("ORCH_WORKSPACE")
    w = runtime.Workspace(env, cwd=str(ws.root), files=FILES)
    assert w.root == ws.root and w.config["workspace"]["id"] == ws.store.workspace_id
    with monkeypatch.context() as m:
        _no_path_open_of_config(m)
        store = w.store
        assert store.workspace_id == ws.store.workspace_id
        assert w.config["workspace"]["id"] == store.workspace_id
    store.close()


@pytest.mark.parametrize("how", ["replaced", "symlink"])
def test_a_root_swapped_after_discovery_is_refused(ws, tmp_path, how):
    """Before the store opens below the root, the root must still be the directory discovery found (dev/ino): a
    directory renamed away and replaced (by a copy, or by a symlink to it) refuses, and nothing is opened."""
    env = ws.env()
    env.pop("ORCH_WORKSPACE")
    w = runtime.Workspace(env, cwd=str(ws.root), files=FILES)
    assert w.root == ws.root and w.ident == (os.stat(ws.root).st_dev, os.stat(ws.root).st_ino)
    old = ws.root.with_name(ws.root.name + "-was")
    ws.root.rename(old)
    if how == "replaced":
        shutil.copytree(old, ws.root, symlinks=True)
    else:
        ws.root.symlink_to(old, target_is_directory=True)
    with pytest.raises(runtime.OrchError) as e:
        w.store  # noqa: B018
    assert e.value.code == "not_found" and "no longer the workspace directory" in e.value.message
    with pytest.raises(runtime.OrchError):
        w.verify_root()


def _swap_config_at_the_identity_check(ws, monkeypatch, m):
    """At the root identity check (right before the store opens), replace ``config.json`` in place (same root
    directory, so the dev/ino check passes) by one naming another workspace, then forbid any path open of it."""
    real = discovery.same_root
    swapped = []

    def same_root(root, ident):
        ok = real(root, ident)
        if not swapped:
            doc = json.loads((ws.store_dir / "config.json").read_text())
            doc["workspace"]["id"] = "f" * 32
            tmp = ws.root / "swap.json"
            tmp.write_text(json.dumps(doc))
            os.replace(tmp, ws.store_dir / "config.json")
            swapped.append(True)
            _no_path_open_of_config(m)
        return ok

    m.setattr(discovery, "same_root", same_root)
    return swapped


def test_orch_worktree_opens_the_store_with_the_checked_config_bytes(ws, monkeypatch):
    """``orch worktree``: ``config.json`` replaced after discovery (the root stays the same directory) is never read
    by path; the store opens with the bytes discovery checked."""
    from orch import worktree_cli

    env = ws.env(grant=False, session=None)
    env.pop("ORCH_WORKSPACE")
    monkeypatch.setenv("ORCH_NO_SERVICE_MANAGER", "1")
    monkeypatch.chdir(ws.root)
    out = io.StringIO()
    with monkeypatch.context() as m:
        swapped = _swap_config_at_the_identity_check(ws, monkeypatch, m)
        code = worktree_cli.main(["list", "--json"], env=env, out=out)
    assert swapped and code == 0, out.getvalue()
    assert json.loads(out.getvalue())["ok"] is True


def test_orch_records_direct_mode_opens_the_store_with_the_checked_config_bytes(ws, monkeypatch):
    """``orch records`` in direct mode on the workspace found here: the same, through ``host_link.resolve``."""
    from orch.records import host_link

    env = ws.env(grant=False, session=None)
    env.pop("ORCH_WORKSPACE")
    monkeypatch.chdir(ws.root)
    res = host_link.resolve(env, None)
    try:
        assert res.mode == "direct"
        with monkeypatch.context() as m:
            swapped = _swap_config_at_the_identity_check(ws, monkeypatch, m)
            store = res.link._open()
        assert swapped and store.workspace_id == res.workspace_id != "f" * 32
    finally:
        res.close()
