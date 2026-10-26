"""V3 G4 (design-g4 C1, C4): ``orch.addons.envtree``: the path rules and stamp vectors, the walk that never follows a
link, and ``verify`` (the check the runner makes before every call)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from orch.addons import envtree
from orch.market import MarketError
from tests.addons import market_ref as ref

CASES = [c for c in json.loads(Path(__file__).with_name("vectors_market.json").read_text())["cases"]]
PATHS = [c for c in CASES if c["check"] == "env_paths"]
STAMPS = [c for c in CASES if c["check"] == "env_stamp"]


@pytest.mark.parametrize("case", PATHS, ids=[c["name"] for c in PATHS])
def test_env_paths_vector(case):
    try:
        envtree.check_paths(case["args"]["paths"])
        got = {"ok": {}}
    except envtree.TreeError:
        got = {"refuse": "market.env_failed"}
    assert got == case["expect"]


@pytest.mark.parametrize("case", STAMPS, ids=[c["name"] for c in STAMPS])
def test_env_stamp_vector_uses_the_same_tree_hash(case):
    a = case["args"]
    entries = a["actual"]
    tree = envtree.Tree(tuple((p, sha, mode) for p, sha, mode in entries), (), 0)
    mine = envtree.tree_hashes(tree)[0]
    assert mine == ref.tree_hash(entries)  # C4: one definition of the tree hash
    stamp = a["stamp"]
    ok = stamp.get("digest") == a["package_sha256"] and stamp.get("tree") == mine
    assert ({"ok": {}} if ok else {"refuse": "market.env_changed"}) == case["expect"]


# ---- the walk and verify on a real directory

WS, NAME, DIGEST, KIND = "ws_1", "dep", "sha256:" + "ab" * 32, "python-requirements"


def build(state: Path, files: dict[str, bytes], *, kind=KIND, digest=DIGEST) -> Path:
    t = envtree.target_dir(state, WS, NAME, digest)
    t.mkdir(parents=True, mode=0o700)
    for rel, data in files.items():
        p = t / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.chmod(0o644)
    for d in [t, *[x for x in t.rglob("*") if x.is_dir()]]:
        d.chmod(0o755 if d != t else 0o700)
    tree = envtree.walk(t)
    body = envtree.stamp_body(WS, NAME, digest, kind, tree)
    sp = envtree.stamp_path(t)
    sp.write_text(json.dumps(body))
    sp.chmod(0o600)
    return t


@pytest.fixture
def state(tmp_path):
    s = tmp_path / "state"
    s.mkdir(mode=0o700)
    return s


def verify(state, digest=DIGEST, kind=KIND):
    try:
        return envtree.verify(str(state), WS, NAME, digest, kind)
    except MarketError as e:
        assert e.code == "market.env_changed"
        return e.reason


def test_a_built_tree_verifies_and_every_change_is_caught(state):
    t = build(state, {"idna/__init__.py": b"x=1\n", "idna/core.py": b"y=2\n", "six.py": b"z=3\n"})
    assert verify(state) == t
    (t / "idna/core.py").write_bytes(b"y=3\n")
    assert verify(state) == "changed"
    (t / "idna/core.py").write_bytes(b"y=2\n")
    assert verify(state) == t
    (t / "idna/core.py").chmod(0o755)  # a mode change
    assert verify(state) == "changed"
    (t / "idna/core.py").chmod(0o644)
    (t / "extra").mkdir()  # an added empty directory
    assert verify(state) == "changed"
    (t / "extra").rmdir()
    (t / "idna/new.py").write_bytes(b"")
    assert verify(state) == "changed"
    (t / "idna/new.py").unlink()
    (t / "six.py").unlink()
    assert verify(state) == "changed"


def test_planted_auto_run_files_links_and_special_files_are_caught(state, tmp_path):
    t = build(state, {"a/m.py": b"1\n"})
    for name in ("x.pth", "sitecustomize.py", "UserCustomize.PY"):
        (t / name).write_bytes(b"import os\n")
        assert verify(state) == "changed", name
        (t / name).unlink()
    (t / "SiteCustomize").mkdir()
    assert verify(state) == "changed"
    (t / "SiteCustomize").rmdir()
    os.symlink("/etc/hosts", t / "a/link")
    assert verify(state) == "changed"
    (t / "a/link").unlink()
    os.link(t / "a/m.py", tmp_path / "hard")
    assert verify(state) == "changed"  # a second name for a file of the tree
    (tmp_path / "hard").unlink()
    os.mkfifo(t / "a/pipe")
    assert verify(state) == "changed"
    (t / "a/pipe").unlink()
    assert verify(state) == t


def test_a_symlinked_environment_directory_or_parent_is_never_followed(state, tmp_path):
    t = build(state, {"a.py": b"1\n"})
    moved = tmp_path / "elsewhere"
    t.rename(moved)
    os.symlink(moved, t)  # the digest directory is now a link to a perfectly good copy
    assert verify(state) == "missing"
    t.unlink()
    moved.rename(t)
    d = t.parent
    d.rename(tmp_path / "namedir")
    os.symlink(tmp_path / "namedir", d)
    assert verify(state) == "missing"


def test_a_missing_stamp_a_wrong_digest_kind_or_abi_and_a_forged_stamp(state):
    t = build(state, {"a.py": b"1\n"})
    sp = envtree.stamp_path(t)
    body = json.loads(sp.read_text())
    assert verify(state, "sha256:" + "cd" * 32) == "missing"  # another digest has no directory
    assert verify(state, kind="node-package-lock") == "changed"  # the stamp is for the other kind
    for edit, reason in (
        ({"abi": "other"}, "abi"),
        ({"digest": "sha256:" + "cd" * 32}, "digest"),
        ({"ws": "ws_2"}, "changed"),
        ({"name": "other"}, "changed"),
        ({"files": 99}, "changed"),
        ({"tree": "sha256:" + "00" * 32}, "changed"),
        ({"dirs": "sha256:" + "00" * 32}, "changed"),
    ):
        sp.write_text(json.dumps({**body, **edit}))
        assert verify(state) == reason, edit
    for bad in ('{"v": 1}', "not json", json.dumps({**body, "extra": 1}), json.dumps({**body, "bytes": "1"})):
        sp.write_text(bad)
        assert verify(state) == "missing", bad
    sp.unlink()
    assert verify(state) == "missing"
    sp.write_text(json.dumps(body))
    os.chmod(sp, 0o600)
    assert verify(state) == t
    sp.unlink()
    os.symlink("/etc/hosts", sp)
    assert verify(state) == "missing"


def test_walk_limits_and_names(state, monkeypatch):
    t = build(state, {"a.py": b"1\n"})
    monkeypatch.setattr(envtree, "MAX_ENTRIES", 2)
    (t / "b.py").write_bytes(b"")
    (t / "c.py").write_bytes(b"")
    with pytest.raises(envtree.TreeError) as e:
        envtree.walk(t)
    assert e.value.reason == "too_large"
    monkeypatch.setattr(envtree, "MAX_ENTRIES", 100)
    monkeypatch.setattr(envtree, "MAX_BYTES", 3)
    (t / "b.py").write_bytes(b"12345")
    with pytest.raises(envtree.TreeError) as e:
        envtree.walk(t)
    assert e.value.reason == "too_large"


def test_rmtree_at_removes_links_without_following_them(state, tmp_path):
    outside = tmp_path / "keep"
    outside.mkdir()
    (outside / "f").write_text("x")
    t = build(state, {"a/b.py": b"1\n"})
    os.symlink(outside, t / "a/out")
    fd = os.open(t.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        (t / "a").chmod(0o500)  # unwritable directories are made writable to be removed
        envtree.rmtree_at(fd, t.name)
    finally:
        os.close(fd)
    assert not t.exists() and (outside / "f").read_text() == "x"
    assert stat.S_ISDIR(os.stat(outside).st_mode)


def test_budget_is_the_one_mechanism_and_a_walk_draws_from_it(state):
    b = envtree.Budget(bytes=10, entries=3)
    b.take(4, 1)
    assert (b.bytes, b.entries, b.room(100), b.room(2)) == (6, 2, 6, 2)
    with pytest.raises(envtree.TreeError) as e:
        b.take(7)
    assert e.value.reason == "too_large"
    t = build(state, {"a.py": b"12345", "b.py": b"12345"})
    shared = envtree.Budget(bytes=12, entries=100)
    envtree.walk(t, shared)  # 10 bytes: fits
    with pytest.raises(envtree.TreeError):
        envtree.walk(t, shared)  # the same budget again: 2 bytes left
    with pytest.raises(envtree.TreeError):
        envtree.walk(t, envtree.Budget(bytes=100, entries=1))  # entries are counted too


def test_a_lockfile_is_drawn_from_a_budget_too():
    from orch.addons import lockfile
    from orch.market import MarketError

    text = ("x==1.0 --hash=sha256:" + "a" * 64 + "\n").encode()
    lockfile.check("python-requirements", text, envtree.Budget(bytes=len(text), entries=1))
    with pytest.raises(MarketError) as e:
        lockfile.check("python-requirements", text, envtree.Budget(bytes=len(text) - 1, entries=1))
    assert e.value.code == "market.lock_invalid"
