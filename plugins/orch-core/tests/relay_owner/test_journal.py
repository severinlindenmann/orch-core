"""The primary's durable per-request journal (11A.15.9, N7): transitions, immutability, conflicts, the derived
``CertJournal``, retention, file hygiene and the cross-client lock."""

from __future__ import annotations

import os
import threading

import pytest

from orch import canon
from orch.relay_owner.journal import (
    RETENTION_MS,
    Entry,
    JournalError,
    PrimaryJournal,
    RequestConflict,
    journal_dir,
)

RID, RID2 = "ab" * 16, "cd" * 16
WS, WS2 = "11" * 16, "22" * 16
REQ = canon.dumps({"o": {"request_id": RID}, "sig": "x"})
CH = canon.dumps({"o": {"kind": "cert_challenge"}, "sig": "y"})
DEC = canon.dumps({"o": {"kind": "device_cert"}, "sig": "z"})


@pytest.fixture
def jdir(tmp_path):
    return tmp_path.resolve() / "requests"


def verified(rid=RID, ws=WS, req=REQ, exp=1000):
    return Entry(rid, ws, exp, "verified", request_cj=req)


def test_journal_dir_is_next_to_the_primary_keys(tmp_path):
    assert journal_dir(tmp_path) == tmp_path / "person" / "primary" / "requests"


def test_the_happy_path_and_the_derived_cert_journal(jdir):
    j = PrimaryJournal(jdir)
    with j.transaction() as v:
        v.put(verified())
        cj = v.cert_journal()
        assert cj.seen == {RID} and cj.open == ((WS, RID, 1000),) and cj.closed == frozenset()
        assert cj.open_request(WS, 999) == RID
    with j.transaction() as v:
        e = v.get(RID)
        v.put(Entry(RID, WS, 1000, "challenged", request_cj=REQ, challenge_cj=CH))
    with j.transaction() as v:
        v.put(Entry(RID, WS, 1000, "decided", request_cj=REQ, challenge_cj=CH, decision="sign", decided_cj=DEC))
        v.put(Entry(RID, WS, 1000, "uploaded", request_cj=REQ, challenge_cj=CH, decision="sign", decided_cj=DEC))
    with j.transaction() as v:
        e = v.get(RID)
        assert e.state == "uploaded" and e.decided == canon.loads_strict(DEC) and e.challenge == canon.loads_strict(CH)
        cj = v.cert_journal()
        assert cj.closed == {RID} and cj.open_request(WS, 999) is None
        assert v.cert_journal(excluding=RID).seen == frozenset()


def test_files_are_private(jdir):
    with PrimaryJournal(jdir).transaction() as v:
        v.put(verified())
    assert (jdir.stat().st_mode & 0o777) == 0o700
    assert (os.stat(jdir / f"{RID}.json").st_mode & 0o777) == 0o600


@pytest.mark.parametrize(
    "bad",
    [
        Entry(RID, WS, 1000, "challenged", request_cj=REQ, challenge_cj=CH),  # skipped verified
        Entry(RID, WS, 1000, "uploaded", request_cj=REQ, decision="reject", decided_cj=DEC),
    ],
)
def test_illegal_first_transitions_are_refused(jdir, bad):
    with PrimaryJournal(jdir).transaction() as v, pytest.raises(JournalError) as e:
        v.put(bad)
    assert e.value.code == "journal.transition"


def test_terminal_states_never_go_back(jdir):
    j = PrimaryJournal(jdir)
    with j.transaction() as v:
        v.put(verified())
        v.put(Entry(RID, WS, 1000, "expired", request_cj=REQ))
        for st, kw in [("challenged", {"challenge_cj": CH}), ("decided", {"decision": "sign", "decided_cj": DEC})]:
            with pytest.raises(JournalError):
                v.put(Entry(RID, WS, 1000, st, request_cj=REQ, **kw))


def test_stored_bytes_never_change(jdir):
    with PrimaryJournal(jdir).transaction() as v:
        v.put(verified())
        v.put(Entry(RID, WS, 1000, "challenged", request_cj=REQ, challenge_cj=CH))
        other = canon.dumps({"o": {"kind": "cert_challenge", "n": 2}, "sig": "y"})
        with pytest.raises(JournalError):  # a re-signed challenge is never stored over the first
            v.put(Entry(RID, WS, 1000, "decided", request_cj=REQ, challenge_cj=other, decision="reject",
                        decided_cj=DEC))  # fmt: skip
        with pytest.raises(JournalError):
            v.put(Entry(RID, WS, 2000, "decided", request_cj=REQ, challenge_cj=CH, decision="reject", decided_cj=DEC))


