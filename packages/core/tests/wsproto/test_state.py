"""``orch.wsproto.state``: the private state directory, atomic writes, symlink refusal, the sign ledger, write-once."""

from __future__ import annotations

import os
import signal

import pytest

from orch import fdwait
from orch.wsproto.state import FileSignLedger, PersistedOnce, StateDir, StateError


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
from orch.wsproto.state import StateDir
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

    def rec(led, base):
        barrier.wait()
        for i in range(20):
            led.record("card", str(base + i), "aa", "s")

    ts = [threading.Thread(target=rec, args=(a, 1)), threading.Thread(target=rec, args=(b, 101))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert len(a.keys("card")) == 40
    st.close()


# --- fork and rename (review round 3, R2 and R3) --------------------------------------------------------------------


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_forked_child_cannot_use_the_parents_state_dir(tmp_path):
    """os.fork inherits the flock (close-on-exec only covers exec): the child must refuse every operation."""
    st = StateDir(tmp_path / "relay")
    st.write("a.json", b"{}")
    pid = os.fork()
    if pid == 0:  # child: report through the exit code only
        code = 0
        for op in (lambda: st.read("a.json"), lambda: st.write("b.json", b"{}"), lambda: st.claim("c.json", b"{}")):
            try:
                op()
                code = 1
            except StateError:
                pass
            except BaseException:
                code = 2
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert st.read("a.json") == b"{}" and not (tmp_path / "relay" / "b.json").exists()
    st.close()


def _fork_and_hold(fn):
    """Fork; the child runs ``fn`` (exit code 0 = ok), reports it on a pipe, then stays alive until released."""
    r1, w1 = os.pipe()
    r2, w2 = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r1)
        os.close(w2)  # else the child's own copy of the write end keeps it waiting forever
        try:
            code = fn()
        except BaseException:
            code = 9
        os.write(w1, bytes([code]))
        os.read(r2, 1)  # stay alive until the parent is done checking
        os._exit(0)
    os.close(w1)
    os.close(r2)
    ready = fdwait.readable(r1, 20)
    code = os.read(r1, 1)[0] if ready else 255  # 255: the child hung (deadlock)
    os.close(r1)

    def release():
        os.close(w2)
        if not ready:
            os.kill(pid, signal.SIGKILL)  # our own forked child
        os.waitpid(pid, 0)

    return code, release


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_live_forked_child_does_not_keep_the_lock(tmp_path):
    """The fork hook closes the child's fd (no LOCK_UN): the parent keeps its lock while it lives, and once the parent
    closes, a new instance gets the directory although the child is still alive (review round 4, N3)."""
    st = StateDir(tmp_path / "relay")
    fd = st._held

    def child():
        if st._held is not None:
            return 1
        try:
            os.fstat(fd)
            return 2  # the child's copy of the lock fd is still open
        except OSError:
            pass
        try:
            st.read("a.json")
            return 3
        except StateError:
            pass
        st.close()  # a no-op in the child
        return 0

    code, release = _fork_and_hold(child)
    try:
        assert code == 0
        with pytest.raises(StateError, match="in use"):
            StateDir(tmp_path / "relay")  # the parent still holds it
        st.write("a.json", b"{}")
        st.close()
        StateDir(tmp_path / "relay").close()  # free while the forked child is still alive
    finally:
        release()


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_close_in_a_forked_child_leaves_the_parent_locked(tmp_path):
    """Guards against a future ``LOCK_UN`` in ``close()`` or the fork hook: it would release the parent's lock."""
    st = StateDir(tmp_path / "relay")

    def child():
        st.close()
        st.close()
        return 0

    code, release = _fork_and_hold(child)
    try:
        assert code == 0
        with pytest.raises(StateError, match="in use"):
            StateDir(tmp_path / "relay")
        st.write("a.json", b"{}")
        assert st.read("a.json") == b"{}"
    finally:
        release()
    with pytest.raises(StateError, match="in use"):
        StateDir(tmp_path / "relay")  # still the parent's after the child exited
    st.close()


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_child_forked_while_another_thread_holds_the_lock_does_not_deadlock(tmp_path):
    """The fork hook replaces the RLock: a parent thread holding it at the fork never releases it in the child."""
    st = StateDir(tmp_path / "relay")
    held, done = threading.Event(), threading.Event()

    def hold_lock():
        with st.lock:
            held.set()
            done.wait(30)

    t = threading.Thread(target=hold_lock)
    t.start()
    held.wait(30)

    def child():
        try:
            st.read("a.json")
            return 1
        except StateError:
            pass
        st.close()
        return 0

    try:
        code, release = _fork_and_hold(child)
        release()
    finally:
        done.set()
        t.join(30)
    assert code == 0
    st.close()


