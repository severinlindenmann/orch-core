"""Custody failures must never repair or disclose an existing credential."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from orch.github import token as t
from orch.host.errors import HostError

SECRET = "github_pat_synthetic_test_only"


@pytest.fixture
def state(tmp_path):
    (tmp_path / "hosts" / "ws").mkdir(parents=True, mode=0o700)
    return tmp_path


def test_roundtrip_rotation_and_remove(state):
    t.store(state, "ws", os.getuid(), t.Token(SECRET))
    directory = t.github_dir(state, "ws")
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / t.TOKEN_FILE).stat().st_mode & 0o777 == 0o600
    assert t.load(state, "ws", os.getuid()) == t.Token(SECRET)
    for name in t.CACHE_FILES:
        (directory / name).write_text("old data")
    (directory / "unrelated").write_text("keep")
    t.store(state, "ws", os.getuid(), t.Token(SECRET + "2"))
    assert all(not (directory / name).exists() for name in t.CACHE_FILES)
    for name in t.CACHE_FILES:
        (directory / name).write_text("old data")
    assert t.remove(state, "ws")
    assert sorted(p.name for p in directory.iterdir()) == ["unrelated"]
    assert not t.remove(state, "ws")


@pytest.mark.parametrize("problem", ["link", "mode", "dir_mode", "oversize", "json", "prefix", "null", "owner"])
def test_unsafe(state, problem):
    t.store(state, "ws", os.getuid(), t.Token(SECRET))
    directory = t.github_dir(state, "ws")
    path = directory / t.TOKEN_FILE
    if problem == "link":
        path.unlink()
        path.symlink_to(state / "missing")
    elif problem == "mode":
        path.chmod(0o644)
    elif problem == "dir_mode":
        directory.chmod(0o755)
    elif problem == "oversize":
        path.write_text(SECRET * 200)
    elif problem == "json":
        path.write_text(SECRET)
    elif problem == "prefix":
        path.write_text('{"v":1,"token":"ghp_synthetic_test_only"}')
    elif problem == "null":
        path.write_text("null")
    with pytest.raises(HostError) as caught:
        t.load(state, "ws", os.getuid() + (problem == "owner"))
    assert caught.value.code == "custody.unsafe_mode"
    assert SECRET not in str(caught.value)
    assert "ghp_synthetic_test_only" not in str(caught.value)
    if problem == "dir_mode":
        assert directory.stat().st_mode & 0o777 == 0o755


def test_missing(state):
    with pytest.raises(t.NoToken):
        t.load(state, "ws", os.getuid())
    t.github_dir(state, "ws").mkdir(mode=0o700)
    with pytest.raises(t.NoToken):
        t.load(state, "ws", os.getuid())
    with pytest.raises(HostError) as caught:
        t.store(state, "missing", os.getuid(), t.Token(SECRET))
    assert caught.value.code == "not_found"


def test_single_reader():
    source = Path(__file__).resolve().parents[2] / "src" / "orch"
    pattern = re.compile(r'\bgithub_dir\b|\bTOKEN_FILE\b|/\s*["\']github["\']\s*/')
    found = {p.relative_to(source).as_posix() for p in source.rglob("*.py") if pattern.search(p.read_text())}
    assert found <= {"github/token.py", "host/custody.py"}


def test_foreign_file_owner(state, monkeypatch):
    t.store(state, "ws", os.getuid(), t.Token(SECRET))
    original = os.fstat

    def foreign(fd):
        info = original(fd)
        if __import__("stat").S_ISREG(info.st_mode):
            values = list(info)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "fstat", foreign)
    with pytest.raises(HostError) as caught:
        t.load(state, "ws", os.getuid())
    assert caught.value.code == "custody.unsafe_mode"
