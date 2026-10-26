"""Users, groups, grants, role_of, hub-admin rules, fail-closed store."""

from __future__ import annotations

import pytest

from orch_hub.access import ADMIN_GROUP, Access, AccessError

WS, WS2 = "a1" * 16, "b2" * 16
U1, U2, U3, U4 = (f"{i:02x}" * 16 for i in (1, 2, 3, 4))


@pytest.fixture
def acc():
    a = Access()
    for u, n in ((U1, "ann"), (U2, "bob"), (U3, "cy"), (U4, "di")):
        a.add_user(u, n)
    yield a
    a.close()


def test_role_of_all_levels(acc):
    for g in ("readers", "editors", "admins"):
        acc.add_group(g)
    acc.grant(WS, "readers", "read")
    acc.grant(WS, "editors", "edit")
    acc.grant(WS, "admins", "admin")
    acc.add_member("readers", U1)
    acc.add_member("editors", U2)
    acc.add_member("admins", U3)
    assert [acc.role_of(u, WS) for u in (U1, U2, U3, U4)] == ["read", "edit", "admin", None]
    assert acc.role_of(U1, WS2) is None  # no grant on another workspace
    assert acc.role_of("nothex", WS) is None and acc.role_of(U1, "nothex") is None


def test_highest_role_wins_and_hub_admin_everywhere(acc):
    acc.add_group("r")
    acc.add_group("e")
    acc.grant(WS, "r", "read")
    acc.grant(WS, "e", "edit")
    acc.add_member("r", U1)
    acc.add_member("e", U1)
    assert acc.role_of(U1, WS) == "edit"
    acc.add_member(ADMIN_GROUP, U4)
    assert acc.role_of(U4, WS) == "admin" and acc.role_of(U4, WS2) == "admin"
    assert acc.roles(WS2) == {U4: "admin"}


def test_group_and_user_removal_drop_roles(acc):
    acc.add_group("g")
    acc.grant(WS, "g", "edit")
    acc.add_member("g", U1)
    acc.add_member("g", U2)
    assert acc.roles(WS) == {U1: "edit", U2: "edit"}
    acc.remove_user(U1)
    assert acc.roles(WS) == {U2: "edit"}
    acc.remove_group("g")
    assert acc.roles(WS) == {} and acc.grants() == []


def test_grant_change_and_revoke(acc):
    acc.add_group("g")
    acc.add_member("g", U1)
    acc.grant(WS, "g", "read")
    acc.grant(WS, "g", "admin")
    assert acc.role_of(U1, WS) == "admin"
    acc.revoke(WS, "g")
    assert acc.role_of(U1, WS) is None
    with pytest.raises(AccessError) as e:
        acc.revoke(WS, "g")
    assert e.value.code == "not_granted"


def test_hub_admin_is_never_a_grant_target_or_deletable(acc):
    for fn in (
        lambda: acc.grant(WS, ADMIN_GROUP, "read"),
        lambda: acc.remove_group(ADMIN_GROUP),
        lambda: acc.add_group(ADMIN_GROUP),
    ):
        with pytest.raises(AccessError) as e:
            fn()
        assert e.value.code == "reserved_group"


def test_hub_admin_changes_over_a_web_path_need_a_hub_admin(acc):
    acc.add_member(ADMIN_GROUP, U1)  # local CLI path
    with pytest.raises(AccessError) as e:
        acc.add_member(ADMIN_GROUP, U2, actor=U3)
    assert e.value.code == "forbidden"
    with pytest.raises(AccessError):
        acc.remove_user(U1, actor=U3)
    acc.add_member(ADMIN_GROUP, U2, actor=U1)
    assert acc.is_hub_admin(U2)
    acc.add_group("g")
    acc.add_member("g", U3, actor=U3)  # ordinary groups are not special here (D13 checks its own policy)


def test_validation_and_unknowns(acc):
    for fn, code in (
        (lambda: acc.add_user(U1, "dup"), "exists"),
        (lambda: acc.add_user("x", "n"), "malformed"),
        (lambda: acc.add_group("Bad Name"), "malformed"),
        (lambda: acc.grant(WS, "nogroup", "read"), "unknown_group"),
        (lambda: acc.grant("zz", "x", "read"), "malformed"),
        (lambda: acc.add_member("nogroup", U1), "unknown_group"),
        (lambda: acc.remove_user("ff" * 16), "unknown_user"),
    ):
        with pytest.raises(AccessError) as e:
            fn()
        assert e.value.code == code
    acc.add_group("g")
    with pytest.raises(AccessError):
        acc.grant(WS, "g", "owner")


def test_persists_and_fails_closed(tmp_path):
    p = str(tmp_path / "a.db")
    a = Access(p)
    a.add_user(U1, "ann")
    a.add_group("g")
    a.add_member("g", U1)
    a.grant(WS, "g", "edit")
    a.close()
    assert (tmp_path / "a.db").stat().st_mode & 0o077 == 0
    b = Access(p)
    assert b.role_of(U1, WS) == "edit"
    b.close()
    with pytest.raises(AccessError) as e:
        b.role_of(U1, WS)  # closed store: never a role
    assert e.value.code == "store_error"
    bad = tmp_path / "junk.db"
    bad.write_bytes(b"not a database" * 100)
    bad.chmod(0o600)
    with pytest.raises(AccessError) as e:
        Access(str(bad))
    assert e.value.code == "store_error"