def test_io_stays_in_the_locked_directory_after_a_rename(tmp_path):
    """``mv relay relay.old; mkdir relay``: the first instance keeps writing into the directory it locked."""
    st = StateDir(tmp_path / "relay")
    os.rename(tmp_path / "relay", tmp_path / "relay.old")
    second = StateDir(tmp_path / "relay")  # a new, unlocked directory at the old path
    st.write("a.json", b"first")
    second.write("a.json", b"second")
    assert (tmp_path / "relay.old" / "a.json").read_bytes() == b"first"
    assert (tmp_path / "relay" / "a.json").read_bytes() == b"second"
    assert st.read("a.json") == b"first"
    st.close()
    second.close()


# --- round 6 (orch-52 #2): the custody tree check on every ancestor ---------------------------------------------------


def test_a_symlinked_ancestor_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(StateError, match="symlink"):
        StateDir(tmp_path / "link" / "relay")
    assert not (real / "relay").exists()  # refused before anything was created
    with pytest.raises(StateError, match="symlink"):
        StateDir(tmp_path / "link" / "deeper" / "relay")


def test_the_resolved_path_works_where_the_symlinked_one_is_refused(tmp_path):
    """Round 7 (R6-4): R5 must hand ``StateDir`` a RESOLVED path from trusted config. The refusal names the fix."""
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(StateError, match="pass the resolved path"):
        StateDir(tmp_path / "link" / "relay")
    with StateDir(os.path.realpath(tmp_path / "link") + "/relay") as st:
        assert st.path == real / "relay"


@pytest.mark.skipif(not os.path.islink("/tmp"), reason="/tmp is a symlink on macOS (to /private/tmp) only")
def test_macos_tmp_and_tmpdir_prefixes_are_refused():
    """Documents the macOS behaviour: ``/tmp``, ``/var/folders`` and ``$TMPDIR`` sit under symlinks (``/tmp``,
    ``/var``), so a state path under them is refused before anything is created; ``/private/...`` is the resolved form.
    The production default under ``/Users/<u>`` has no symlinked ancestor."""
    import secrets

    name = f"orch-r7-{secrets.token_hex(6)}"
    candidates = [f"/tmp/{name}"]
    tmpdir = os.environ.get("TMPDIR", "")
    if tmpdir.startswith("/var/"):
        candidates.append(os.path.join(tmpdir, name))
    for path in candidates:
        with pytest.raises(StateError, match="symlink"):
            StateDir(path)
        assert not os.path.exists(os.path.realpath(path))


def test_an_ancestor_group_writable_by_the_users_private_group_passes(tmp_path, monkeypatch):
    from orch import privdir

    monkeypatch.setattr(privdir, "_is_private_group", lambda uid, gid: True)
    parent = tmp_path / "ubuntu"
    parent.mkdir()
    parent.chmod(0o775)
    try:
        with StateDir(parent / "relay"):
            pass
        parent.chmod(0o777)  # world-writable never passes
        with pytest.raises(StateError, match="writable by another principal"):
            StateDir(parent / "relay2")
    finally:
        parent.chmod(0o700)


@pytest.mark.parametrize("mode", [0o777, 0o770, 0o757, 0o722, 0o1777])
def test_a_too_open_ancestor_is_refused(tmp_path, mode):
    """Group or other write on an ancestor owned by this user (a sticky bit only helps a root-owned one, like /tmp)."""
    wide = tmp_path / "wide"
    wide.mkdir()
    wide.chmod(mode)
    try:
        with pytest.raises(StateError, match="writable by another principal"):
            StateDir(wide / "relay")
        with pytest.raises(StateError, match="writable by another principal"):
            StateDir(wide / "a" / "relay")  # deeper: still found
    finally:
        wide.chmod(0o700)


def test_an_ancestor_of_another_owner_is_refused(tmp_path, monkeypatch):
    """Simulated: this process claims another UID, so the ancestors owned by the real user (not root) are foreign."""
    from orch.wsproto import state

    real = os.getuid()
    monkeypatch.setattr(state, "_me", lambda: real + 4242)
    with pytest.raises(StateError, match=f"owner {real}, expected root or {real + 4242}"):
        StateDir(tmp_path / "relay")
    assert not (tmp_path / "relay").exists()


def test_a_relative_path_is_checked_from_the_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with StateDir("relay") as s:
        assert s.path == tmp_path / "relay"
    (tmp_path / "l").symlink_to(tmp_path)
    with pytest.raises(StateError, match="symlink"):
        StateDir("l/relay2")


def test_parent_traversal_is_refused(tmp_path):
    with pytest.raises(StateError, match="parent traversal"):
        StateDir(f"{tmp_path}/x/../relay")


