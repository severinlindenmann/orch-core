"""open_private_db: private from the first byte, refuses symlinks, loose modes, foreign owners, writable parents."""

from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from orch_hub import fsutil
from orch_hub.access import Access, AccessError
from orch_hub.fsutil import PrivateFileError, open_private_db
from orch_hub.link import Registry


@pytest.fixture
def d(tmp_path):
    p = tmp_path / "state"
    p.mkdir(mode=0o700)
    return p


def mode(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


def test_fresh_create_is_0600_before_first_write(d, monkeypatch):
    monkeypatch.setattr(os, "umask", os.umask)  # keep the real one
    old = os.umask(0o000)  # a wide umask must not matter
    try:
        db = open_private_db(str(d / "x.db"))
        assert mode(d / "x.db") == 0o600  # file exists and is private before any statement ran
        db.execute("CREATE TABLE t(a)")
        db.execute("INSERT INTO t VALUES (1)")
        db.commit()
        db.close()
    finally:
        os.umask(old)
    assert mode(d / "x.db") == 0o600


def test_journal_and_wal_files_are_private(d):
    old = os.umask(0o000)
    try:
        db = open_private_db(str(d / "x.db"), isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE t(a)")
        db.execute("INSERT INTO t VALUES (1)")
        sidecars = [p for p in d.iterdir() if p.name.startswith("x.db-")]
        assert {p.name for p in sidecars} & {"x.db-wal", "x.db-shm"}
        assert all(mode(p) & 0o077 == 0 for p in sidecars)
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (2)")
        db.execute("COMMIT")
        db.close()
    finally:
        os.umask(old)


def test_symlink_refused_even_to_a_good_file(d):
    open_private_db(str(d / "real.db")).close()
    os.symlink(d / "real.db", d / "link.db")
    with pytest.raises(PrivateFileError):
        open_private_db(str(d / "link.db"))
    os.symlink(d / "nowhere.db", d / "dangling.db")
    with pytest.raises(PrivateFileError):
        open_private_db(str(d / "dangling.db"))
    assert not (d / "nowhere.db").exists()


def test_loose_existing_file_refused_not_repaired(d):
    p = d / "x.db"
    sqlite3.connect(p).close()
    os.chmod(p, 0o644)
    with pytest.raises(PrivateFileError):
        open_private_db(str(p))
    assert mode(p) == 0o644


def test_other_owner_refused(d, monkeypatch):
    p = d / "x.db"
    open_private_db(str(p)).close()
    real = os.fstat

    def fake(fd):
        st = real(fd)
        return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid + 1, *st[5:]))

    monkeypatch.setattr(fsutil.os, "fstat", fake)
    with pytest.raises(PrivateFileError):
        open_private_db(str(p))


def test_loose_sidecar_and_writable_parent_refused(d):
    p = d / "x.db"
    open_private_db(str(p)).close()
    (d / "x.db-journal").write_bytes(b"")
    os.chmod(d / "x.db-journal", 0o644)
    with pytest.raises(PrivateFileError):
        open_private_db(str(p))
    os.unlink(d / "x.db-journal")
    os.chmod(d, 0o770)
    with pytest.raises(PrivateFileError):
        open_private_db(str(p))


def test_access_and_registry_use_it(d):
    os.symlink(d / "a", d / "access.db")
    with pytest.raises(AccessError) as e:
        Access(str(d / "access.db"))
    assert e.value.code == "bad_private_file"
    Registry(str(d / "registry.db")).close()
    assert mode(d / "registry.db") == 0o600
    os.chmod(d / "registry.db", 0o666)
    with pytest.raises(PrivateFileError):
        Registry(str(d / "registry.db"))
    Registry().close()  # :memory: still works


def test_route_queue_uses_it(d):
    from orch_hub.router.queue import RouteQueue

    RouteQueue(str(d / "route-queue.db")).close()
    assert mode(d / "route-queue.db") == 0o600
    os.chmod(d / "route-queue.db", 0o644)
    with pytest.raises(PrivateFileError):
        RouteQueue(str(d / "route-queue.db"))
