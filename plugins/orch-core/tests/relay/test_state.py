"""``orch.relay.state``: the private state directory, atomic writes, symlink refusal, the sign ledger, write-once."""

from __future__ import annotations

import os

import pytest

from orch.relay.state import FileSignLedger, PersistedOnce, StateDir, StateError


def test_creates_a_0700_directory_and_0600_files(tmp_path):
    s = StateDir(tmp_path / "relay")
    assert oct(os.stat(tmp_path / "relay").st_mode & 0o777) == "0o700"
    s.write("a.json", b"{}")
    assert oct(os.stat(tmp_path / "relay" / "a.json").st_mode & 0o777) == "0o600"
    assert s.read("a.json") == b"{}" and s.read("missing") is None
    assert [p.name for p in (tmp_path / "relay").iterdir()] == ["a.json"]  # no temp file left


def test_refuses_a_wide_or_symlinked_directory(tmp_path):
    d = tmp_path / "wide"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    with pytest.raises(StateError):
        StateDir(d)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(StateError):
        StateDir(tmp_path / "link")


def test_never_reads_through_a_symlink(tmp_path):
    s = StateDir(tmp_path / "relay")
    secret = tmp_path / "secret"
    secret.write_bytes(b"x")
    os.chmod(secret, 0o600)
    (tmp_path / "relay" / "a.json").symlink_to(secret)
    with pytest.raises(StateError):
        s.read("a.json")


def test_write_replaces_a_symlink_without_following_it(tmp_path):
    s = StateDir(tmp_path / "relay")
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    (tmp_path / "relay" / "a.json").symlink_to(victim)
    s.write("a.json", b"new")
    assert victim.read_bytes() == b"keep"
    assert not (tmp_path / "relay" / "a.json").is_symlink() and s.read("a.json") == b"new"


def test_refuses_a_wide_file(tmp_path):
    s = StateDir(tmp_path / "relay")
    s.write("a.json", b"{}")
    os.chmod(tmp_path / "relay" / "a.json", 0o644)
    with pytest.raises(StateError):
        s.read("a.json")


@pytest.mark.parametrize("name", ["../x", "a/b", "", ".hidden", "A.json", "x" * 80])
def test_bad_names(tmp_path, name):
    with pytest.raises(StateError):
        StateDir(tmp_path / "relay").write(name, b"")


def test_ledger_round_trip_and_conflict(tmp_path):
    led = FileSignLedger(StateDir(tmp_path / "relay"))
    assert led.get("card", "1") is None and led.keys("card") == []
    led.record("card", "1", "aa", "sig1")
    led.record("card", "1", "aa", "sig1")
    assert FileSignLedger(StateDir(tmp_path / "relay")).get("card", "1") == ("aa", "sig1")
    with pytest.raises(StateError):
        led.record("card", "1", "bb", "sig2")
    led.record("card", "2", "cc", "sig3")
    assert led.keys("card") == ["1", "2"]


def test_persisted_once(tmp_path):
    st = StateDir(tmp_path / "relay")
    p = PersistedOnce(st, "genesis-pin.json")
    assert p.get() is None
    assert p.keep(b"first") == b"first"
    assert p.keep(b"second") == b"first"
    assert PersistedOnce(StateDir(tmp_path / "relay"), "genesis-pin.json").get() == b"first"
