"""Remote MCP servers a hub admin offers to workspaces (M3, #1435 #1436). Hub side only: nothing here reaches an agent and
nothing here holds a credential. An entry is a name, an https address, and either no sign-in or the NAME of a secret (the
secret itself comes with the secrets slice, sealed in the browser to each workspace, never stored here). A workspace's host
shows the owner the exact address and which secret goes where, keeps the server Off until the owner accepts that digest, and
reaches it through its own proxy (Opus M3 conditions 5, 6). There is no field for a command, so a local server cannot be
offered (decision: remote only in v1)."""

from __future__ import annotations

from typing import Any

from ..contract.org_bundles import MCP_MAX, mcp_digest, mcp_problem, normal_text
from .store import Access, AccessError, _hex

_SCHEMA = """
CREATE TABLE IF NOT EXISTS org_mcp (
  name TEXT PRIMARY KEY, url TEXT NOT NULL, auth TEXT NOT NULL, secret TEXT, description TEXT NOT NULL,
  created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS org_mcp_offer (
  name TEXT NOT NULL REFERENCES org_mcp(name) ON DELETE CASCADE, workspace_id TEXT NOT NULL,
  PRIMARY KEY (name, workspace_id)
);
CREATE TABLE IF NOT EXISTS org_mcp_bundle (
  workspace_id TEXT PRIMARY KEY, version INTEGER NOT NULL, fingerprint TEXT NOT NULL, signed TEXT NOT NULL
);
"""


def _entry(row: tuple) -> dict[str, Any]:
    name, url, auth, secret, desc = row[:5]
    e = {"name": name, "url": url, "auth": auth, "secret": secret, "description": desc}
    return {**e, "digest": mcp_digest(e)}


class OrgMcpStore:
    def __init__(self, a: Access) -> None:
        self._a = a
        with a.lock:
            a._db.executescript(_SCHEMA)
            a._db.execute("PRAGMA foreign_keys=ON")

    def _admin(self, actor: str) -> None:
        if not self._a.is_hub_admin(_hex(actor, "actor")):
            raise AccessError("forbidden_role", "only a hub-admin manages MCP servers")

    def list(self) -> list[dict[str, Any]]:
        a = self._a
        offers: dict[str, list[str]] = {}
        for n, w in a._q("SELECT name, workspace_id FROM org_mcp_offer ORDER BY workspace_id"):
            offers.setdefault(n, []).append(w)
        rows = a._q("SELECT name, url, auth, secret, description, updated_ms FROM org_mcp ORDER BY name")
        return [{**_entry(r), "updated_ms": r[5], "offered_to": offers.get(r[0], [])} for r in rows]

    def put(
        self, name: object, url: object, auth: object, secret: object, description: object, *, actor: str
    ) -> dict[str, Any]:
        """Create or replace a server. A change of address, sign-in or secret is a new digest, so every owner accepts again."""
        self._admin(actor)
        desc = normal_text(description) if type(description) is str else description
        entry = {"name": name, "url": url, "auth": auth, "secret": secret, "description": desc}
        problem = mcp_problem(entry)
        if problem is not None:
            raise AccessError("malformed", problem)
        a, now = self._a, self._a._now()
        with a.tx() as db:
            exists = db.execute("SELECT 1 FROM org_mcp WHERE name=?", (name,)).fetchone()
            if not exists and db.execute("SELECT COUNT(*) FROM org_mcp").fetchone()[0] >= MCP_MAX:
                raise AccessError("too_many", "MCP servers")
            db.execute(
                "INSERT INTO org_mcp(name, url, auth, secret, description, created_ms, updated_ms) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET url=excluded.url, auth=excluded.auth, secret=excluded.secret, "
                "description=excluded.description, updated_ms=excluded.updated_ms",
                (name, url, auth, secret, desc, now, now),
            )
        return {"name": name, "digest": mcp_digest(entry)}

    def delete(self, name: object, *, actor: str) -> None:
        self._admin(actor)
        if type(name) is not str:
            raise AccessError("malformed", "name")
        with self._a.tx() as db:
            db.execute("DELETE FROM org_mcp_offer WHERE name=?", (name,))
            if db.execute("DELETE FROM org_mcp WHERE name=?", (name,)).rowcount == 0:
                raise AccessError("unknown_server")

    def set_offers(self, name: object, workspace_ids: object, *, actor: str) -> list[str]:
        """The exact set of workspaces this server is offered to. Offering is not enabling: the owner's accept decides."""
        self._admin(actor)
        if type(workspace_ids) is not list or len(workspace_ids) > 256:
            raise AccessError("malformed", "workspace ids")
        ids = sorted({_hex(w, "workspace_id") for w in workspace_ids})
        with self._a.tx() as db:
            if not db.execute("SELECT 1 FROM org_mcp WHERE name=?", (name,)).fetchone():
                raise AccessError("unknown_server")
            db.execute("DELETE FROM org_mcp_offer WHERE name=?", (name,))
            db.executemany("INSERT INTO org_mcp_offer VALUES (?,?)", [(name, w) for w in ids])
        return ids

    def offered_to(self, workspace_id: str) -> list[dict[str, Any]]:
        """What the bundle of ONE workspace carries (the link's workspace only, never a caller-named one)."""
        rows = self._a._q(
            "SELECT s.name, s.url, s.auth, s.secret, s.description FROM org_mcp s JOIN org_mcp_offer o ON o.name=s.name "
            "WHERE o.workspace_id=? ORDER BY s.name",
            (_hex(workspace_id, "workspace_id"),),
        )
        return [_entry(r) for r in rows]

    def drop_workspace(self, workspace_id: str) -> None:
        """Unbind or leave: nothing is offered to that workspace any more."""
        self._a._w("DELETE FROM org_mcp_offer WHERE workspace_id=?", (_hex(workspace_id, "workspace_id"),))

    def withdraw_all(self) -> int:
        """The hub is being removed (#1463): every tool server and every offer goes in ONE transaction, so the next
        ``mcp_servers_get`` of each workspace signs an EMPTY bundle at a higher version."""
        with self._a.tx() as db:
            n = db.execute("SELECT COUNT(*) FROM org_mcp").fetchone()[0]
            db.execute("DELETE FROM org_mcp_offer")
            db.execute("DELETE FROM org_mcp")
        return int(n)


def org_mcp(a: Access) -> OrgMcpStore:
    s = getattr(a, "_org_mcp", None)
    if s is None:
        s = a._org_mcp = OrgMcpStore(a)  # type: ignore[attr-defined]
    return s
