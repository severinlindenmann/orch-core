"""Company secrets, sealed in the admin's browser to each workspace's key (M3, #1431-#1433). Hub side only.

The hub never holds a value or a key that opens one: a row here is a name, a version, an expiry, who it is shared with, and
one opaque ``sealed`` wrap per workspace (``contract/org_secrets.py`` has the construction and the shape rules). No column
holds a value. Every change that narrows who can read a secret (unshare, remove, chosen to all, unbind) runs in ONE
transaction, so a failure leaves no half-removed state (Opus S-2 condition 7)."""

from __future__ import annotations

import datetime
import re
from collections import deque
from collections.abc import Mapping
from typing import Any

from ..contract import hub as H
from ..contract.canon import CanonError, b64u, unb64u
from ..contract.hub import Refused
from ..contract.org_secrets import EXPIRY_MAX_MS, NAME, SECRETS_MAX, sealed_shape_ok
from .store import Access, AccessError, _hex

_SCHEMA = """
CREATE TABLE IF NOT EXISTS org_secrets (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL, expires_ms INTEGER NOT NULL,
  shared TEXT NOT NULL CHECK (shared IN ('all','chosen')), created_by TEXT NOT NULL, created_ms INTEGER NOT NULL,
  removed_ms INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS org_secrets_live_name ON org_secrets(name) WHERE removed_ms IS NULL;
CREATE TABLE IF NOT EXISTS org_secret_shares (
  secret_id TEXT NOT NULL, workspace_id TEXT NOT NULL, PRIMARY KEY (secret_id, workspace_id)
);
CREATE TABLE IF NOT EXISTS org_secret_wraps (
  secret_id TEXT NOT NULL, version INTEGER NOT NULL, workspace_id TEXT NOT NULL, wxk_version INTEGER NOT NULL,
  sealed BLOB NOT NULL, PRIMARY KEY (secret_id, workspace_id)
);
CREATE TABLE IF NOT EXISTS org_secrets_bundle (
  workspace_id TEXT PRIMARY KEY, version INTEGER NOT NULL, fingerprint TEXT NOT NULL, signed TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS org_secret_use (
  secret_id TEXT NOT NULL, workspace_id TEXT NOT NULL, day INTEGER NOT NULL, n INTEGER NOT NULL,
  PRIMARY KEY (secret_id, workspace_id, day)
);
"""
DAY_MS = 86_400_000
USE_N_MAX = 1_000_000  # a count a host reports for one day: bounded, and shown as untrusted
USE_PER_HOUR = 120  # reports per workspace per hour


def workspace_wxk(registry: Any, ws: str, now_ms: int) -> tuple[bytes, int] | None:
    """The workspace's exchange key and its version, ONLY from the VERIFIED v2 card in the registry (the owner's PK signs
    the delegation, the pinned WSK signs the card), as the shared-space wraps do. Anything else is ``None``: a key is never
    taken from an unverified card, a request, or the browser."""
    try:
        pin = registry.pin(ws)
        if pin is None or registry.mode(ws) is None:
            return None
        c = H.verify_v2_card(registry.card(ws), unb64u(pin["pk_pub"], 65), now_ms=now_ms)
        if c["workspace_id"] != ws or c["wsk_pub"] != pin["wsk_pub"]:
            return None
        return unb64u(c["wxk_pub"], 65), int(c["wxk_version"])
    except Refused, TypeError, KeyError, CanonError, ValueError:
        return None


