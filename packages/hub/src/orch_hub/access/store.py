"""SQLite store of the hub access model: users, groups, memberships, workspace grants, revoked channels, and the
last issued snapshot per workspace (so versions never go backwards, also across restarts).

Rules enforced here, in one place:

* roles are ``read < edit < admin``; a user's role in a workspace is the highest over the groups they belong to that
  hold a grant on it; members of the reserved group ``hub-admin`` are ``admin`` in every workspace (D71) and that
  group is never a grant target, so the status cannot be handed out through a grant;
* changing the ``hub-admin`` group through a web path (``actor`` given) needs an actor who is hub-admin themselves;
  ``actor=None`` is the local CLI, which :func:`require_state_owner` has tied to the owner of the state dir;
* every store failure raises :class:`AccessError` (fail closed): callers refuse, they never guess a role.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..contract import market_policy as MP
from ..contract.canon import cj, loads_strict
from ..contract.hub import ROLES
from ..fsutil import PrivateFileError, open_private_db

ADMIN_GROUP = "hub-admin"
DAY_MS = 86_400_000
END_MAX_MS = 400 * DAY_MS  # an end date is at most this far ahead of the hub clock
SWEEP_AFTER_MS = (
    DAY_MS  # an ended membership row is deleted this long after its end (the review list shows it until then)
)
REVIEW_AFTER_MS = 90 * DAY_MS  # a permanent member not confirmed for this long is offered for review
REVIEW_SOON_MS = 14 * DAY_MS
EXTEND_MS = 30 * DAY_MS
_KEEP: Any = object()  # "leave the end date as it is"
_HEX16 = re.compile(r"[0-9a-f]{32}")
_GROUP = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS groups (name TEXT PRIMARY KEY, created_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS members (
  grp TEXT NOT NULL REFERENCES groups(name) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  PRIMARY KEY (grp, user_id)
);
CREATE TABLE IF NOT EXISTS default_group (id INTEGER PRIMARY KEY CHECK (id = 1), grp TEXT REFERENCES groups(name) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS grants (
  workspace_id TEXT NOT NULL,
  grp TEXT NOT NULL REFERENCES groups(name) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('read','edit','admin')),
  PRIMARY KEY (workspace_id, grp)
);
CREATE TABLE IF NOT EXISTS revoked_channels (channel_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS snapshots (
  workspace_id TEXT PRIMARY KEY, version INTEGER NOT NULL, fingerprint TEXT NOT NULL, signed TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS version_floor (id INTEGER PRIMARY KEY CHECK (id = 1), floor INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS market_policy (
  id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL CHECK (version >= 1),
  policy TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_addon_allow (workspace_id TEXT PRIMARY KEY, ids TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS addon_request (
  workspace_id TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL, actor TEXT NOT NULL DEFAULT 'person',
  first_ms INTEGER NOT NULL, last_ms INTEGER NOT NULL, n INTEGER NOT NULL,
  PRIMARY KEY (workspace_id, name)
);
CREATE TABLE IF NOT EXISTS recovery_notes (
  user_id TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  by_user TEXT NOT NULL, at_ms INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS default_group_no_admin_set BEFORE UPDATE OF grp ON default_group
WHEN NEW.grp IS NOT NULL AND EXISTS (SELECT 1 FROM grants WHERE grp = NEW.grp AND role = 'admin')
BEGIN SELECT RAISE(ABORT, 'default_group_admin'); END;
CREATE TRIGGER IF NOT EXISTS grants_no_admin_for_default_ins BEFORE INSERT ON grants
WHEN NEW.role = 'admin' AND NEW.grp = (SELECT grp FROM default_group WHERE id = 1)
BEGIN SELECT RAISE(ABORT, 'default_group_admin'); END;
CREATE TRIGGER IF NOT EXISTS grants_no_admin_for_default_upd BEFORE UPDATE ON grants
WHEN NEW.role = 'admin' AND NEW.grp = (SELECT grp FROM default_group WHERE id = 1)
BEGIN SELECT RAISE(ABORT, 'default_group_admin'); END;
"""