def test_same_request_id_with_other_bytes_is_a_conflict(jdir):
    with PrimaryJournal(jdir).transaction() as v:
        v.put(verified())
        assert v.put(verified()) == v.get(RID)  # the same bytes: nothing new
        with pytest.raises(RequestConflict) as e:
            v.put(verified(req=canon.dumps({"o": {"request_id": RID, "x": 1}, "sig": "x"})))
        assert e.value.code == "primary_request_conflict"
        v.put(Entry(RID2, WS2, 5, "refused"))
        with pytest.raises(RequestConflict):  # a burned id never becomes verified
            v.put(verified(rid=RID2, ws=WS2))
        assert v.cert_journal().seen == {RID, RID2} and RID2 not in v.cert_journal().closed


def test_retention_prunes_to_tombstones_that_keep_the_id_terminal(jdir):
    j = PrimaryJournal(jdir)
    with j.transaction() as v:
        v.put(verified(exp=1000))
        v.put(Entry(RID, WS, 1000, "decided", request_cj=REQ, decision="reject", decided_cj=DEC))
        v.put(verified(rid=RID2, exp=10**12))
        assert v.prune(1000 + RETENTION_MS) == 0
        assert v.prune(1001 + RETENTION_MS) == 1
    with j.transaction() as v:
        t = v.get(RID)
        assert t.tombstone and t.state == "decided" and t.request is None and t.decided is None
        assert RID in v.cert_journal().seen and RID in v.cert_journal().closed
        assert v.get(RID2).request is not None


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"{}",
        canon.dumps({"v": 1}),
        Entry(RID2, WS, 1, "refused").to_json(),  # names another request than its file
        Entry(RID, WS, 1, "verified", request_cj=REQ).to_json().replace(b'"verified"', b'"signed!"'),
        Entry(RID, WS, 1, "verified", request_cj=REQ).to_json().replace(b'"request":{', b'"request":[{', 1),
    ],
)
def test_a_damaged_record_fails_closed(jdir, raw):
    with PrimaryJournal(jdir).transaction() as v:
        v.put(verified())
    fd = os.open(jdir / f"{RID}.json", os.O_WRONLY | os.O_TRUNC)
    os.write(fd, raw)
    os.close(fd)
    with pytest.raises(JournalError) as e, PrimaryJournal(jdir).transaction():
        pass
    assert e.value.code == "journal.corrupt"


def test_a_symlinked_or_wide_record_is_refused(jdir, tmp_path):
    with PrimaryJournal(jdir).transaction() as v:
        v.put(verified())
    os.chmod(jdir / f"{RID}.json", 0o644)
    with pytest.raises(JournalError), PrimaryJournal(jdir).transaction():
        pass
    os.unlink(jdir / f"{RID}.json")
    target = tmp_path / "elsewhere.json"
    target.write_bytes(verified().to_json())
    os.symlink(target, jdir / f"{RID}.json")
    with pytest.raises(JournalError), PrimaryJournal(jdir).transaction():
        pass


def test_a_world_readable_directory_is_refused(jdir):
    with PrimaryJournal(jdir).transaction() as v:
        v.put(verified())
    os.chmod(jdir, 0o755)
    with pytest.raises(JournalError), PrimaryJournal(jdir).transaction():
        pass


def test_two_owner_clients_never_hold_the_journal_at_once(jdir):
    """``primary_concurrent_owner_clients``: a second transaction waits for the first, then sees its write."""
    j1, j2 = PrimaryJournal(jdir), PrimaryJournal(jdir, lock_wait_s=5.0)
    inside, release, seen = threading.Event(), threading.Event(), []

    def first():
        with j1.transaction() as v:
            inside.set()
            release.wait(5)
            v.put(verified())

    t = threading.Thread(target=first)
    t.start()
    assert inside.wait(5)

    def second():
        with j2.transaction() as v:
            seen.append(v.get(RID))

    t2 = threading.Thread(target=second)
    t2.start()
    t2.join(0.3)
    assert t2.is_alive() and seen == []  # blocked behind the first
    release.set()
    t.join(5)
    t2.join(5)
    assert seen and seen[0].state == "verified"


def test_a_held_journal_times_out_busy(jdir):
    clock = [0.0]

    def sleep(s):
        clock[0] += s

    with PrimaryJournal(jdir).transaction():
        with pytest.raises(JournalError) as e, PrimaryJournal(jdir, mono=lambda: clock[0], sleep=sleep).transaction():
            pass
    assert e.value.code == "journal.busy"
