"""The reader profiles under their audit hook (section 3), and the reader working under the host's own hook (H3a).
Audit hooks cannot be removed, so every case runs in a subprocess."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from tests.gitread.conftest import make_repo  # noqa: F401  (fixture)

PRELUDE = textwrap.dedent(
    """
    import json, os, subprocess, sys
    from orch.gitread import fs, ops, sandbox
    from orch.gitread.errors import Unavailable
    args = json.loads(sys.argv[1])
    out = {}
    def t(name, fn):
        try:
            fn()
            out[name] = "ok"
        except PermissionError:
            out[name] = "denied"
        except Unavailable as e:
            out[name] = "unavailable:" + e.reason
        except Exception as e:
            out[name] = "error:" + type(e).__name__
    def read(path):
        with open(path, "rb") as f:
            f.read(1)
    def walk_read(dirpath, name):
        d, _ = fs.open_walk(dirpath)
        try:
            d.read(name, 1 << 20)
        finally:
            d.close()
    """
)

PROFILE = PRELUDE + textwrap.dedent(
    """
    sandbox.install(args["allowed"])
    wt, gd, outside = args["worktree"], args["gitdir"], args["outside"]
    t("gitdir_config", lambda: read(gd + "/config"))
    t("gitdir_config_walk", lambda: walk_read(gd, "config"))
    t("worktree_file", lambda: read(wt + "/a.txt"))
    t("worktree_file_walk", lambda: walk_read(wt, "a.txt"))
    t("worktree_dotdot", lambda: read(gd + "/../a.txt"))
    t("outside_file", lambda: read(outside + "/secret.txt"))
    t("outside_walk", lambda: walk_read(outside, "secret.txt"))
    t("etc_hosts", lambda: read("/etc/hosts"))
    wt_fd = os.open(wt, os.O_RDONLY | os.O_DIRECTORY)  # an fd passes the hook; the relative open below must not
    t("relative_plain", lambda: os.close(os.open("a.txt", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=wt_fd)))
    t("write_in_gitdir", lambda: open(gd + "/x", "w").close())
    t("spawn", lambda: subprocess.run(["true"]))
    t("system", lambda: os.system("true"))
    t("rename", lambda: os.rename(gd + "/config", gd + "/config2"))
    t("listdir_outside", lambda: os.listdir(outside))
    print(json.dumps(out))
    """
)


def _run(script: str, args: dict) -> dict:
    done = subprocess.run(
        [sys.executable, "-c", script, json.dumps(args)], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture
def layout(make_repo, tmp_path):  # noqa: F811
    r = make_repo()
    r.write("a.txt", "work\n")
    r.commit()
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    return {"worktree": str(r.path), "gitdir": str(r.gitdir), "outside": str(outside)}


def test_gitdir_profile_cannot_open_a_worktree_file(layout):
    res = _run(PROFILE, {**layout, "allowed": [layout["gitdir"], layout["worktree"] + "/.git"]})
    assert res["gitdir_config"] == "ok" and res["gitdir_config_walk"] == "ok"
    for name in ("worktree_file", "worktree_dotdot", "outside_file", "etc_hosts", "relative_plain"):
        assert res[name] == "denied", (name, res)
    # the fd walk turns the hook's PermissionError into a refusal: "cannot be opened"
    assert res["worktree_file_walk"].endswith("cannot be opened"), res
    assert res["outside_walk"].endswith("cannot be opened"), res
    for name in ("write_in_gitdir", "spawn", "system", "rename", "listdir_outside"):
        assert res[name] == "denied", (name, res)


def test_worktree_profile_opens_only_its_three_pins(layout):
    res = _run(PROFILE, {**layout, "allowed": [layout["gitdir"], layout["gitdir"], layout["worktree"]]})
    assert res["gitdir_config"] == "ok" and res["worktree_file"] == "ok" and res["worktree_file_walk"] == "ok"
    for name in ("outside_file", "etc_hosts", "relative_plain", "worktree_dotdot"):
        assert res[name] == "denied", (name, res)
    assert res["outside_walk"].endswith("cannot be opened"), res
    for name in ("write_in_gitdir", "spawn", "system", "rename", "listdir_outside"):
        assert res[name] == "denied", (name, res)


def test_the_profiles_are_built_from_the_pins(layout):
    from orch.gitread import ops
    from orch.gitread.pinning import pin_repo

    pins = pin_repo(layout["worktree"], [layout["worktree"]]).to_json()
    # #486: the gitdir profile also admits exactly the working copy's ``.git`` (the working copy only as an ancestor)
    dotgit = layout["worktree"] + "/.git"
    assert ops.allowed_paths("gitdir", {"pins": pins}) == [layout["gitdir"], layout["gitdir"], dotgit]
    assert ops.allowed_paths("worktree", {"pins": pins}) == [layout["gitdir"], layout["gitdir"], layout["worktree"]]


HOST = textwrap.dedent(
    """
    import json, os, sys
    args = json.loads(sys.argv[1])
    from orch.host import audit
    audit.warm()
    from pathlib import Path
    from orch.gitread.client import HostGitReader, PinStore, ReaderPool
    from orch.ops.caps import Capabilities
    reader = HostGitReader(ReaderPool(), PinStore(Path(args["state"]) / "git-pins.json"))
    caps = Capabilities.host((args["root"], args["state"]), git=reader).with_principal("agent-1")
    audit.install([args["root"], args["state"]])
    out = {}
    try:
        open(args["worktree"] + "/a.txt").close()
        out["host_open_repo"] = "ok"
    except PermissionError:
        out["host_open_repo"] = "denied"
    out["sync"] = reader.sync([args["worktree"]])
    out["head"] = caps.git.head(Path(args["worktree"]))
    d = caps.git.diff(Path(args["worktree"]), name="r", branch="feat", default_branch=None, remote=None)
    out["files"] = [f["path"] for f in d["files"]]
    out["run"] = "absent" if not hasattr(caps.git, "run") else "present"  # no free-argv git at all (#546)
    out["dirty"] = caps.git.dirty(Path(args["worktree"]))
    print(json.dumps(out))
    """
)


@pytest.mark.slow
def test_the_host_reads_git_only_through_reader_children(make_repo, tmp_path):  # noqa: F811
    r = make_repo()
    r.write("a.txt", "1\n")
    r.commit()
    r.git("checkout", "-q", "-b", "feat")
    r.write("a.txt", "2\n")
    head = r.commit()
    base = tmp_path.resolve()
    (base / "ws").mkdir()
    (base / "state").mkdir()
    res = _run(HOST, {"root": str(base / "ws"), "state": str(base / "state"), "worktree": str(r.path)})
    assert res["host_open_repo"] == "denied"  # the host itself never opens a repo file
    assert res["sync"] == {str(r.path): None} and res["head"] == head and res["files"] == ["a.txt"]
    assert res["run"] == "absent" and res["dirty"] is None