class AccessError(Exception):
    """A refused or failed access-model operation; ``code`` is stable, the text never carries secrets."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def role_rank(role: str | None) -> int:
    """0 for no role (or anything unknown: fail closed), 1..3 for read, edit, admin."""
    return ROLES.index(role) + 1 if role in ROLES else 0


def require_state_owner(state: Path) -> None:
    """Writes through the CLI need the hub state dir to be a real directory owned by the running user."""
    try:
        st = os.lstat(state)
    except OSError:
        raise AccessError("state_unreadable", state.name) from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():  # lstat: a symlink is not a directory here
        raise AccessError("not_state_owner", state.name)
    if st.st_mode & 0o022:
        raise AccessError("state_dir_writable", state.name)


def _hex(x: object, what: str) -> str:
    if type(x) is not str or not _HEX16.fullmatch(x):
        raise AccessError("malformed", f"{what} must be 32 lower-case hex characters")
    return x


def check_name(name: object) -> str:
    if type(name) is not str or not name or len(name) > 80 or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise AccessError("malformed", "name: 1-80 printable characters")
    return name


_ADDON_ID = re.compile(r"[a-z][a-z0-9-]{0,39}")
_VERSION = re.compile(r"[0-9A-Za-z.+-]{0,32}")


def _grp(x: object) -> str:
    if type(x) is not str or not _GROUP.fullmatch(x):
        raise AccessError("malformed", "group name: 1-40 chars of a-z 0-9 -")
    return x


class Access:
    def __init__(self, path: str = ":memory:", *, now_ms: Callable[[], int] | None = None) -> None:
        self.lock = threading.RLock()
        self._now = now_ms or (lambda: int(time.time() * 1000))
        #: called as ``observer(event)`` INSIDE the transaction of a grant / membership change, before it commits (D4:
        #: the audit chain). If it raises, the change is rolled back; a failed commit after it leaves an audit record
        #: for a change that did not happen, never the reverse.
        self.observer: Callable[[dict], None] | None = None
        try:
            self._db = open_private_db(path, check_same_thread=False, isolation_level=None)
            self._db.executescript(_SCHEMA)
            if "disabled_ms" not in {r[1] for r in self._db.execute("PRAGMA table_info(users)")}:
                self._db.execute("ALTER TABLE users ADD COLUMN disabled_ms INTEGER")  # NULL = enabled
            if "must_recover" not in {r[1] for r in self._db.execute("PRAGMA table_info(users)")}:
                # set by an enabling: the old password and TOTP stay dead until a recovery link is redeemed
                self._db.execute("ALTER TABLE users ADD COLUMN must_recover INTEGER NOT NULL DEFAULT 0")
            mcols = {r[1] for r in self._db.execute("PRAGMA table_info(members)")}
            for col in ("ends_ms", "added_ms", "confirmed_ms"):  # NULL ends_ms = no end date
                if col not in mcols:
                    self._db.execute(f"ALTER TABLE members ADD COLUMN {col} INTEGER")
            self._db.execute("INSERT OR IGNORE INTO default_group(id, grp) VALUES (1, NULL)")
            self._db.execute("INSERT OR IGNORE INTO groups(name, created_ms) VALUES (?,?)", (ADMIN_GROUP, self._now()))
            d = MP.default_policy()  # a new hub trusts the official marketplace only (D14)
            self._db.execute(
                "INSERT OR IGNORE INTO market_policy VALUES (1, 1, ?, ?)", (cj(d).decode(), MP.policy_hash(d))
            )
        except PrivateFileError as e:
            raise AccessError("bad_private_file", str(e)) from None
        except (sqlite3.Error, OSError) as e:
            raise AccessError("store_error", type(e).__name__) from None

    def close(self) -> None:
        with self.lock:
            self._db.close()

    def tx(self) -> _Tx:
        """One immediate transaction under the lock (read-check-write is one step)."""
        return _Tx(self)

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        try:
            with self.lock:
                return self._db.execute(sql, args).fetchall()
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None

    def _w(self, sql: str, args: tuple = ()) -> int:
        try:
            with self.tx() as db:
                return db.execute(sql, args).rowcount
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None

    # --- users ---------------------------------------------------------------------------------------------------

    def add_user(self, user_id: str, name: str) -> None:
        _hex(user_id, "user_id")
        check_name(name)
        try:
            with self.tx() as db:
                if db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
                    raise AccessError("exists", "user")
                db.execute("INSERT INTO users(user_id, name, created_ms) VALUES (?,?,?)", (user_id, name, self._now()))
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None

    def check_remove_user(self, user_id: str, actor: str | None) -> None:
        """The refusals of :meth:`remove_user` without deleting anything (``forbidden``, ``unknown_user``)."""
        _hex(user_id, "user_id")
        if self._admin_only_change(user_id, actor):
            raise AccessError("forbidden", "only a hub-admin removes a hub-admin")
        if not self.has_user(user_id):
            raise AccessError("unknown_user")

    def remove_user(self, user_id: str, *, actor: str | None = None) -> None:
        _hex(user_id, "user_id")
        try:
            with self.tx() as db:
                if self._admin_only_change(user_id, actor):
                    raise AccessError("forbidden", "only a hub-admin removes a hub-admin")
                if not db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
                    raise AccessError("unknown_user")
                # the cascade removes the user's group memberships: each one is a membership change and is audited
                for (g,) in db.execute("SELECT grp FROM members WHERE user_id=? ORDER BY grp", (user_id,)).fetchall():
                    self._observe(
                        {
                            "kind": "membership",
                            "group": g,
                            "user_id": user_id,
                            "added": False,
                            "ends_ms": None,
                            "actor_user_id": actor,
                        }
                    )
                db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None

    def users(self) -> list[dict[str, Any]]:
        return [
            {"user_id": u, "name": n, "groups": self.groups_of(u), "disabled": d is not None}
            for u, n, d in self._q("SELECT user_id, name, disabled_ms FROM users ORDER BY user_id")
        ]

    def has_user(self, user_id: str) -> bool:
        """The user exists (enabled or not): removal and credential changes still reach a disabled person."""
        return bool(self._q("SELECT 1 FROM users WHERE user_id=?", (_hex(user_id, "user_id"),)))

    def is_active(self, user_id: str) -> bool:
        """The user exists and is not disabled. Sessions, sign-in, channels and custodial signing all ask this one."""
        return bool(self._q("SELECT 1 FROM users WHERE user_id=? AND disabled_ms IS NULL", (_hex(user_id, "user_id"),)))

    def is_live(self, user_id: str) -> bool:
        """Active AND not waiting for a recovery link: what channels, channel sessions and pairing ask. An enabled person
        who has not redeemed the recovery link has NO live channel, so old devices never come back on enable."""
        return self.is_active(user_id) and not self.must_recover(user_id)

    def set_disabled(self, user_id: str, disabled: bool, *, actor: str | None = None) -> bool:
        """Set or clear the disabled flag in ONE transaction. A disabled person has no role in any workspace (``roles``
        and ``role_of`` skip them, so the next ``hub.snapshot`` fingerprint changes and its version moves up, exactly as
        for a grant change). Refused: disabling yourself (``self_disable``) and the last enabled hub admin
        (``last_admin``). Idempotent; returns whether anything changed. It touches nothing but this flag: a workspace's
        own owner key and SELECTIVE membership are not hub-derived roles and stay as they are."""
        _hex(user_id, "user_id")
        if actor is not None:
            _hex(actor, "actor")
        try:
            with self.tx() as db:
                row = db.execute("SELECT disabled_ms FROM users WHERE user_id=?", (user_id,)).fetchone()
                if row is None:
                    raise AccessError("unknown_user")
                if disabled:
                    if actor is not None and actor == user_id:
                        raise AccessError("self_disable")
                    is_admin = db.execute(
                        "SELECT 1 FROM members WHERE grp=? AND user_id=?", (ADMIN_GROUP, user_id)
                    ).fetchone()
                    if row[0] is None and is_admin:
                        (others,) = db.execute(
                            "SELECT COUNT(*) FROM members m JOIN users u ON u.user_id = m.user_id "
                            "WHERE m.grp=? AND m.user_id<>? AND u.disabled_ms IS NULL",
                            (ADMIN_GROUP, user_id),
                        ).fetchone()
                        if others == 0:
                            raise AccessError("last_admin")
                if (row[0] is not None) == disabled:
                    return False
                # an enabling NEVER brings the old credentials back: ``must_recover`` is set in the same statement that
                # clears ``disabled_ms`` (fail closed) and only a redeemed recovery link clears it
                db.execute(
                    "UPDATE users SET disabled_ms=?, must_recover=CASE WHEN ? THEN must_recover ELSE 1 END "
                    "WHERE user_id=?",
                    (self._now() if disabled else None, 1 if disabled else 0, user_id),
                )
                return True
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None

    def must_recover(self, user_id: str) -> bool:
        """The person was enabled and has not yet redeemed a recovery link: they may not sign in with anything old."""
        return bool(self._q("SELECT 1 FROM users WHERE user_id=? AND must_recover<>0", (_hex(user_id, "user_id"),)))

    def clear_must_recover(self, user_id: str) -> None:
        self._w("UPDATE users SET must_recover=0 WHERE user_id=?", (_hex(user_id, "user_id"),))

    def undo_enable(self, user_id: str, *, must_recover: bool) -> None:
        """Roll an enabling back (its recovery link could not be made): disabled again, ``must_recover`` as it was.
        Deliberately not :meth:`set_disabled`: it restores a state the hub already allowed, so no admin guard applies."""
        self._w(
            "UPDATE users SET disabled_ms=?, must_recover=? WHERE user_id=?",
            (self._now(), 1 if must_recover else 0, _hex(user_id, "user_id")),
        )

    def set_recovery_note(self, user_id: str, by_user: str, at_ms: int) -> None:
        """``{by, at_ms}`` of the last recovery of this account (a recovery link was redeemed); shown to the person."""
        _hex(user_id, "user_id")
        self._w(
            "INSERT INTO recovery_notes(user_id, by_user, at_ms) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET by_user=excluded.by_user, at_ms=excluded.at_ms",
            (user_id, by_user, at_ms),
        )

    def recovery_note(self, user_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT by_user, at_ms FROM recovery_notes WHERE user_id=?", (_hex(user_id, "user_id"),))
        return {"by": rows[0][0], "at_ms": rows[0][1]} if rows else None

    def clear_recovery_note(self, user_id: str) -> None:
        self._w("DELETE FROM recovery_notes WHERE user_id=?", (_hex(user_id, "user_id"),))

    # --- groups and membership -----------------------------------------------------------------------------------

    def add_group(self, name: str) -> None:
        _grp(name)
        if name == ADMIN_GROUP:
            raise AccessError("reserved_group")
        try:
            with self.tx() as db:
                if db.execute("SELECT 1 FROM groups WHERE name=?", (name,)).fetchone():
                    raise AccessError("exists", "group")
                db.execute("INSERT INTO groups VALUES (?,?)", (name, self._now()))
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None

    def remove_group(self, name: str, *, actor: str | None = None) -> None:
        _grp(name)
        if name == ADMIN_GROUP:
            raise AccessError("reserved_group")
        try:
            with self.tx() as db:
                if not db.execute("SELECT 1 FROM groups WHERE name=?", (name,)).fetchone():
                    raise AccessError("unknown_group")
                # the cascade removes the group's grants and memberships: each row is a change and is audited, so a
                # workspace's access cannot disappear without a record (D4)
                for (w,) in db.execute("SELECT workspace_id FROM grants WHERE grp=? ORDER BY workspace_id", (name,)):
                    self._observe(
                        {"kind": "grant", "group": name, "workspace_id": w, "role": None, "actor_user_id": actor}
                    )
                for (u,) in db.execute("SELECT user_id FROM members WHERE grp=? ORDER BY user_id", (name,)).fetchall():
                    self._observe(
                        {
                            "kind": "membership",
                            "group": name,
                            "user_id": u,
                            "added": False,
                            "ends_ms": None,
                            "actor_user_id": actor,
                        }
                    )
                db.execute("DELETE FROM groups WHERE name=?", (name,))
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None

    def groups(self) -> list[dict[str, Any]]:
        return [
            {"name": g, "members": self.members(g), "ends": self.member_ends(g), "grants": self.grants_of_group(g)}
            for (g,) in self._q("SELECT name FROM groups ORDER BY name")
        ]

    #: THE active-membership condition (alias ``m``): no end date, or the end is still ahead. Every read that turns a
    #: membership into access uses it; the ``*_all`` variants exist for clean-up paths only (disable/enable/restore).
    _ACTIVE = "(m.ends_ms IS NULL OR m.ends_ms > ?)"

    def members(self, group: str, at_ms: int | None = None) -> list[str]:
        """The members whose membership is active at ``at_ms`` (default: now)."""
        at = self._now() if at_ms is None else at_ms
        return [
            u
            for (u,) in self._q(
                f"SELECT m.user_id FROM members m WHERE m.grp=? AND {self._ACTIVE} ORDER BY m.user_id",
                (_grp(group), at),
            )
        ]

    def members_all(self, group: str) -> list[str]:
        return [u for (u,) in self._q("SELECT user_id FROM members WHERE grp=? ORDER BY user_id", (_grp(group),))]

    def groups_of(self, user_id: str, at_ms: int | None = None) -> list[str]:
        at = self._now() if at_ms is None else at_ms
        return [
            g
            for (g,) in self._q(
                f"SELECT m.grp FROM members m WHERE m.user_id=? AND {self._ACTIVE} ORDER BY m.grp", (user_id, at)
            )
        ]

    def groups_of_all(self, user_id: str) -> list[str]:
        return [g for (g,) in self._q("SELECT grp FROM members WHERE user_id=? ORDER BY grp", (user_id,))]

    def member_ends(self, group: str) -> dict[str, int]:
        """``{user_id: ends_ms}`` of the members of ``group`` that have an end date (ended ones too, until swept)."""
        return dict(self._q("SELECT user_id, ends_ms FROM members WHERE grp=? AND ends_ms IS NOT NULL", (_grp(group),)))

    def is_hub_admin(self, user_id: str) -> bool:
        """A member of the hub-admin group who is not disabled."""
        return bool(
            self._q(
                "SELECT 1 FROM members m JOIN users u ON u.user_id = m.user_id "
                "WHERE m.grp=? AND m.user_id=? AND u.disabled_ms IS NULL",
                (ADMIN_GROUP, _hex(user_id, "user_id")),
            )
        )

    def _admin_only_change(self, user_id: str, actor: str | None) -> bool:
        """True if ``actor`` (a web path) may not change ``user_id``'s admin standing (a disabled admin still counts as
        an admin here: being disabled does not make someone easier to remove)."""
        member = bool(self._q("SELECT 1 FROM members WHERE grp=? AND user_id=?", (ADMIN_GROUP, user_id)))
        return actor is not None and member and not self.is_hub_admin(actor)

    def add_member(self, group: str, user_id: str, *, actor: str | None = None, ends_ms: Any = _KEEP) -> None:
        """Add a member. ``ends_ms`` (a future time, at most 400 days out) makes the membership end then; ``None`` makes it
        permanent; left out, a new member is permanent and an existing one keeps what it had. The hub-admin group takes none."""
        self._set_member(group, user_id, actor, add=True, ends_ms=ends_ms)

    def remove_member(self, group: str, user_id: str, *, actor: str | None = None) -> None:
        self._set_member(group, user_id, actor, add=False)

    def check_end(self, ends_ms: object, group: str | None = None) -> int | None:
        """A valid end date: ``None`` or an int in ``(now, now + 400 days]`` (UTC ms). Never for the hub-admin group."""
        if ends_ms is None:
            return None
        if type(ends_ms) is not int or isinstance(ends_ms, bool):
            raise AccessError("malformed", "ends_ms")
        now = self._now()
        if ends_ms <= now:
            raise AccessError("end_in_past")
        if ends_ms > now + END_MAX_MS:
            raise AccessError("malformed", "ends_ms too far ahead")
        if group == ADMIN_GROUP:
            raise AccessError("reserved_group", "the hub-admin group has no end dates")
        return ends_ms

    def _set_member(self, group: str, user_id: str, actor: str | None, *, add: bool, ends_ms: Any = _KEEP) -> None:
        _grp(group)
        _hex(user_id, "user_id")
        if actor is not None:
            _hex(actor, "actor")
        if ends_ms is not _KEEP:
            ends_ms = self.check_end(ends_ms, group)
        try:
            with self.tx() as db:
                if group == ADMIN_GROUP and actor is not None and not self.is_hub_admin(actor):
                    raise AccessError("forbidden", "only a hub-admin changes the hub-admin group")
                if not db.execute("SELECT 1 FROM groups WHERE name=?", (group,)).fetchone():
                    raise AccessError("unknown_group")
                if not db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
                    raise AccessError("unknown_user")
                if add:
                    now = self._now()
                    # an ended row that was not swept yet counts as missing: it is replaced, so the audit and the access agree
                    db.execute(
                        "DELETE FROM members WHERE grp=? AND user_id=? AND ends_ms IS NOT NULL AND ends_ms <= ?",
                        (group, user_id, now),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO members(grp, user_id, ends_ms, added_ms, confirmed_ms) VALUES (?,?,?,?,?)",
                        (group, user_id, None if ends_ms is _KEEP else ends_ms, now, now),
                    )
                    if ends_ms is not _KEEP:  # an existing row takes the new end date (or becomes permanent)
                        db.execute(
                            "UPDATE members SET ends_ms=?, confirmed_ms=? WHERE grp=? AND user_id=?",
                            (ends_ms, now, group, user_id),
                        )
                    ends_now = db.execute(
                        "SELECT ends_ms FROM members WHERE grp=? AND user_id=?", (group, user_id)
                    ).fetchone()[0]
                else:
                    db.execute("DELETE FROM members WHERE grp=? AND user_id=?", (group, user_id))
                    ends_now = None
                self._observe(
                    {
                        "kind": "membership",
                        "group": group,
                        "user_id": user_id,
                        "added": add,
                        "ends_ms": ends_now,
                        "actor_user_id": actor,
                    }
                )
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None

    def member_end(self, group: str, user_id: str) -> int | None | str:
        """The stored end date of a membership: an int, ``None`` (permanent) or ``"absent"`` (no such row)."""
        rows = self._q("SELECT ends_ms FROM members WHERE grp=? AND user_id=?", (_grp(group), _hex(user_id, "user_id")))
        return rows[0][0] if rows else "absent"

    def set_member_end(self, group: str, user_id: str, ends_ms: object, *, actor: str | None = None) -> None:
        """Change the end date of an existing, still active membership (``None`` = permanent). One conditional UPDATE of
        the ACTIVE row: if the member was removed or ended in between, nothing is written (``not_member``); this path never
        inserts. Policy (owner approval for a later end, owner/last-admin guards for a shorter one) is ``ChangeService``."""
        _grp(group)
        _hex(user_id, "user_id")
        if actor is not None:
            _hex(actor, "actor")
        ends = self.check_end(ends_ms, group)
        try:
            with self.tx() as db:
                now = self._now()
                n = db.execute(
                    "UPDATE members SET ends_ms=?, confirmed_ms=? WHERE grp=? AND user_id=? "
                    "AND (ends_ms IS NULL OR ends_ms > ?)",
                    (ends, now, group, user_id, now),
                ).rowcount
                if n == 0:
                    raise AccessError("not_member")
                self._observe(
                    {
                        "kind": "membership",
                        "group": group,
                        "user_id": user_id,
                        "added": True,
                        "ends_ms": ends,
                        "actor_user_id": actor,
                    }
                )
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None

    def is_member(self, group: str, user_id: str) -> bool:
        """An ACTIVE membership of ``group``."""
        return user_id in self.members(group)

    def confirm_member(self, group: str, user_id: str, *, actor: str | None = None) -> None:
        """Mark a permanent membership as still needed (the review's one click)."""
        self._w(
            "UPDATE members SET confirmed_ms=? WHERE grp=? AND user_id=?",
            (self._now(), _grp(group), _hex(user_id, "user_id")),
        )

    def review(self) -> list[dict[str, Any]]:
        """The "still needed?" list: memberships ending within 14 days or ended in the last 7 (not the hub-admin group), and
        permanent ones not confirmed for 90 days. Display data; the writes go through the normal paths."""
        now = self._now()
        out = []
        for g, u, ends, conf, added in self._q(
            "SELECT m.grp, m.user_id, m.ends_ms, m.confirmed_ms, m.added_ms FROM members m JOIN users x ON x.user_id=m.user_id "
            "WHERE m.grp<>? AND x.disabled_ms IS NULL ORDER BY m.grp, m.user_id",
            (ADMIN_GROUP,),
        ):
            if ends is not None:
                if now - 7 * DAY_MS <= ends <= now + REVIEW_SOON_MS:
                    out.append({"group": g, "user_id": u, "ends_ms": ends, "ended": ends <= now})
            elif now - (conf if conf is not None else (added if added is not None else 0)) > REVIEW_AFTER_MS:
                out.append({"group": g, "user_id": u, "ends_ms": None, "ended": False})
        return out

    def sweep(self) -> int:
        """Delete memberships that ended more than a day ago and audit each as a removal (actor None). Correctness never
        depends on this: an ended membership grants nothing from its end on."""
        cutoff = self._now() - SWEEP_AFTER_MS
        n = 0
        try:
            with self.tx() as db:
                for g, u in db.execute(
                    "SELECT grp, user_id FROM members WHERE ends_ms IS NOT NULL AND ends_ms <= ?", (cutoff,)
                ).fetchall():
                    db.execute("DELETE FROM members WHERE grp=? AND user_id=?", (g, u))
                    self._observe(
                        {
                            "kind": "membership",
                            "group": g,
                            "user_id": u,
                            "added": False,
                            "ends_ms": None,
                            "actor_user_id": None,
                        }
                    )
                    n += 1
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None
        return n

    # --- the default group for new people ------------------------------------------------------------------------

    def default_group(self) -> str | None:
        rows = self._q("SELECT grp FROM default_group WHERE id=1")
        return rows[0][0] if rows else None

    def set_default_group(self, group: str | None, *, actor: str) -> None:
        """Hub admin only. Never ``hub-admin`` and never a group that holds an admin grant anywhere (R5). Every check and
        the write are ONE transaction; triggers on ``grants`` and ``default_group`` hold the rule for any other writer."""
        _hex(actor, "actor")
        if group is not None:
            _grp(group)
            if group == ADMIN_GROUP:
                raise AccessError("reserved_group")
        try:
            with self.tx() as db:
                if not self.is_hub_admin(actor):
                    raise AccessError("forbidden_role", "only a hub-admin sets the default group")
                if group is not None:
                    if not db.execute("SELECT 1 FROM groups WHERE name=?", (group,)).fetchone():
                        raise AccessError("unknown_group")
                    if db.execute("SELECT 1 FROM grants WHERE grp=? AND role='admin'", (group,)).fetchone():
                        raise AccessError("default_group_admin", "a group with admin access cannot be the default")
                db.execute("UPDATE default_group SET grp=? WHERE id=1", (group,))
        except sqlite3.Error as e:
            if "default_group_admin" in str(e):
                raise AccessError("default_group_admin") from None
            raise AccessError("store_error", type(e).__name__) from None

    # --- grants --------------------------------------------------------------------------------------------------

    def _observe(self, ev: dict) -> None:
        if self.observer is not None:
            self.observer(ev)

    def grant(self, workspace_id: str, group: str, role: str, *, actor: str | None = None) -> None:
        """Set (or change) the role a group holds in a workspace. The hub-admin group is never a grantee."""
        _hex(workspace_id, "workspace_id")
        _grp(group)
        if role not in ROLES:
            raise AccessError("malformed", f"role must be one of {', '.join(ROLES)}")
        if group == ADMIN_GROUP:
            raise AccessError("reserved_group", "hub-admin is implicit admin everywhere; it is not granted")
        try:
            with self.tx() as db:
                if not db.execute("SELECT 1 FROM groups WHERE name=?", (group,)).fetchone():
                    raise AccessError("unknown_group")
                if role == "admin" and self.default_group() == group:
                    raise AccessError("default_group_admin", "the default group cannot hold an admin grant")
                db.execute(
                    "INSERT INTO grants VALUES (?,?,?) ON CONFLICT(workspace_id, grp) DO UPDATE SET role=excluded.role",
                    (workspace_id, group, role),
                )
                self._observe(
                    {
                        "kind": "grant",
                        "group": group,
                        "workspace_id": workspace_id,
                        "role": role,
                        "actor_user_id": actor,
                    }
                )
        except sqlite3.Error as e:
            if "default_group_admin" in str(e):
                raise AccessError("default_group_admin") from None
            raise AccessError("store_error", type(e).__name__) from None

    def revoke(self, workspace_id: str, group: str, *, actor: str | None = None) -> None:
        _hex(workspace_id, "workspace_id")
        _grp(group)
        try:
            with self.tx() as db:
                if db.execute("DELETE FROM grants WHERE workspace_id=? AND grp=?", (workspace_id, group)).rowcount == 0:
                    raise AccessError("not_granted")
                self._observe(
                    {
                        "kind": "grant",
                        "group": group,
                        "workspace_id": workspace_id,
                        "role": None,
                        "actor_user_id": actor,
                    }
                )
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None

    def grants_of_group(self, group: str) -> list[dict[str, str]]:
        return [
            {"workspace_id": w, "role": r}
            for w, r in self._q("SELECT workspace_id, role FROM grants WHERE grp=? ORDER BY workspace_id", (group,))
        ]

    def grants(self, workspace_id: str | None = None) -> list[dict[str, str]]:
        sql, args = "SELECT workspace_id, grp, role FROM grants", ()
        if workspace_id is not None:
            sql, args = sql + " WHERE workspace_id=?", (_hex(workspace_id, "workspace_id"),)
        return [
            {"workspace_id": w, "group": g, "role": r} for w, g, r in self._q(sql + " ORDER BY workspace_id, grp", args)
        ]

    # --- roles ---------------------------------------------------------------------------------------------------

    def roles(self, workspace_id: str) -> dict[str, str]:
        """``{user_id: role}`` for one workspace, sorted by user id; users without a role are absent."""
        return self.roles_overlay(workspace_id)

    def roles_overlay(
        self,
        workspace_id: str,
        *,
        set_grant: tuple[str, str] | None = None,
        drop_grant: str | None = None,
        drop_group: str | None = None,
        drop_user: str | None = None,
        drop_member: tuple[str, str] | None = None,
        add_member: tuple[str, str] | None = None,
        include_disabled: bool = False,
        at_ms: int | None = None,
    ) -> dict[str, str]:
        """THE role computation: what :meth:`roles` answers, optionally as it WOULD be after one hypothetical edit (a
        grant set or dropped, a group, user or member removed, a member added). With no edit it is :meth:`roles` itself,
        so a simulation (``changes.rules.reach_delta``) and the roles the hub signs cannot drift apart. A disabled user has no
        role anywhere (these are the hub-derived roles only); ``include_disabled`` is for the simulation alone: it names the
        disabled members a change would reach, so an owner sees everyone who could ever regain access."""
        _hex(workspace_id, "workspace_id")
        at = self._now() if at_ms is None else at_ms  # memberships that ended by ``at`` give nothing
        with self.lock:  # one consistent read
            grants = {g: r for g, r in self._q("SELECT grp, role FROM grants WHERE workspace_id=?", (workspace_id,))}
            if set_grant is not None:
                grants[set_grant[0]] = set_grant[1]
            for dropped in (drop_grant, drop_group):
                if dropped is not None:
                    grants.pop(dropped, None)

            def members(group: str) -> set[str]:
                if group == drop_group:
                    return set()
                out = {
                    u
                    for (u,) in self._q(
                        "SELECT m.user_id FROM members m JOIN users x ON x.user_id = m.user_id "
                        f"WHERE m.grp=? AND {self._ACTIVE}"
                        + ("" if include_disabled else " AND x.disabled_ms IS NULL"),
                        (group, at),
                    )
                }
                if drop_member is not None and drop_member[0] == group:
                    out.discard(drop_member[1])
                if (
                    add_member is not None
                    and add_member[0] == group
                    and (include_disabled or self.is_active(add_member[1]))
                ):
                    out.add(add_member[1])
                if drop_user is not None:
                    out.discard(drop_user)
                return out

            out: dict[str, str] = {u: "admin" for u in members(ADMIN_GROUP)}
            for group, role in grants.items():
                for u in members(group):
                    if role_rank(role) > role_rank(out.get(u)):
                        out[u] = role
        return dict(sorted(out.items()))

    def ends_for(self, workspace_id: str, now: int | None = None) -> dict[str, int]:
        """``{user_id: ends_ms}`` for the signed snapshot: for each user who has a role now, the first moment their role
        would fall (rank lower than now) because a membership ends. Users whose role never falls are absent."""
        _hex(workspace_id, "workspace_id")
        now = self._now() if now is None else now
        with self.lock:
            cur = self.roles_overlay(workspace_id, at_ms=now)
            points = sorted(
                {
                    e
                    for (e,) in self._q(
                        "SELECT DISTINCT ends_ms FROM members WHERE ends_ms > ? AND grp IN "
                        "(SELECT grp FROM grants WHERE workspace_id=? UNION SELECT ?)",
                        (now, workspace_id, ADMIN_GROUP),
                    )
                }
            )
            out: dict[str, int] = {}
            for t in points:
                later = self.roles_overlay(workspace_id, at_ms=t)
                for u, r in cur.items():
                    if u not in out and role_rank(later.get(u)) < role_rank(r):
                        out[u] = t
        return dict(sorted(out.items()))

    def role_of(self, user_id: str, workspace_id: str) -> str | None:
        """The user's role in the workspace: ``read``/``edit``/``admin`` or None. Unknown ids, malformed ids and
        store errors give None or raise :class:`AccessError`; they never give a role."""
        if type(user_id) is not str or not _HEX16.fullmatch(user_id) or type(workspace_id) is not str:
            return None
        if not _HEX16.fullmatch(workspace_id):
            return None
        return self.roles(workspace_id).get(user_id)

    # --- channels ------------------------------------------------------------------------------------------------

    def revoke_channel(self, channel_id: str) -> None:
        self._w("INSERT OR IGNORE INTO revoked_channels VALUES (?)", (_hex(channel_id, "channel_id"),))

    def revoked_channels(self) -> list[str]:
        return [c for (c,) in self._q("SELECT channel_id FROM revoked_channels ORDER BY channel_id")]

    # --- the hub marketplace policy (D14) ------------------------------------------------------------------------

    def market_policy(self) -> dict[str, Any]:
        """``{version, policy, hash}``. The stored row is re-validated on every read (fail closed: a damaged row is a
        store error, never a guessed policy)."""
        rows = self._q("SELECT version, policy, hash FROM market_policy WHERE id=1")
        if not rows:
            raise AccessError("store_error", "market_policy")
        version, text, h = rows[0]
        try:
            policy = MP.check_policy(loads_strict(text))
        except MP.PolicyMalformed, ValueError:
            raise AccessError("store_error", "market_policy") from None
        if h != MP.policy_hash(policy):
            raise AccessError("store_error", "market_policy")
        return {"version": version, "policy": policy, "hash": h}

    def set_market_policy(self, base_version: object, policy: object, *, actor: str) -> dict[str, Any]:
        """Replace the whole policy. ``base_version`` must be the stored version (``conflict`` otherwise: no lost
        update). The audit record is appended inside the transaction; if that fails the change is refused."""
        if type(base_version) is not int or base_version < 1:
            raise AccessError("malformed", "base_version")
        _hex(actor, "actor")
        try:
            new = MP.check_policy(policy)
        except MP.PolicyMalformed:
            raise AccessError("malformed", "policy") from None
        h = MP.policy_hash(new)
        try:
            with self.tx() as db:
                if not self.is_hub_admin(actor):
                    raise AccessError("forbidden_role", "only a hub-admin sets the marketplace policy")
                row = db.execute("SELECT version FROM market_policy WHERE id=1").fetchone()
                if row is None:
                    raise AccessError("store_error", "market_policy")
                if row[0] != base_version:
                    raise AccessError("conflict", "market_policy")
                version = row[0] + 1
                db.execute(
                    "UPDATE market_policy SET version=?, policy=?, hash=? WHERE id=1", (version, cj(new).decode(), h)
                )
                # ORDER (fail-safe, D14): the audit record goes to audit.db, the row to access.db, so the two cannot share one
                # commit. The record is appended BEFORE this transaction commits: if the append fails the row rolls back
                # (refused); if the COMMIT then fails, an audit record exists for a change that did not happen. That is
                # the safe direction (an extra record is possible, a missing one never): the chain over-reports.
                if self.observer is None:  # never change the policy without the audit chain behind it
                    raise AccessError("store_error", "no audit")
                self.observer(
                    {
                        "kind": "market_policy",
                        "version": version,
                        "policy_hash": h,
                        "actor_user_id": actor,
                    }
                )
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None
        return {"version": version, "policy": new, "hash": h}

    # --- per-workspace add-on allowlist and the requests that feed it (M3, #1449 #1450) ---------------------------------

    def workspace_addon_allow(self, workspace_id: str) -> list[str] | None:
        """This ONE workspace's own allow list: ``None`` = no limit of its own (the hub-wide rule alone applies), a list =
        only these (and only those the hub-wide rule also allows). It can narrow, never widen (Opus condition 13)."""
        rows = self._q(
            "SELECT ids FROM workspace_addon_allow WHERE workspace_id=?", (_hex(workspace_id, "workspace_id"),)
        )
        return None if not rows else list(loads_strict(rows[0][0]))

    def set_workspace_addon_allow(self, workspace_id: str, ids: object, *, actor: str) -> list[str] | None:
        """Set (a list, possibly empty) or clear (``None``) the workspace's own allow list. Hub admin only."""
        _hex(workspace_id, "workspace_id")
        _hex(actor, "actor")
        if ids is not None and (
            type(ids) is not list
            or len(ids) > 256
            or any(type(i) is not str or not _ADDON_ID.fullmatch(i) for i in ids)
        ):
            raise AccessError("malformed", "add-on ids")
        try:
            with self.tx() as db:
                if not self.is_hub_admin(actor):
                    raise AccessError("forbidden_role", "only a hub-admin sets an allowlist")
                if ids is None:
                    db.execute("DELETE FROM workspace_addon_allow WHERE workspace_id=?", (workspace_id,))
                    return None
                clean = sorted(set(ids))
                db.execute(
                    "INSERT INTO workspace_addon_allow(workspace_id, ids) VALUES (?,?) "
                    "ON CONFLICT(workspace_id) DO UPDATE SET ids=excluded.ids",
                    (workspace_id, cj(clean).decode()),
                )
                return clean
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None

    def effective_market_policy(self, workspace_id: str) -> dict[str, Any]:
        """The policy THIS workspace's snapshot carries: the hub-wide one with ``addons.allow`` INTERSECTED with the
        workspace's own list when it has one (``null`` hub-wide = the workspace list alone). ``block`` (joined) and
        ``ceiling`` are never touched, so the result can only be narrower than the hub-wide policy, never wider. Same shape
        as :meth:`market_policy`, so the snapshot and core need no change."""
        base = self.market_policy()
        own = self.workspace_addon_allow(workspace_id)
        if own is None:
            return base
        allow = base["policy"]["addons"]["allow"]
        policy = loads_strict(cj(base["policy"]).decode())
        policy["addons"]["allow"] = sorted(set(own) if allow is None else set(allow) & set(own))
        try:
            policy = MP.check_policy(policy)
        except MP.PolicyMalformed:
            raise AccessError("store_error", "effective policy") from None
        return {"version": base["version"], "policy": policy, "hash": MP.policy_hash(policy)}

    def add_addon_request(
        self, workspace_id: str, name: object, version: object = "", actor: object = "person"
    ) -> None:
        """A workspace's host says an install was refused (``market.addon_not_allowed``). Display data only: bounded, one row
        per (workspace, add-on) with a counter, at most 50 distinct add-ons per workspace. Never an authority."""
        _hex(workspace_id, "workspace_id")
        if (
            type(name) is not str
            or not _ADDON_ID.fullmatch(name)
            or type(version) is not str
            or not _VERSION.fullmatch(version)
            or actor not in ("person", "agent")
        ):
            raise AccessError("malformed", "add-on request")
        now = self._now()
        with self.lock:
            if self._q("SELECT 1 FROM addon_request WHERE workspace_id=? AND name=?", (workspace_id, name)):
                self._w(
                    "UPDATE addon_request SET version=?, actor=?, last_ms=?, n=n+1 WHERE workspace_id=? AND name=?",
                    (version, actor, now, workspace_id, name),
                )
            elif self._q("SELECT COUNT(*) FROM addon_request WHERE workspace_id=?", (workspace_id,))[0][0] < 50:
                self._w(
                    "INSERT INTO addon_request VALUES (?,?,?,?,?,?,1)", (workspace_id, name, version, actor, now, now)
                )

    def addon_requests(self) -> list[dict[str, Any]]:
        rows = self._q(
            "SELECT workspace_id, name, version, first_ms, last_ms, n, actor FROM addon_request ORDER BY last_ms DESC"
        )
        return [
            {"workspace_id": w, "name": nm, "version": v, "first_ms": f, "last_ms": la, "count": n, "actor": actor}
            for w, nm, v, f, la, n, actor in rows
        ]

    def decide_addon_request(self, workspace_id: str, name: object, approve: bool, *, actor: str) -> None:
        """Approve or turn down a request. Approving makes the id allowed where it was refused: in the hub-wide allow list
        when that is a list (a policy change for EVERYONE, audited, and the page says so) and in this workspace's own list when
        it has one. A blocked id cannot be approved: ``block`` wins everywhere. The owner's local grant is still needed."""
        _hex(workspace_id, "workspace_id")
        _hex(actor, "actor")
        if type(name) is not str or not _ADDON_ID.fullmatch(name):
            raise AccessError("malformed", "name")
        with self.lock:
            if not self.is_hub_admin(actor):
                raise AccessError("forbidden_role", "only a hub-admin decides a request")
            if not self._q("SELECT 1 FROM addon_request WHERE workspace_id=? AND name=?", (workspace_id, name)):
                raise AccessError("unknown_request")
            if approve:
                base = self.market_policy()
                if any(b == name or b.startswith(name + "@") for b in base["policy"]["addons"]["block"]):
                    raise AccessError("blocked", "a blocked add-on cannot be allowed")
                allow = base["policy"]["addons"]["allow"]
                if allow is not None and name not in allow:
                    policy = loads_strict(cj(base["policy"]).decode())
                    policy["addons"]["allow"] = sorted({*allow, name})
                    self.set_market_policy(base["version"], policy, actor=actor)
                own = self.workspace_addon_allow(workspace_id)
                if own is not None and name not in own:
                    self.set_workspace_addon_allow(workspace_id, [*own, name], actor=actor)
            self._w("DELETE FROM addon_request WHERE workspace_id=? AND name=?", (workspace_id, name))

    # --- snapshot persistence (used by AccessSnapshots) -----------------------------------------------------------

    def version_floor(self) -> int:
        """The lowest version a signed per-workspace object may be issued after: a restore raises it above everything the
        hub ever issued (protocol §17.8), so a workspace whose row is new or was rolled back still gets a newer version."""
        rows = self._q("SELECT floor FROM version_floor WHERE id=1")
        return int(rows[0][0]) if rows else 0

    def snapshot_row(self, workspace_id: str) -> tuple[int, str, str] | None:
        rows = self._q("SELECT version, fingerprint, signed FROM snapshots WHERE workspace_id=?", (workspace_id,))
        return rows[0] if rows else None


class _Tx:
    def __init__(self, a: Access) -> None:
        self._a = a

    def __enter__(self) -> sqlite3.Connection:
        self._a.lock.acquire()
        try:
            self._a._db.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._a.lock.release()
            raise
        return self._a._db

    def __exit__(self, et: object, ev: object, tb: object) -> None:
        try:
            self._a._db.execute("COMMIT" if et is None else "ROLLBACK")
        finally:
            self._a.lock.release()
