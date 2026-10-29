"""The one rule of a restore: it never widens and never revives (M4 S6, review of 29 Oct).

A backup is old. Anything an admin took away after it (a withdrawn secret share, a skill or MCP offer, a grant, a group
member, a workspace binding, a connection, a space member, an old password) must not come back with it. Every table that a
restore takes from a backup goes through :func:`never_widen`, which has two modes:

* **in place** (a live hub exists): the result is a subset of the live hub. Tables that hold permissions or withdrawals are
  taken from the LIVE store (:data:`LIVE_TABLES` inside ``access.db``; whole stores in ``restore.LIVE_WHOLE``); grants and
  group members are intersected with the live rows (a role is the live role); every surviving person gets the LIVE
  password and TOTP row, never the backup's.
* **fresh server** (nothing live to compare): what could widen is switched off until an admin confirms it again. Every org
  secret share, every "shared with all" flag, every skill and MCP offer is moved into ``restore_pending`` (the console lists
  them; one plain click turns each back on), and every admin must recover the account with a one-time link (the old password
  and TOTP stay dead, ``must_recover``).

Checked tables (what the rule does to each): see :data:`POLICY`."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from ..contract import market_policy as MP
from ..contract.canon import cj
from ..invites import Invites

ADMIN_GROUP = "hub-admin"
#: access.db tables taken from the live store as they are (in place): withdrawals, offers, allow lists, policies
LIVE_TABLES = (
    "org_secrets",
    "org_secret_shares",
    "org_secret_wraps",
    "org_secret_use",
    "org_skill",
    "org_skill_offer",
    "org_mcp",
    "org_mcp_offer",
    "org_cleanup_pending",
    "addon_request",
    "restore_pending",
)
#: Every restored table has ONE class (the structural rule of the review, 29 Oct): RESTRICTING rows only narrow what people may
#: do (allow lists, block lists, ceilings, disabled / must_recover, revocations, lockouts): a restore always keeps them and,
#: in place, takes the STRICTER of backup and live; it never deletes or stages them. GRANTING rows widen something (shares,
#: offers, grants, company markets and keys, credentials): in place the live rows (or their intersection), on a fresh server
#: staged OFF until an admin confirms. ``log`` = appended to (live wins), ``state`` = counters / bookkeeping lifted or
#: carried by the restore itself. ``class_of`` raises KeyError for a table nobody classified (a test enforces it).
CLASSES: dict[str, str] = {
    "access.db:users": "restricting",  # fields: disabled_ms, must_recover are restricting (stricter wins); the row itself is people
    "access.db:groups": "granting",
    "access.db:members": "granting",
    "access.db:grants": "granting",
    "access.db:revoked_channels": "restricting",
    "access.db:snapshots": "state",
    "access.db:version_floor": "state",
    "access.db:market_policy": "restricting",  # fields: addons.allow/block, ceiling restricting; markets/keys granting
    "access.db:workspace_addon_allow": "restricting",
    "access.db:addon_request": "granting",
    "access.db:recovery_notes": "state",
    "access.db:restore_pending": "state",
    "access.db:org_secrets": "granting",
    "access.db:org_secret_shares": "granting",
    "access.db:org_secret_wraps": "granting",
    "access.db:org_secret_use": "log",
    "access.db:org_secrets_bundle": "state",
    "access.db:org_skill": "granting",
    "access.db:org_skill_offer": "granting",
    "access.db:org_skills_bundle": "state",
    "access.db:org_mcp": "granting",
    "access.db:org_mcp_offer": "granting",
    "access.db:org_mcp_bundle": "state",
    "access.db:org_cleanup_pending": "state",
    "channels.db:credentials": "granting",
    "channels.db:failures": "restricting",
    "channels.db:channels": "restricting",  # all revoked
    "channels.db:sessions": "granting",  # emptied
    "channels.db:pairing": "granting",  # emptied
    "channels.db:challenges": "granting",  # emptied
    "channels.db:pending_totp": "granting",  # emptied
    "channels.db:push_keys": "granting",  # emptied
    "channels.db:known_sources": "granting",  # emptied
    "channels.db:api_origins": "state",
    "channels.db:events": "log",
    "channels.db:relay_purged": "state",
    "channels.db:announcements": "state",
    "invites.db:invites": "granting",  # unused ones never travel; fresh: new recovery links
}
#: stores taken whole from the live hub in place (restore.LIVE_WHOLE); every table in them is ``state`` or ``granting``
WHOLE_STORE_CLASS = "state"


def class_of(db: str, table: str) -> str:
    """The class of ``db:table`` (``KeyError`` when nobody classified it)."""
    key = f"{db}:{table}"
    if key in CLASSES:
        return CLASSES[key]
    if db in (
        "registry.db",
        "connections.db",
        "shares.db",
        "spaces.db",
        "changes.db",
        "wk-seal.db",
        "workspace-view.db",
    ):
        return WHOLE_STORE_CLASS
    if db in ("audit.db", "activity.db"):
        return "log"
    raise KeyError(key)


#: table -> what a restore does with it (the sweep the review asked for)
POLICY = {
    "access.users": "live removals deleted, live disabled kept, all other non-admins suspended (restore.py)",
    "access.groups/members/grants": "in place: only rows the live hub still has, role = the live role; fresh: non-admins "
    "lose every group when an admin enables them (api enable)",
    "access.revoked_channels": "union with live",
    "access.org_*, addon_request": "in place: the live rows; fresh: shares and offers pending",
    "access.market_policy, workspace_addon_allow": "RESTRICTING: in place the stricter of backup and live; fresh: kept (company "
    "markets and keys staged)",
    "access.snapshots/bundles": "versions lifted above the floor (version_floor)",
    "channels.credentials": "in place: the live row (totp_last too); nobody who must recover keeps a row; fresh: admins must recover",
    "channels.failures (lockouts)": "in place: the live rows",
    "users.must_recover/disabled_ms": "the stricter value (max / live)",
    "channels.channels/sessions/pairing/...": "all revoked / emptied",
    "invites.db": "unused invites are never in a backup (S5); fresh: one recovery link per admin",
    "registry.db, connections.db, shares.db, spaces.db, changes.db, wk-seal.db, workspace-view.db": "in place: the live "
    "store; fresh: the backup's (the only source; residual, hardening #1263)",
    "audit.db, activity.db, backup key file, hub-settings.json, tls.key": "live (appended to / kept)",
}
PENDING_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS restore_pending (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
    "a TEXT NOT NULL, b TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '')"
)
KINDS = ("secret_share", "secret_all", "skill_offer", "mcp_offer", "market_company", "addon_request")


def _connect(path: Path, live: Path | None) -> sqlite3.Connection:
    c = sqlite3.connect(path.resolve().as_uri(), uri=True, isolation_level=None, timeout=15)
    c.execute("PRAGMA foreign_keys=OFF")  # the order below is ours; the live rows are consistent with each other
    if live is not None:
        c.execute("ATTACH DATABASE ? AS live", (f"{live.resolve().as_uri()}?mode=ro",))
    return c


def _has(c: sqlite3.Connection, schema: str, table: str) -> bool:
    return bool(c.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _take(c: sqlite3.Connection, table: str) -> None:
    """``table`` of the staged store becomes the live table (same rows, same columns)."""
    if not _has(c, "live", table):
        if _has(c, "main", table):
            c.execute(f"DELETE FROM main.{table}")  # the name is one of the constants above
        return
    if not _has(c, "main", table):
        c.execute(c.execute("SELECT sql FROM live.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0])
    c.execute(f"DELETE FROM main.{table}")
    c.execute(f"INSERT INTO main.{table} SELECT * FROM live.{table}")


def _in_place_access(new: Path, live: Path) -> None:
    c = _connect(new / "access.db", live / "access.db")
    try:
        c.execute("BEGIN IMMEDIATE")
        for t in LIVE_TABLES:
            _take(c, t)
        c.execute(
            "DELETE FROM main.members WHERE NOT EXISTS (SELECT 1 FROM live.members l "
            "WHERE l.grp=main.members.grp AND l.user_id=main.members.user_id)"
        )
        c.execute(
            "DELETE FROM main.grants WHERE NOT EXISTS (SELECT 1 FROM live.grants l "
            "WHERE l.workspace_id=main.grants.workspace_id AND l.grp=main.grants.grp)"
        )
        c.execute(
            "UPDATE main.grants SET role=(SELECT l.role FROM live.grants l WHERE l.workspace_id=main.grants.workspace_id "
            "AND l.grp=main.grants.grp)"
        )
        # per-user security fields: the stricter value always wins (must_recover = max, disabled = the live one if set)
        c.execute(
            "UPDATE main.users SET must_recover=1 WHERE user_id IN (SELECT user_id FROM live.users WHERE must_recover<>0)"
        )
        c.execute(
            "UPDATE main.users SET disabled_ms=(SELECT l.disabled_ms FROM live.users l WHERE l.user_id=main.users.user_id) "
            "WHERE disabled_ms IS NULL AND user_id IN (SELECT user_id FROM live.users WHERE disabled_ms IS NOT NULL)"
        )
        _merge_restricting(c)
        c.execute(
            "DELETE FROM main.groups WHERE name<>? AND name NOT IN (SELECT name FROM live.groups)", (ADMIN_GROUP,)
        )
        c.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def _in_place_credentials(new: Path, live: Path, survivors: set[str]) -> None:
    c = _connect(new / "channels.db", live / "channels.db")
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM main.credentials")
        for u in sorted(survivors):
            c.execute("INSERT INTO main.credentials SELECT * FROM live.credentials WHERE user_id=?", (u,))
        # a lockout of the live hub (failed sign-ins) is still a lockout
        c.execute("DELETE FROM main.failures")
        if _has(c, "live", "failures"):
            c.execute("INSERT INTO main.failures(kind, subject, ts_ms) SELECT kind, subject, ts_ms FROM live.failures")
        c.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def live_channels(new: Path, live: Path, now: int, actor: str | None) -> None:
    """Channels that exist only in the live hub (paired after the backup) are put into the staged store AS REVOKED, so the
    periodic relay purge (``unpurged_revoked``) still removes their push target and mailbox."""
    c = _connect(new / "channels.db", live / "channels.db")
    try:
        c.execute(
            "INSERT OR IGNORE INTO main.channels(channel_id, user_id, kind, label, factor, created_ms, revoked_ms, revoked_by) "
            "SELECT channel_id, user_id, kind, label, factor, created_ms, ?, ? FROM live.channels",
            (now, actor or "restore"),
        )
    finally:
        c.close()


def _drop_recovering(new: Path) -> None:
    """Whoever must recover has NO password and TOTP row at all (not just the flag): the backup's or the live one."""
    a = sqlite3.connect(new / "access.db")
    try:
        users = [u for (u,) in a.execute("SELECT user_id FROM users WHERE must_recover<>0")]
    finally:
        a.close()
    c = sqlite3.connect(new / "channels.db", isolation_level=None)
    try:
        for u in users:
            c.execute("DELETE FROM credentials WHERE user_id=?", (u,))
    finally:
        c.close()


