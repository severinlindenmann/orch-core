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

from ..contract.hub import ROLES
from ..fsutil import PrivateFileError, open_private_db

ADMIN_GROUP = "hub-admin"
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


def _grp(x: object) -> str:
    if type(x) is not str or not _GROUP.fullmatch(x):
        raise AccessError("malformed", "group name: 1-40 chars of a-z 0-9 -")
    return x


class Access:
    def __init__(self, path: str = ":memory:", *, now_ms: Callable[[], int] | None = None) -> None:
        self.lock = threading.RLock()
        self._now = now_ms or (lambda: int(time.time() * 1000))
        try:
            self._db = open_private_db(path, check_same_thread=False, isolation_level=None)
            self._db.executescript(_SCHEMA)
            self._db.execute("INSERT OR IGNORE INTO groups(name, created_ms) VALUES (?,?)", (ADMIN_GROUP, self._now()))
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
                db.execute("INSERT INTO users VALUES (?,?,?)", (user_id, name, self._now()))
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None

    def remove_user(self, user_id: str, *, actor: str | None = None) -> None:
        _hex(user_id, "user_id")
        with self.lock:
            if self._admin_only_change(user_id, actor):
                raise AccessError("forbidden", "only a hub-admin removes a hub-admin")
            if self._w("DELETE FROM users WHERE user_id=?", (user_id,)) == 0:
                raise AccessError("unknown_user")

    def users(self) -> list[dict[str, Any]]:
        return [
            {"user_id": u, "name": n, "groups": self.groups_of(u)}
            for u, n in self._q("SELECT user_id, name FROM users ORDER BY user_id")
        ]

    def has_user(self, user_id: str) -> bool:
        return bool(self._q("SELECT 1 FROM users WHERE user_id=?", (_hex(user_id, "user_id"),)))

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

    def remove_group(self, name: str) -> None:
        _grp(name)
        if name == ADMIN_GROUP:
            raise AccessError("reserved_group")
        if self._w("DELETE FROM groups WHERE name=?", (name,)) == 0:
            raise AccessError("unknown_group")

    def groups(self) -> list[dict[str, Any]]:
        return [
            {"name": g, "members": self.members(g), "grants": self.grants_of_group(g)}
            for (g,) in self._q("SELECT name FROM groups ORDER BY name")
        ]

    def members(self, group: str) -> list[str]:
        return [u for (u,) in self._q("SELECT user_id FROM members WHERE grp=? ORDER BY user_id", (_grp(group),))]

    def groups_of(self, user_id: str) -> list[str]:
        return [g for (g,) in self._q("SELECT grp FROM members WHERE user_id=? ORDER BY grp", (user_id,))]

    def is_hub_admin(self, user_id: str) -> bool:
        return bool(self._q("SELECT 1 FROM members WHERE grp=? AND user_id=?", (ADMIN_GROUP, _hex(user_id, "user_id"))))

    def _admin_only_change(self, user_id: str, actor: str | None) -> bool:
        """True if ``actor`` (a web path) may not change ``user_id``'s admin standing."""
        return actor is not None and self.is_hub_admin(user_id) and not self.is_hub_admin(actor)

    def add_member(self, group: str, user_id: str, *, actor: str | None = None) -> None:
        self._set_member(group, user_id, actor, add=True)

    def remove_member(self, group: str, user_id: str, *, actor: str | None = None) -> None:
        self._set_member(group, user_id, actor, add=False)

    def _set_member(self, group: str, user_id: str, actor: str | None, *, add: bool) -> None:
        _grp(group)
        _hex(user_id, "user_id")
        if actor is not None:
            _hex(actor, "actor")
        try:
            with self.tx() as db:
                if group == ADMIN_GROUP and actor is not None and not self.is_hub_admin(actor):
                    raise AccessError("forbidden", "only a hub-admin changes the hub-admin group")
                if not db.execute("SELECT 1 FROM groups WHERE name=?", (group,)).fetchone():
                    raise AccessError("unknown_group")
                if not db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone():
                    raise AccessError("unknown_user")
                if add:
                    db.execute("INSERT OR IGNORE INTO members VALUES (?,?)", (group, user_id))
                else:
                    db.execute("DELETE FROM members WHERE grp=? AND user_id=?", (group, user_id))
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None

    # --- grants --------------------------------------------------------------------------------------------------

    def grant(self, workspace_id: str, group: str, role: str) -> None:
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
                db.execute(
                    "INSERT INTO grants VALUES (?,?,?) ON CONFLICT(workspace_id, grp) DO UPDATE SET role=excluded.role",
                    (workspace_id, group, role),
                )
        except sqlite3.Error as e:
            raise AccessError("store_error", type(e).__name__) from None

    def revoke(self, workspace_id: str, group: str) -> None:
        if (
            self._w(
                "DELETE FROM grants WHERE workspace_id=? AND grp=?", (_hex(workspace_id, "workspace_id"), _grp(group))
            )
            == 0
        ):
            raise AccessError("not_granted")

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
        _hex(workspace_id, "workspace_id")
        out: dict[str, str] = {}
        with self.lock:  # one consistent read
            for (u,) in self._q("SELECT user_id FROM members WHERE grp=?", (ADMIN_GROUP,)):
                out[u] = "admin"
            for u, r in self._q(
                "SELECT m.user_id, g.role FROM grants g JOIN members m ON m.grp = g.grp WHERE g.workspace_id=?",
                (workspace_id,),
            ):
                if role_rank(r) > role_rank(out.get(u)):
                    out[u] = r
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

    def unrevoke_channel(self, channel_id: str) -> None:
        self._w("DELETE FROM revoked_channels WHERE channel_id=?", (_hex(channel_id, "channel_id"),))

    def revoked_channels(self) -> list[str]:
        return [c for (c,) in self._q("SELECT channel_id FROM revoked_channels ORDER BY channel_id")]

    # --- snapshot persistence (used by AccessSnapshots) -----------------------------------------------------------

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