class OrgSecretStore:
    def __init__(self, a: Access) -> None:
        self._a = a
        self._hits: dict[str, deque[int]] = {}
        with a.lock:
            a._db.executescript(_SCHEMA)

    def _admin(self, actor: str) -> None:
        if not self._a.is_hub_admin(_hex(actor, "actor")):
            raise AccessError("forbidden_role", "only a hub-admin manages company secrets")

    # --- reads -------------------------------------------------------------------------------------------------------

    def get(self, name: object) -> dict[str, Any] | None:
        if type(name) is not str:
            return None
        rows = self._a._q(
            "SELECT id, version, expires_ms, shared FROM org_secrets WHERE name=? AND removed_ms IS NULL", (name,)
        )
        if not rows:
            return None
        return {"id": rows[0][0], "name": name, "version": rows[0][1], "expires_ms": rows[0][2], "shared": rows[0][3]}

    def list(self, now_ms: int) -> list[dict[str, Any]]:
        """Every live secret with its share set, its wraps (workspace and WXK version only) and this week's use counts.
        Never a wrap's bytes."""
        a = self._a
        shares: dict[str, list[str]] = {}
        for sid, w in a._q("SELECT secret_id, workspace_id FROM org_secret_shares ORDER BY workspace_id"):
            shares.setdefault(sid, []).append(w)
        wraps: dict[str, dict[str, int]] = {}
        for sid, w, v in a._q("SELECT secret_id, workspace_id, wxk_version FROM org_secret_wraps"):
            wraps.setdefault(sid, {})[w] = v
        since = now_ms // DAY_MS - 6
        use: dict[str, int] = {}
        for sid, n in a._q("SELECT secret_id, SUM(n) FROM org_secret_use WHERE day>=? GROUP BY secret_id", (since,)):
            use[sid] = int(n)
        rows = a._q(
            "SELECT id, name, version, expires_ms, shared, created_ms FROM org_secrets WHERE removed_ms IS NULL ORDER BY name"
        )
        return [
            {
                "id": i,
                "name": n,
                "version": v,
                "expires_ms": e,
                "shared": s,
                "created_ms": c,
                "workspaces": shares.get(i, []),
                "wraps": wraps.get(i, {}),
                "used_this_week": use.get(i, 0),
            }
            for i, n, v, e, s, c in rows
        ]

    # --- writes: every one a single transaction ------------------------------------------------------------------------

    def put(
        self,
        *,
        actor: str,
        name: object,
        secret_id: object,
        version: object,
        expires_ms: object,
        shared: object,
        workspaces: list[str],
        wraps: object,
        required: Mapping[str, int],
        now_ms: int,
    ) -> dict[str, Any]:
        """Create (version 1) or replace the wraps of a secret (version exactly current + 1). ``required`` is
        ``{workspace_id: verified wxk_version}`` for the bound workspaces of the share set; the wraps must be exactly that
        set (no extra, no duplicate, no missing), each for that WXK version. Atomic: the version bump and the whole wrap set
        change together or not at all."""
        self._admin(actor)
        if type(name) is not str or NAME.fullmatch(name) is None:
            raise AccessError("malformed", "name")
        sid = _hex(secret_id, "secret_id")
        if type(version) is not int or version < 1 or version >= 2**32:
            raise AccessError("malformed", "version")
        if type(expires_ms) is not int or not now_ms < expires_ms <= now_ms + EXPIRY_MAX_MS:
            raise AccessError("malformed", "expiry")
        if shared not in ("all", "chosen"):
            raise AccessError("malformed", "shared")
        if type(wraps) is not list or len(wraps) > 256:
            raise AccessError("malformed", "wraps")
        ws_chosen = sorted({_hex(w, "workspace_id") for w in workspaces})
        if shared == "chosen" and not ws_chosen:
            raise AccessError("malformed", "workspaces")  # a secret shared with no workspace is a dead end
        got: dict[str, tuple[int, bytes]] = {}
        for w in wraps:
            if type(w) is not dict or set(w) != {"workspace_id", "wxk_version", "sealed"}:
                raise AccessError("malformed", "wrap")
            wid = _hex(w["workspace_id"], "workspace_id")
            sealed = w["sealed"]
            if (
                wid in got
                or type(w["wxk_version"]) is not int
                or type(sealed) is not bytes
                or not sealed_shape_ok(sealed)
            ):
                raise AccessError("malformed", "wrap")
            got[wid] = (w["wxk_version"], sealed)
        if set(got) != set(required) or any(got[w][0] != required[w] for w in required):
            raise AccessError("conflict", "recipients changed")  # the page fetches the recipients again
        a = self._a
        with a.tx() as db:
            cur = db.execute(
                "SELECT id, version FROM org_secrets WHERE name=? AND removed_ms IS NULL", (name,)
            ).fetchone()
            if cur is None:
                if version != 1:
                    raise AccessError("conflict", "version")
                if db.execute("SELECT 1 FROM org_secrets WHERE id=?", (sid,)).fetchone():
                    raise AccessError("conflict", "id")
                if db.execute("SELECT COUNT(*) FROM org_secrets WHERE removed_ms IS NULL").fetchone()[0] >= SECRETS_MAX:
                    raise AccessError("too_many", "company secrets")
                db.execute(
                    "INSERT INTO org_secrets(id, name, version, expires_ms, shared, created_by, created_ms) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (sid, name, 1, expires_ms, shared, actor, now_ms),
                )
            else:
                if cur[0] != sid or version != cur[1] + 1:
                    raise AccessError("conflict", "version")
                db.execute(
                    "UPDATE org_secrets SET version=?, expires_ms=?, shared=? WHERE id=?",
                    (version, expires_ms, shared, sid),
                )
            db.execute("DELETE FROM org_secret_wraps WHERE secret_id=?", (sid,))
            db.execute("DELETE FROM org_secret_shares WHERE secret_id=?", (sid,))
            if shared == "chosen":
                db.executemany("INSERT INTO org_secret_shares VALUES (?,?)", [(sid, w) for w in ws_chosen])
            db.executemany(
                "INSERT INTO org_secret_wraps VALUES (?,?,?,?,?)",
                [(sid, version, w, got[w][0], got[w][1]) for w in sorted(got)],
            )
        return {"id": sid, "name": name, "version": version, "created": cur is None}

    def set_share(self, name: object, shared: object, workspaces: object, *, actor: str) -> dict[str, Any]:
        """Change who a secret is shared with. A workspace that leaves the share loses its wrap in the SAME transaction;
        chosen to all keeps the wraps (and drops the chosen list). A workspace that joins the share has no wrap until the
        value is entered again (the hub cannot make one)."""
        self._admin(actor)
        if shared not in ("all", "chosen") or type(workspaces) is not list or len(workspaces) > 256:
            raise AccessError("malformed", "share")
        ids = sorted({_hex(w, "workspace_id") for w in workspaces})
        with self._a.tx() as db:
            cur = db.execute("SELECT id FROM org_secrets WHERE name=? AND removed_ms IS NULL", (name,)).fetchone()
            if cur is None:
                raise AccessError("unknown_item")
            sid = cur[0]
            db.execute("UPDATE org_secrets SET shared=? WHERE id=?", (shared, sid))
            db.execute("DELETE FROM org_secret_shares WHERE secret_id=?", (sid,))
            if shared == "chosen":
                db.executemany("INSERT INTO org_secret_shares VALUES (?,?)", [(sid, w) for w in ids])
                keep = ",".join("?" * len(ids)) or "NULL"
                db.execute(
                    f"DELETE FROM org_secret_wraps WHERE secret_id=? AND workspace_id NOT IN ({keep})", (sid, *ids)
                )
        return {"id": sid, "shared": shared, "workspaces": ids if shared == "chosen" else []}

    def remove(self, name: object, *, actor: str, now_ms: int) -> str:
        """Remove a secret: its wraps, shares and use counts go with it in one transaction (a wrap a host already took cannot
        be taken back; the console says so)."""
        self._admin(actor)
        with self._a.tx() as db:
            cur = db.execute("SELECT id FROM org_secrets WHERE name=? AND removed_ms IS NULL", (name,)).fetchone()
            if cur is None:
                raise AccessError("unknown_item")
            sid = cur[0]
            db.execute("UPDATE org_secrets SET removed_ms=? WHERE id=?", (now_ms, sid))
            db.execute("DELETE FROM org_secret_wraps WHERE secret_id=?", (sid,))
            db.execute("DELETE FROM org_secret_shares WHERE secret_id=?", (sid,))
            db.execute("DELETE FROM org_secret_use WHERE secret_id=?", (sid,))
        return sid

    def drop_workspace(self, workspace_id: str) -> None:
        """Unbind or leave: no share and no wrap of that workspace remains. One transaction."""
        ws = _hex(workspace_id, "workspace_id")
        with self._a.tx() as db:
            db.execute("DELETE FROM org_secret_shares WHERE workspace_id=?", (ws,))
            db.execute("DELETE FROM org_secret_wraps WHERE workspace_id=?", (ws,))
            db.execute("DELETE FROM org_secret_use WHERE workspace_id=?", (ws,))

    def withdraw_all(self, now_ms: int) -> int:
        """The hub is being removed (#1463): every secret is marked removed and every wrap, share and use count goes, in ONE
        transaction. The next ``org_secrets_get`` of each workspace signs an EMPTY bundle at a higher version. Returns the
        number of secrets withdrawn."""
        with self._a.tx() as db:
            n = db.execute("UPDATE org_secrets SET removed_ms=? WHERE removed_ms IS NULL", (now_ms,)).rowcount
            db.execute("DELETE FROM org_secret_wraps")
            db.execute("DELETE FROM org_secret_shares")
            db.execute("DELETE FROM org_secret_use")
        return n

    # --- what ONE workspace's bundle carries ---------------------------------------------------------------------------

    def wraps_for(self, workspace_id: str, now_ms: int) -> list[dict[str, Any]]:
        """The entries of the bundle of the LINK's workspace: its own wraps, of live, unexpired secrets shared with it.
        The workspace id is the authenticated link's, never a caller-named one."""
        ws = _hex(workspace_id, "workspace_id")
        rows = self._a._q(
            "SELECT s.id, s.name, w.version, s.expires_ms, w.wxk_version, w.sealed FROM org_secrets s "
            "JOIN org_secret_wraps w ON w.secret_id=s.id AND w.version=s.version "
            "WHERE w.workspace_id=? AND s.removed_ms IS NULL AND s.expires_ms>? "
            "AND (s.shared='all' OR EXISTS (SELECT 1 FROM org_secret_shares h WHERE h.secret_id=s.id AND h.workspace_id=w.workspace_id)) "
            "ORDER BY s.name",
            (ws, now_ms),
        )
        return [
            {"id": i, "name": n, "version": v, "expires_ms": e, "wxk_version": x, "sealed": b64u(bytes(sl))}
            for i, n, v, e, x, sl in rows
        ]

    # --- the use log (counts only) -------------------------------------------------------------------------------------

    def record_use(self, workspace_id: str, secret_id: object, day: object, n: object, now_ms: int) -> None:
        """A host reports how often a secret was used on one UTC day (the day's total so far; the larger value is kept).
        Bounded, rate limited per workspace, accepted only for a live secret shared with that workspace. Display data."""
        ws = _hex(workspace_id, "workspace_id")
        sid = _hex(secret_id, "secret_id")
        today = now_ms // DAY_MS
        try:
            if type(day) is not str or re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None:
                raise ValueError
            day = (datetime.date.fromisoformat(day) - datetime.date(1970, 1, 1)).days
        except ValueError:
            raise Refused("malformed") from None
        if type(day) is not int or type(n) is not int or not today - 7 <= day <= today + 1 or not 0 <= n <= USE_N_MAX:
            raise Refused("malformed")
        q = self._hits.setdefault(ws, deque())
        while q and q[0] <= now_ms - 3_600_000:
            q.popleft()
        if len(q) >= USE_PER_HOUR:
            raise Refused("rate_limited")
        q.append(now_ms)
        if len(self._hits) > 10_000:
            for k in [k for k, v in self._hits.items() if not v or v[-1] <= now_ms - 3_600_000]:
                del self._hits[k]
        with self._a.tx() as db:
            ok = db.execute(
                "SELECT 1 FROM org_secrets s WHERE s.id=? AND s.removed_ms IS NULL AND (s.shared='all' OR EXISTS "
                "(SELECT 1 FROM org_secret_shares h WHERE h.secret_id=s.id AND h.workspace_id=?))",
                (sid, ws),
            ).fetchone()
            if not ok:
                raise Refused("malformed")
            db.execute(
                "INSERT INTO org_secret_use VALUES (?,?,?,?) ON CONFLICT(secret_id, workspace_id, day) "
                "DO UPDATE SET n=MAX(n, excluded.n)",
                (sid, ws, day, n),
            )
            db.execute(
                "DELETE FROM org_secret_use WHERE secret_id=? AND workspace_id=? AND day<?", (sid, ws, today - 30)
            )

    def use_by_workspace(self, secret_id: str, now_ms: int) -> dict[str, int]:
        since = now_ms // DAY_MS - 6
        return {
            w: int(n)
            for w, n in self._a._q(
                "SELECT workspace_id, SUM(n) FROM org_secret_use WHERE secret_id=? AND day>=? GROUP BY workspace_id",
                (secret_id, since),
            )
        }


def org_secrets(a: Access) -> OrgSecretStore:
    """One store per Access (the tables are created once)."""
    s = getattr(a, "_org_secrets", None)
    if s is None:
        s = a._org_secrets = OrgSecretStore(a)  # type: ignore[attr-defined]
    return s
