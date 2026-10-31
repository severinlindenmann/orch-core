"""privdir.others_can_write: the one rule for "can someone else write this person-owned directory"."""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from orch import privdir

pytestmark = pytest.mark.real_private_group

UID, GID = 1000, 1000


def st(mode: int, uid: int = UID, gid: int = GID) -> os.stat_result:
    return SimpleNamespace(st_mode=stat.S_IFDIR | mode, st_uid=uid, st_gid=gid)  # type: ignore[return-value]


class Pwd:
    def __init__(self, entries):
        self.entries = entries

    def getpwuid(self, uid):
        for e in self.entries:
            if e.pw_uid == uid:
                return e
        raise KeyError(uid)

    def getpwall(self):
        return list(self.entries)


def pw(name, uid, gid):
    return SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=gid)


@pytest.fixture(autouse=True)
def db(monkeypatch):
    privdir._cache.clear()
    monkeypatch.setattr(privdir, "pwd", Pwd([pw("me", UID, GID), pw("other", 1001, 1001)]))
    monkeypatch.setattr(privdir, "grp", SimpleNamespace(getgrgid=lambda g: SimpleNamespace(gr_name="me", gr_mem=[])))
    yield
    privdir._cache.clear()


def test_private_mode_is_not_writable_by_others():
    assert not privdir.others_can_write(st(0o700), UID)
    assert not privdir.others_can_write(st(0o755), UID)


def test_own_private_group_write_is_allowed():
    assert not privdir.others_can_write(st(0o775), UID)


def test_private_group_listing_only_the_user_is_allowed(monkeypatch):
    monkeypatch.setattr(
        privdir, "grp", SimpleNamespace(getgrgid=lambda g: SimpleNamespace(gr_name="me", gr_mem=["me"]))
    )
    assert not privdir.others_can_write(st(0o775), UID)


def test_group_with_another_member_is_refused(monkeypatch):
    monkeypatch.setattr(
        privdir, "grp", SimpleNamespace(getgrgid=lambda g: SimpleNamespace(gr_name="me", gr_mem=["me", "agent"]))
    )
    assert privdir.others_can_write(st(0o775), UID)


def test_group_shared_as_primary_group_by_another_account_is_refused(monkeypatch):
    monkeypatch.setattr(privdir, "pwd", Pwd([pw("me", UID, GID), pw("other", 1001, GID)]))
    assert privdir.others_can_write(st(0o775), UID)


def test_stock_ubuntu_layout_is_allowed(monkeypatch):
    """Stock Ubuntu: umask 002, user "ubuntu", private group "ubuntu" (gid 1000), another account with its own group."""
    monkeypatch.setattr(privdir, "pwd", Pwd([pw("ubuntu", UID, GID), pw("other", 1001, 1001)]))
    monkeypatch.setattr(
        privdir, "grp", SimpleNamespace(getgrgid=lambda g: SimpleNamespace(gr_name="ubuntu", gr_mem=[]))
    )
    assert not privdir.others_can_write(st(0o775), UID)  # ~/.config, ~/.local, ~/.local/share, ~/.local/state


def test_group_numbered_like_the_user_but_named_differently_is_refused(monkeypatch):
    """gid == uid is no proof of a private group: a differently named group (even with no local entry listing others)
    stays "not private" and fail closed."""
    monkeypatch.setattr(privdir, "grp", SimpleNamespace(getgrgid=lambda g: SimpleNamespace(gr_name="staff", gr_mem=[])))
    assert privdir.others_can_write(st(0o775), UID)
    monkeypatch.setattr(
        privdir, "grp", SimpleNamespace(getgrgid=lambda g: SimpleNamespace(gr_name="staff", gr_mem=["me"]))
    )
    privdir._cache.clear()
    assert privdir.others_can_write(st(0o775), UID)


def test_shared_group_with_another_name_and_number_is_refused(monkeypatch):
    monkeypatch.setattr(privdir, "pwd", Pwd([pw("me", UID, 100), pw("other", 1001, 1001)]))
    monkeypatch.setattr(privdir, "grp", SimpleNamespace(getgrgid=lambda g: SimpleNamespace(gr_name="users", gr_mem=[])))
    assert privdir.others_can_write(st(0o775, gid=100), UID)


def test_parent_group_writable_and_owned_by_another_real_user_is_refused():
    assert privdir.others_can_write(st(0o775, uid=1001, gid=1001), UID)
    assert privdir.others_can_write(st(0o770, uid=1001, gid=GID), UID)


def test_foreign_group_is_refused():
    assert privdir.others_can_write(st(0o775, gid=20), UID)


def test_world_writable_is_refused_even_with_private_group():
    assert privdir.others_can_write(st(0o777), UID)
    assert privdir.others_can_write(st(0o707), UID)


def test_directory_of_another_owner_is_refused():
    assert privdir.others_can_write(st(0o775, uid=0), UID)


def test_unreadable_group_database_is_refused(monkeypatch):
    def boom(_):
        raise OSError("sssd down")

    monkeypatch.setattr(privdir, "grp", SimpleNamespace(getgrgid=boom))
    assert privdir.others_can_write(st(0o775), UID)


def test_missing_account_database_is_refused(monkeypatch):
    monkeypatch.setattr(privdir, "pwd", SimpleNamespace())
    assert privdir.others_can_write(st(0o775), UID)


def test_answer_is_cached_briefly(monkeypatch):
    assert not privdir.others_can_write(st(0o775), UID)
    monkeypatch.setattr(privdir, "grp", SimpleNamespace(getgrgid=lambda g: SimpleNamespace(gr_name="me", gr_mem=["x"])))
    assert not privdir.others_can_write(st(0o775), UID)
    privdir._cache.clear()
    assert privdir.others_can_write(st(0o775), UID)


def test_key_directories_and_files_keep_their_modes(tmp_path):
    d = tmp_path / "a" / "b"
    privdir.make_private_dir(d)
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    os.chmod(d, 0o775)
    assert not privdir.tighten(d)  # writable by group: never healed, as before
    assert not privdir.healable(0o775)
