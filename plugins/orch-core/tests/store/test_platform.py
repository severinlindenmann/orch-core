"""File primitives per platform: binary appends, no symlink follow without O_NOFOLLOW, F_FULLFSYNC, replace retries."""

from __future__ import annotations

import os
import sys

import pytest

from orch.store import fsio


def test_appends_are_binary_and_exact(tmp_path):
    p = tmp_path / "log"
    fsio.append_durable(p, b'{"a":1}\n', commit=True)
    fsio.append_durable(p, b'{"b":2}\n')
    assert p.read_bytes() == b'{"a":1}\n{"b":2}\n' and b"\r" not in p.read_bytes()
    assert fsio._BIN == getattr(os, "O_BINARY", 0)


def test_without_o_nofollow_a_link_is_still_not_followed(tmp_path, monkeypatch):
    monkeypatch.setattr(fsio, "_NOFOLLOW", 0)  # what Windows gives us
    real = tmp_path / "real"
    real.write_bytes(b"secret")
    link = tmp_path / "link"
    os.symlink(real, link)
    assert fsio.read_or_none(link) is None  # fstat and lstat disagree: counts as absent
    assert fsio.read_or_none(real) == b"secret"
    with pytest.raises(OSError):
        fsio.append_durable(link, b"x\n")
    assert real.read_bytes() == b"secret"


def test_replace_retries_a_busy_target(tmp_path, monkeypatch):
    calls = []
    real = os.replace

    def flaky(a, b):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("in use")
        real(a, b)

    monkeypatch.setattr(fsio, "_RETRY", True)
    monkeypatch.setattr(fsio.os, "replace", flaky)
    (tmp_path / "a").write_bytes(b"1")
    fsio.replace(tmp_path / "a", tmp_path / "b")
    assert len(calls) == 3 and (tmp_path / "b").read_bytes() == b"1"
    calls.clear()
    monkeypatch.setattr(fsio, "_RETRY", False)
    (tmp_path / "c").write_bytes(b"1")
    with pytest.raises(PermissionError):
        fsio.replace(tmp_path / "c", tmp_path / "d")
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="F_FULLFSYNC is macOS")
def test_the_commit_asks_the_drive_to_flush_on_macos(tmp_path, monkeypatch):
    import fcntl

    seen = []
    real = fcntl.fcntl
    monkeypatch.setattr(fcntl, "fcntl", lambda fd, cmd, *a: seen.append(cmd) or real(fd, cmd, *a))
    fsio.append_durable(tmp_path / "log", b"x\n", commit=True)
    assert fcntl.F_FULLFSYNC in seen
    seen.clear()
    fsio.append_durable(tmp_path / "log", b"y\n")  # a pending file or a note: plain fsync is enough
    assert fcntl.F_FULLFSYNC not in seen
