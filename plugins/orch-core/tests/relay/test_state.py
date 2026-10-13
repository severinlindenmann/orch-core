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
    st = StateDir(tmp_path / "relay")
    led = FileSignLedger(st)
    assert led.get("card", "1") is None and led.keys("card") == []
    led.record("card", "1", "aa", "sig1")
    led.record("card", "1", "aa", "sig1")
    st.close()
    st = StateDir(tmp_path / "relay")
    led = FileSignLedger(st)
    assert led.get("card", "1") == ("aa", "sig1")
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
    st.close()
    with StateDir(tmp_path / "relay") as again:
        assert PersistedOnce(again, "genesis-pin.json").get() == b"first"


# --- one owner per directory, atomic claims (review D1) -----------------------------------------------------------

import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402

HOLD_LOCK = """
import sys
from orch.relay.state import StateDir
st = StateDir(sys.argv[1])
print("ready", flush=True)
sys.stdin.read()
"""


def test_a_second_instance_in_this_process_is_refused(tmp_path):
    st = StateDir(tmp_path / "relay")
    with pytest.raises(StateError, match="in use"):
        StateDir(tmp_path / "relay")
    st.close()
    StateDir(tmp_path / "relay").close()


def test_a_second_instance_in_another_process_is_refused(tmp_path):
    p = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK, str(tmp_path / "relay")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        assert p.stdout.readline().strip() == "ready"
        with pytest.raises(StateError, match="in use"):
            StateDir(tmp_path / "relay")
    finally:
        p.stdin.close()
        p.wait(timeout=30)
    StateDir(tmp_path / "relay").close()  # released when the holder exits


def test_the_lock_is_not_inherited_by_a_child(tmp_path):
    st = StateDir(tmp_path / "relay")
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        st.close()
        StateDir(tmp_path / "relay").close()  # the live child does not hold the directory
    finally:
        child.stdin.close()
        child.wait(timeout=30)


def test_closed_state_dir_refuses_io(tmp_path):
    st = StateDir(tmp_path / "relay")
    st.close()
    with pytest.raises(StateError):
        st.write("a.json", b"{}")
    with pytest.raises(StateError):
        st.read("a.json")


def test_concurrent_keepers_all_get_the_first_value(tmp_path):
    st = StateDir(tmp_path / "relay")
    out: list[bytes] = []
    barrier = threading.Barrier(8)

    def keep(i):
        barrier.wait()
        out.append(PersistedOnce(st, "genesis-pin.json").keep(f"pin-{i}".encode()))

    ts = [threading.Thread(target=keep, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert len(out) == 8 and len(set(out)) == 1 and st.read("genesis-pin.json") == out[0]
    assert [p.name for p in (tmp_path / "relay").iterdir()] == ["genesis-pin.json"]
    st.close()


def test_two_ledgers_on_one_directory_lose_no_update(tmp_path):
    st = StateDir(tmp_path / "relay")
    a, b = FileSignLedger(st), FileSignLedger(st)
    barrier = threading.Barrier(2)

    def rec(led, key):
        barrier.wait()
        for i in range(20):
            led.record("card", f"{key}{i}", "aa", "s")

    ts = [threading.Thread(target=rec, args=(a, "a")), threading.Thread(target=rec, args=(b, "b"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert len(a.keys("card")) == 40
    st.close()