def _survivors(new: Path) -> set[str]:
    c = sqlite3.connect(new / "access.db")
    try:
        return {u for (u,) in c.execute("SELECT user_id FROM users")}
    finally:
        c.close()


def _fresh_access(new: Path) -> None:
    """Fresh server: shares and offers go to ``restore_pending``; every admin must recover the account."""
    c = _connect(new / "access.db", None)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(PENDING_SCHEMA)
        c.execute("DELETE FROM restore_pending")
        names = (
            {i: n for i, n in c.execute("SELECT id, name FROM org_secrets WHERE removed_ms IS NULL")}
            if _has(c, "main", "org_secrets")
            else {}
        )
        if names:
            for sid, ws in c.execute("SELECT secret_id, workspace_id FROM org_secret_shares").fetchall():
                if sid in names:
                    c.execute(
                        "INSERT INTO restore_pending(kind,a,b,label) VALUES ('secret_share',?,?,?)",
                        (sid, ws, names[sid]),
                    )
            for (sid,) in c.execute("SELECT id FROM org_secrets WHERE removed_ms IS NULL AND shared='all'").fetchall():
                c.execute("INSERT INTO restore_pending(kind,a,label) VALUES ('secret_all',?,?)", (sid, names[sid]))
            c.execute("UPDATE org_secrets SET shared='chosen' WHERE shared='all'")
            c.execute("DELETE FROM org_secret_shares")
        for table, kind in (("org_skill_offer", "skill_offer"), ("org_mcp_offer", "mcp_offer")):
            if _has(c, "main", table):
                for name, ws in c.execute(f"SELECT name, workspace_id FROM {table}").fetchall():
                    c.execute("INSERT INTO restore_pending(kind,a,b,label) VALUES (?,?,?,?)", (kind, name, ws, name))
                c.execute(f"DELETE FROM {table}")
        _fresh_addons(c)
        c.execute(
            "UPDATE users SET must_recover=1 WHERE user_id IN (SELECT user_id FROM members WHERE grp=?)",
            (ADMIN_GROUP,),
        )
        c.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def _merge_policy(back: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """The stricter of two market policies: block lists united, allow lists and ceilings intersected (``None`` = no limit),
    the GRANTING part (markets and keys) is the live one."""
    ba, la = back["addons"], live["addons"]
    if ba["allow"] is None:
        allow = la["allow"]
    elif la["allow"] is None:
        allow = ba["allow"]
    else:
        allow = sorted(set(ba["allow"]) & set(la["allow"]))
    if back["ceiling"] is None:
        ceiling = live["ceiling"]
    elif live["ceiling"] is None:
        ceiling = back["ceiling"]
    else:
        ceiling = sorted(set(back["ceiling"]) & set(live["ceiling"]))
    return MP.check_policy(
        {
            "markets": live["markets"],
            "addons": {"allow": allow, "block": sorted(set(ba["block"]) | set(la["block"]))},
            "ceiling": ceiling,
        }
    )


def _create_like_live(c: sqlite3.Connection, table: str) -> None:
    c.execute(c.execute("SELECT sql FROM live.sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0])


def _merge_restricting(c: sqlite3.Connection) -> None:
    """In place: the market policy and the per-workspace add-on allow lists are the STRICTER of backup and live. Only one
    side has a table or a row: that side is taken as it is (a restriction is never lost because an older backup lacks it).
    Both sides have it: merged. A live policy that cannot be read or merged RAISES (the restore fails closed)."""
    for table in ("market_policy", "workspace_addon_allow"):
        if _has(c, "live", table) and not _has(c, "main", table):
            _create_like_live(c, table)
            c.execute(f"INSERT INTO main.{table} SELECT * FROM live.{table}")  # one of two constants
    if _has(c, "live", "market_policy"):
        b = c.execute("SELECT version, policy FROM main.market_policy WHERE id=1").fetchone()
        lv = c.execute("SELECT version, policy, hash FROM live.market_policy WHERE id=1").fetchone()
        if lv is not None and b is None:
            c.execute("INSERT OR REPLACE INTO main.market_policy SELECT * FROM live.market_policy WHERE id=1")
        elif lv is not None and b is not None:
            try:
                pol = _merge_policy(json.loads(b[1]), json.loads(lv[1]))
            except MP.PolicyMalformed, ValueError, KeyError, TypeError:
                raise RuntimeError("market policy cannot be merged") from None
            c.execute(
                "UPDATE main.market_policy SET version=?, policy=?, hash=? WHERE id=1",
                (max(b[0], lv[0]) + 1, cj(pol).decode(), MP.policy_hash(pol)),
            )
    if _has(c, "main", "workspace_addon_allow"):
        live_rows = (
            dict(c.execute("SELECT workspace_id, ids FROM live.workspace_addon_allow").fetchall())
            if _has(c, "live", "workspace_addon_allow")
            else {}
        )
        back_rows = dict(c.execute("SELECT workspace_id, ids FROM main.workspace_addon_allow").fetchall())
        for ws in sorted(set(live_rows) | set(back_rows)):
            if ws in live_rows and ws in back_rows:
                ids = sorted(set(json.loads(live_rows[ws])) & set(json.loads(back_rows[ws])))
                val = json.dumps(ids)
            else:
                val = live_rows.get(ws) or back_rows[ws]  # a row only narrows: whichever hub has it keeps it
            c.execute("INSERT OR REPLACE INTO main.workspace_addon_allow VALUES (?,?)", (ws, val))


def _fresh_addons(c: sqlite3.Connection) -> None:
    """Fresh server. RESTRICTING rows (the allow lists, the policy's allow / block / ceiling) stay exactly as they were.
    GRANTING rows wait in ``restore_pending``: the company markets and keys of the policy (the policy keeps the official
    market only) and the add-on requests."""
    if _has(c, "main", "market_policy"):
        row = c.execute("SELECT policy FROM market_policy WHERE id=1").fetchone()
        pol = json.loads(row[0]) if row else None
        if pol is not None and any(m["class"] != "official" for m in pol["markets"]):
            official = [m for m in pol["markets"] if m["class"] == "official"]  # never ADD a market the backup lacked
            narrow = MP.check_policy({**pol, "markets": official})
            c.execute(
                "INSERT INTO restore_pending(kind,a,label) VALUES ('market_company',?,'Company add-on markets')",
                (json.dumps(pol["markets"]),),
            )
            c.execute(
                "UPDATE market_policy SET version=version+1, policy=?, hash=? WHERE id=1",
                (cj(narrow).decode(), MP.policy_hash(narrow)),
            )
    if _has(c, "main", "addon_request"):
        for r in c.execute("SELECT * FROM addon_request").fetchall():
            c.execute(
                "INSERT INTO restore_pending(kind,a,b,label) VALUES ('addon_request',?,?,?)",
                (json.dumps(list(r)), r[0], f"Add-on request: {r[1]}"),
            )
        c.execute("DELETE FROM addon_request")


def recovery_links(new: Path, admins: frozenset[str], now_ms: Any) -> dict[str, str]:
    """One recovery invite per admin in the staged invites store: ``{admin_id: token}``. The tokens are returned to the
    caller once (the server command prints them); the store keeps only hashes."""
    inv = Invites(new / "invites.db", now_ms=now_ms)
    try:
        return {u: inv.create("recovery", created_by=u, for_user=u)[0] for u in sorted(admins)}
    finally:
        inv.close()


def never_widen(new: Path, live: Path | None) -> None:
    """Apply the rule to the access store (in place: ``live`` = the live state dir). Credentials and the fresh-server
    recovery are separate steps because they need the surviving people / the admins."""
    if live is not None:
        _in_place_access(new, live)
    else:
        _fresh_access(new)


def credentials(new: Path, live: Path | None) -> None:
    """In place: the LIVE password / TOTP row of every surviving person (an old password stays dead) and the live lockouts.
    Both modes: no credentials row for anybody who must recover. Run after :func:`never_widen`."""
    if live is not None:
        _in_place_credentials(new, live, _survivors(new))
    _drop_recovering(new)


# --- the confirm path (console) ------------------------------------------------------------------------------------------------


def pending(a: Any) -> list[dict[str, Any]]:
    """What a restore switched off, for the console: ``[{id, kind, label, workspace_id}]``."""
    if not a._q("SELECT 1 FROM sqlite_master WHERE type='table' AND name='restore_pending'"):
        return []
    return [
        {"id": i, "kind": k, "label": lb, "workspace_id": b or None}
        for i, k, b, lb in a._q("SELECT id, kind, b, label FROM restore_pending ORDER BY id")
    ]


def confirm(a: Any, pid: int) -> dict[str, Any]:
    """An admin turns one switched-off item back on. Returns the row; ``KeyError`` if there is none."""
    with a.tx() as db:
        row = db.execute("SELECT id, kind, a, b, label FROM restore_pending WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise KeyError(pid)
        _i, kind, x, y, label = row
        if kind == "secret_share":
            db.execute(
                "INSERT OR IGNORE INTO org_secret_shares SELECT ?,? WHERE EXISTS "
                "(SELECT 1 FROM org_secrets WHERE id=? AND removed_ms IS NULL)",
                (x, y, x),
            )
        elif kind == "secret_all":
            db.execute("UPDATE org_secrets SET shared='all' WHERE id=? AND removed_ms IS NULL", (x,))
        elif kind == "skill_offer":
            db.execute(
                "INSERT OR IGNORE INTO org_skill_offer SELECT ?,? WHERE EXISTS (SELECT 1 FROM org_skill WHERE name=?)",
                (x, y, x),
            )
        elif kind == "mcp_offer":
            db.execute(
                "INSERT OR IGNORE INTO org_mcp_offer SELECT ?,? WHERE EXISTS (SELECT 1 FROM org_mcp WHERE name=?)",
                (x, y, x),
            )
        elif kind == "market_company":
            cur = json.loads(db.execute("SELECT policy FROM market_policy WHERE id=1").fetchone()[0])
            have = {m["url"] for m in cur["markets"]}
            staged = [m for m in json.loads(x) if m["class"] != "official" and m["url"] not in have]
            markets = sorted([*cur["markets"], *staged], key=lambda m: m["url"])  # official is never re-added
            policy = MP.check_policy({**cur, "markets": markets})  # only the markets come back; restrictions stay
            db.execute(
                "UPDATE market_policy SET version=version+1, policy=?, hash=? WHERE id=1",
                (cj(policy).decode(), MP.policy_hash(policy)),
            )
        elif kind == "addon_request":
            db.execute("INSERT OR IGNORE INTO addon_request VALUES (?,?,?,?,?,?,?)", json.loads(x))
        db.execute("DELETE FROM restore_pending WHERE id=?", (pid,))
    return {"id": pid, "kind": kind, "label": label}
