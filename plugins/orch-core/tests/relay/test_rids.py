"""R2: ``rids.sqlite`` (11A.3): the checked open, the seq window, one transaction per transition, retention."""

from __future__ import annotations

import os

import pytest

from orch.relay.rids import CHUNK_KEEP_MS, RID_KEEP_MS, SEQ_WINDOW, TOMBSTONE_KEEP_MS, Crash, RidStore, seq_accept
from orch.wsproto.state import StateDir, StateError

D, R = "aa" * 16, "bb" * 16


@pytest.fixture
def store(tmp_path):
    with StateDir(tmp_path / "relay") as sd:
        s = RidStore(sd)
        yield s
        s.close()


def _admit(s, rid=R, *, kind="write", seq=1, first=1000, window=(1, 1)):
    s.admit(
        device=D, rid=rid, first_ms=first, req_digest="sha256:x", epoch=2, seq=seq, principal="r:d_" + D,
        idem="I", kind=kind, window=window, submission=("new_ticket", "L", [{"id": "e"}]) if kind == "write" else None,
    )  # fmt: skip


def test_seq_window_matches_the_reference():
    ok, high, bm = seq_accept(0, 0, 1)
    assert ok and (high, bm) == (1, 1)
    assert seq_accept(1, 1, 1)[0] is False  # used
    assert seq_accept(1, 1, 0)[0] is False  # zero
    ok, high, bm = seq_accept(100, 1, 100 - SEQ_WINDOW + 1)
    assert ok and high == 100
    assert seq_accept(100, 1, 100 - SEQ_WINDOW)[0] is False  # below the window
    ok, high, bm = seq_accept(5, 1, 5 + SEQ_WINDOW)
    assert ok and bm == 1  # a jump past the window resets it


def test_t1_t2_and_the_submission_lifecycle(store):
    _admit(store)
    rec = store.get(D, R)
    assert rec.state == "admitted" and store.submission(D, R) == ("new_ticket", "L", [{"id": "e"}])
    assert store.window(D) == (1, 1)
    assert store.answer(D, R, [(0, b"c0"), (1, b"c1")])
    rec = store.get(D, R)
    assert rec.state == "answered" and [c.state for c in rec.chunks] == ["stored", "stored"]
    assert store.submission(D, R) is None
    assert not store.answer(D, R, [(0, b"x")])  # only an admitted rid is answered, once


def test_an_admitted_rid_is_never_admitted_twice(store):
    _admit(store)
    with pytest.raises(Exception):  # noqa: B017 - the primary key
        _admit(store)


def test_a_crash_inside_a_transaction_leaves_nothing(tmp_path):
    def fault(p):
        if p == "before_T1":
            raise Crash(p)

    with StateDir(tmp_path / "relay") as sd:
        s = RidStore(sd, fault=fault)
        with pytest.raises(Crash):
            _admit(s)
        s.close()
        s = RidStore(sd)
        assert s.get(D, R) is None and s.window(D) == (0, 0)
        s.close()


def test_attempted_chunks_are_frozen(store):
    _admit(store)
    store.answer(D, R, [(0, b"c0"), (1, b"c1")])
    store.mark(D, R, 0, "attempted")
    with pytest.raises(ValueError):
        store.replace_unattempted(D, R, [(0, 2, b"other")])
    store.replace_unattempted(D, R, [(1, 2, b"refusal")])
    assert [c.data for c in store.get(D, R).chunks] == [b"c0", b"refusal"]


def test_cursor_never_moves_back(store):
    store.set_cursor(5)
    with pytest.raises(ValueError):
        store.set_cursor(4)
    assert store.cursor() == 5


def test_retention(store):
    _admit(store, kind="read", first=0)
    store.answer(D, R, [(0, b"c0")])
    _admit(store, rid="cc" * 16, first=0, seq=2, window=(2, 3))  # admitted: never touched
    store.sweep(CHUNK_KEEP_MS)
    assert store.get(D, R).chunks[0].data is None
    store.sweep(RID_KEEP_MS)
    assert store.get(D, R).state == "tombstone" and store.get(D, R).chunks == ()
    assert store.get(D, "cc" * 16).state == "admitted"
    store.sweep(RID_KEEP_MS + TOMBSTONE_KEEP_MS)
    assert store.get(D, R) is None and store.get(D, "cc" * 16) is not None
    assert store.window(D) == (2, 3)  # the window is never dropped


def test_a_symlinked_sidecar_refuses_the_open(tmp_path):
    with StateDir(tmp_path / "relay") as sd:
        os.symlink(tmp_path / "elsewhere", tmp_path / "relay" / "rids.sqlite-wal")
        with pytest.raises(StateError):
            RidStore(sd)


def test_a_wide_mode_or_hard_link_refuses_the_open(tmp_path):
    with StateDir(tmp_path / "relay") as sd:
        RidStore(sd).close()
        os.chmod(tmp_path / "relay" / "rids.sqlite", 0o644)
        with pytest.raises(StateError):
            RidStore(sd)
        os.chmod(tmp_path / "relay" / "rids.sqlite", 0o600)
        os.link(tmp_path / "relay" / "rids.sqlite", tmp_path / "linked")
        with pytest.raises(StateError):
            RidStore(sd)


def test_files_are_private(tmp_path):
    with StateDir(tmp_path / "relay") as sd:
        RidStore(sd).close()
    for name in ("rids.sqlite", "rids.sqlite-wal", "rids.sqlite-shm"):
        p = tmp_path / "relay" / name
        if p.exists():
            assert (os.stat(p).st_mode & 0o777) == 0o600