@pytest.mark.parametrize(
    "bad",
    [
        b'{"card":{"10":null}}',
        b'{"card":null}',
        b'{"k":[]}',
        b'{"card":[]}',
        b'{"card":{"10":[]}}',
        b'{"card":{"10":"x"}}',
        b'{"card":{"10":{"digest":1,"sig":"s"}}}',
        b'{"card":{"10":{"digest":"aa","sig":null}}}',
        b'{"card":{"10":{"digest":"aa"}}}',
        b'{"other":{"x":null},"card":{}}',
    ],
)
def test_a_null_or_malformed_ledger_entry_is_a_state_error_not_nothing_signed(tmp_path, bad):
    """Round 9: a ``null`` or malformed kind or entry fails closed on every read and on record; it is never "nothing
    signed", and the file is never rewritten over it."""
    with StateDir(tmp_path / "relay") as st:
        st.write(FileSignLedger.FILE, bad)
        led = FileSignLedger(st)
        with pytest.raises(StateError, match="sign ledger"):
            led.get("card", "10")
        with pytest.raises(StateError, match="sign ledger"):
            led.keys("card")
        with pytest.raises(StateError, match="sign ledger"):
            led.record("card", "10", "bb", "sig")
        assert st.read(FileSignLedger.FILE) == bad


NONCANONICAL_CARD_KEYS = ["010", " 10", "10 ", "+10", "-10", "1_0", "abc", "", "\uff11\uff10", "10\n", "1e1", "0x0a"]


@pytest.mark.parametrize("key", NONCANONICAL_CARD_KEYS)
def test_a_noncanonical_card_key_is_a_state_error_never_a_value_error_or_accepted(tmp_path, key):
    """Round 10, R9-1: a card key must be the canonical decimal ``record`` writes (``str(seq)``); ``"010"`` or ``" 10"``
    is int()-equal to 10 but missed by the exact lookup, so it fails closed on every read and on record, and the file
    is never rewritten over it."""
    import json

    bad = json.dumps({"card": {key: {"digest": "aa", "sig": "s"}}}).encode()
    with StateDir(tmp_path / "relay") as st:
        st.write(FileSignLedger.FILE, bad)
        led = FileSignLedger(st)
        for read in (
            lambda: led.get("card", "10"),
            lambda: led.keys("card"),
            lambda: led.record("card", "10", "b", "s"),
        ):
            with pytest.raises(StateError, match="sign ledger key"):
                read()
        with pytest.raises(StateError, match="sign ledger key"):
            FileSignLedger(st).record("card", key, "aa", "s")
        assert st.read(FileSignLedger.FILE) == bad


@pytest.mark.parametrize("kind", ["Card", "", "c d"])
def test_a_kind_record_would_never_write_is_a_state_error(tmp_path, kind):
    import json

    bad = json.dumps({kind: {}}).encode()
    with StateDir(tmp_path / "relay") as st:
        st.write(FileSignLedger.FILE, bad)
        with pytest.raises(StateError, match="sign ledger key"):
            FileSignLedger(st).keys("card")


def test_canonical_card_keys_are_accepted(tmp_path):
    with StateDir(tmp_path / "relay") as st:
        led = FileSignLedger(st)
        for k in ("0", "1", "10", "9223372036854775807"):
            led.record("card", k, "aa", "s")
        assert led.keys("card") == sorted(["0", "1", "10", "9223372036854775807"])


# -- R3: per-file limits and remove (11A.3 publish-manifest.json, published-restore-<n>.json) ------------------------


def test_a_larger_limit_is_per_call_and_bounded(tmp_path):
    from orch.wsproto.state import BIG_FILE, MAX_FILE

    s = StateDir(tmp_path / "big")
    data = b"x" * (MAX_FILE + 1)
    with pytest.raises(StateError):
        s.write("publish-manifest.json", data)
    s.write("publish-manifest.json", data, limit=BIG_FILE)
    with pytest.raises(StateError):
        s.read("publish-manifest.json")  # the default read limit still applies
    assert s.read("publish-manifest.json", limit=BIG_FILE) == data
    with pytest.raises(StateError):
        s.write("x.json", b"{}", limit=BIG_FILE + 1)
    s.close()


def test_remove_unlinks_the_entry_itself_and_is_idempotent(tmp_path):
    s = StateDir(tmp_path / "rm")
    s.write("publish.json", b"{}")
    target = tmp_path / "elsewhere"
    target.write_bytes(b"keep")
    (tmp_path / "rm" / "link.json").symlink_to(target)
    assert s.remove("publish.json") and not s.remove("publish.json")
    assert s.remove("link.json") and target.read_bytes() == b"keep"  # the symlink entry, never its target
    s.close()
