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


# --- GA-07: membership end dates, review, default group -------------------------------------------------------------


class Clock:
    def __init__(self, t: int = 1_000_000_000_000) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


@pytest.fixture
def timed():
    clk = Clock()
    a = Access(now_ms=clk)
    for u, n in ((U1, "ann"), (U2, "bob"), (U3, "cy")):
        a.add_user(u, n)
    a.add_group("g")
    a.grant(WS, "g", "edit")
    yield a, clk
    a.close()


def test_end_date_boundary_is_exclusive_and_read_time(timed):
    a, clk = timed
    end = clk.t + 5000
    a.add_member("g", U1, ends_ms=end)
    a.add_member("g", U2)
    clk.t = end - 1
    assert a.role_of(U1, WS) == "edit" and a.members("g") == [U1, U2] and a.groups_of(U1) == ["g"]
    clk.t = end  # no sweeper ran: the role is gone at the boundary
    assert a.role_of(U1, WS) is None and a.roles(WS) == {U2: "edit"}
    assert a.members("g") == [U2] and a.groups_of(U1) == [] and a.groups_of_all(U1) == ["g"]


def test_end_date_validation_and_admin_group(timed):
    a, clk = timed
    for bad in (clk.t, clk.t - 1, "x", True, 1.5):
        with pytest.raises(AccessError):
            a.add_member("g", U1, ends_ms=bad)
    with pytest.raises(AccessError) as e:
        a.add_member("g", U1, ends_ms=clk.t + 401 * 86_400_000)
    assert e.value.code == "malformed"
    with pytest.raises(AccessError) as e:
        a.add_member(ADMIN_GROUP, U1, ends_ms=clk.t + 1000)
    assert e.value.code == "reserved_group"


def test_set_member_end_shorten_extend_permanent(timed):
    a, clk = timed
    a.add_member("g", U1, ends_ms=clk.t + 10_000)
    a.set_member_end("g", U1, clk.t + 20_000)
    assert a.member_end("g", U1) == clk.t + 20_000
    a.set_member_end("g", U1, None)
    assert a.member_end("g", U1) is None
    with pytest.raises(AccessError) as e:
        a.set_member_end("g", U2, clk.t + 1000)
    assert e.value.code == "not_member"
    a.add_member("g", U1)  # a plain re-add keeps what was there
    a.set_member_end("g", U1, clk.t + 1000)
    a.add_member("g", U1)
    assert a.member_end("g", U1) == clk.t + 1000


def test_ends_for_first_fall_only_when_role_falls(timed):
    a, clk = timed
    a.add_group("g2")
    a.grant(WS, "g2", "edit")
    a.add_member("g", U1, ends_ms=clk.t + 1000)
    a.add_member("g2", U1)  # still edit through g2: the role never falls
    a.add_member("g", U2, ends_ms=clk.t + 3000)
    a.add_member("g", U3, ends_ms=clk.t + 2000)
    assert a.ends_for(WS) == {U2: clk.t + 3000, U3: clk.t + 2000}


def test_sweep_review_and_default_group(timed):
    a, clk = timed
    seen = []
    a.observer = seen.append
    a.add_member("g", U1, ends_ms=clk.t + 3 * 86_400_000)
    a.add_member("g", U2)
    assert [r["user_id"] for r in a.review()] == [U1]
    clk.t += 91 * 86_400_000
    assert {(r["user_id"], r["ended"]) for r in a.review()} == {(U2, False)}  # U1 ended long ago: out of the list
    assert a.sweep() == 1 and a.members_all("g") == [U2]
    assert seen[-1] == {"kind": "membership", "group": "g", "user_id": U1, "added": False, "actor_user_id": None}
    a.confirm_member("g", U2)
    assert a.review() == []


def test_default_group_never_admin(timed):
    a, _clk = timed
    a.add_member(ADMIN_GROUP, U3)
    a.set_default_group("g", actor=U3)
    assert a.default_group() == "g"
    with pytest.raises(AccessError) as e:
        a.grant(WS2, "g", "admin")
    assert e.value.code == "default_group_admin"
    for bad, code in ((ADMIN_GROUP, "reserved_group"), ("nope", "unknown_group")):
        with pytest.raises(AccessError) as e:
            a.set_default_group(bad, actor=U3)
        assert e.value.code == code
    with pytest.raises(AccessError) as e:
        a.set_default_group(None, actor=U1)
    assert e.value.code == "forbidden_role"
    a.add_group("adm")
    a.grant(WS2, "adm", "admin")
    with pytest.raises(AccessError) as e:
        a.set_default_group("adm", actor=U3)
    assert e.value.code == "default_group_admin"
    a.set_default_group(None, actor=U3)
    a.remove_group("g")
    assert a.default_group() is None


def test_set_member_end_on_removed_member_inserts_nothing(timed):
    a, clk = timed
    a.add_member("g", U1, ends_ms=clk.t + 5000)
    a.remove_member("g", U1)
    with pytest.raises(AccessError) as e:
        a.set_member_end("g", U1, clk.t + 9000)
    assert e.value.code == "not_member" and a.members_all("g") == []
    a.add_member("g", U2, ends_ms=clk.t + 1000)
    clk.t += 1000  # ended (not swept): also not a member to extend
    with pytest.raises(AccessError) as e:
        a.set_member_end("g", U2, clk.t + 9000)
    assert e.value.code == "not_member" and a.member_end("g", U2) == clk.t


def test_default_group_triggers_refuse_direct_writes(timed):
    import sqlite3

    a, _clk = timed
    a.add_member(ADMIN_GROUP, U3)
    a.set_default_group("g", actor=U3)
    db = a._db
    with pytest.raises(sqlite3.IntegrityError, match="default_group_admin"):
        db.execute("INSERT INTO grants VALUES (?,?,?)", (WS2, "g", "admin"))
    with pytest.raises(sqlite3.IntegrityError, match="default_group_admin"):
        db.execute("UPDATE grants SET role='admin' WHERE grp='g'")
    a.add_group("adm")
    a.grant(WS2, "adm", "admin")
    with pytest.raises(sqlite3.IntegrityError, match="default_group_admin"):
        db.execute("UPDATE default_group SET grp='adm' WHERE id=1")
    assert a.default_group() == "g"


def test_default_group_rule_holds_when_a_grant_lands_after_the_check(timed):
    """The check passes, then another writer grants admin before the update: the update is refused (trigger)."""
    a, _clk = timed
    a.add_member(ADMIN_GROUP, U3)
    a.add_group("late")
    real = a.is_hub_admin

    def racing(user_id):  # runs inside set_default_group's check, then a grant sneaks in on the raw connection
        ok = real(user_id)
        a._db.execute("INSERT INTO grants VALUES (?,?,?)", (WS2, "late", "admin"))
        return ok

    a.is_hub_admin = racing  # type: ignore[method-assign]
    with pytest.raises(AccessError) as e:
        a.set_default_group("late", actor=U3)
    assert e.value.code == "default_group_admin" and a.default_group() is None
