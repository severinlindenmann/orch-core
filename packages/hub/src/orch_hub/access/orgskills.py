"""Company skills a hub admin writes once and offers to workspaces (M3, #1430). Hub side only: nothing here reaches an
agent. A workspace's host receives its own signed ``hub.org_skills`` bundle (see ``AccessSnapshots.org_skills``), shows
the full text to its owner and keeps a skill Off until the owner accepts the exact digest (Opus M3 decisions 1-4).

The text rules live in ``contract/org_bundles.py`` and are checked again when a bundle is signed, so a row that is bad
for any reason is never served."""

from __future__ import annotations

from typing import Any

from ..contract.org_bundles import SKILLS_MAX, normal_text, skill_digest, skill_problem
from .store import Access, AccessError, _hex

_SCHEMA = """
CREATE TABLE IF NOT EXISTS org_skill (
  name TEXT PRIMARY KEY, text TEXT NOT NULL, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS org_skill_offer (
  name TEXT NOT NULL REFERENCES org_skill(name) ON DELETE CASCADE, workspace_id TEXT NOT NULL,
  PRIMARY KEY (name, workspace_id)
);
CREATE TABLE IF NOT EXISTS org_skills_bundle (
  workspace_id TEXT PRIMARY KEY, version INTEGER NOT NULL, fingerprint TEXT NOT NULL, signed TEXT NOT NULL
);
"""


class OrgSkillStore:
    def __init__(self, a: Access) -> None:
        self._a = a
        with a.lock:
            a._db.executescript(_SCHEMA)
            a._db.execute("PRAGMA foreign_keys=ON")

    def _admin(self, actor: str) -> None:
        if not self._a.is_hub_admin(_hex(actor, "actor")):
            raise AccessError("forbidden_role", "only a hub-admin manages company skills")

    def list(self) -> list[dict[str, Any]]:
        a = self._a
        offers: dict[str, list[str]] = {}
        for n, w in a._q("SELECT name, workspace_id FROM org_skill_offer ORDER BY workspace_id"):
            offers.setdefault(n, []).append(w)
        return [
            {"name": n, "text": t, "digest": skill_digest(n, t), "updated_ms": u, "offered_to": offers.get(n, [])}
            for n, t, u in a._q("SELECT name, text, updated_ms FROM org_skill ORDER BY name")
        ]

    def put(self, name: object, text: object, *, actor: str) -> dict[str, Any]:
        """Create or replace a skill. Only visible text is accepted (hidden or control characters are refused, never silently
        stripped); a name or text that is not acceptable is refused with the reason. Replacing changes the digest, so every owner has to accept again."""
        self._admin(actor)
        if type(text) is str:
            text = normal_text(text)
        problem = skill_problem(name, text)
        if problem is not None:
            raise AccessError("malformed", problem)
        a, now = self._a, self._a._now()
        with a.tx() as db:
            exists = db.execute("SELECT 1 FROM org_skill WHERE name=?", (name,)).fetchone()
            if not exists and db.execute("SELECT COUNT(*) FROM org_skill").fetchone()[0] >= SKILLS_MAX:
                raise AccessError("too_many", "company skills")
            db.execute(
                "INSERT INTO org_skill(name, text, created_ms, updated_ms) VALUES (?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET text=excluded.text, updated_ms=excluded.updated_ms",
                (name, text, now, now),
            )
        return {"name": name, "digest": skill_digest(name, text)}

    def delete(self, name: object, *, actor: str) -> None:
        self._admin(actor)
        if type(name) is not str:
            raise AccessError("malformed", "name")
        with self._a.tx() as db:
            db.execute("DELETE FROM org_skill_offer WHERE name=?", (name,))
            if db.execute("DELETE FROM org_skill WHERE name=?", (name,)).rowcount == 0:
                raise AccessError("unknown_skill")

    def set_offers(self, name: object, workspace_ids: object, *, actor: str) -> list[str]:
        """The exact set of workspaces this skill is offered to. Offering is not enabling: the owner's accept decides."""
        self._admin(actor)
        if type(workspace_ids) is not list or len(workspace_ids) > 256:
            raise AccessError("malformed", "workspace ids")
        ids = sorted({_hex(w, "workspace_id") for w in workspace_ids})
        with self._a.tx() as db:
            if not db.execute("SELECT 1 FROM org_skill WHERE name=?", (name,)).fetchone():
                raise AccessError("unknown_skill")
            db.execute("DELETE FROM org_skill_offer WHERE name=?", (name,))
            db.executemany("INSERT INTO org_skill_offer VALUES (?,?)", [(name, w) for w in ids])
        return ids

    def offered_to(self, workspace_id: str) -> list[dict[str, str]]:
        """What the bundle of ONE workspace carries (the link's workspace only, never a caller-named one)."""
        rows = self._a._q(
            "SELECT s.name, s.text FROM org_skill s JOIN org_skill_offer o ON o.name=s.name "
            "WHERE o.workspace_id=? ORDER BY s.name",
            (_hex(workspace_id, "workspace_id"),),
        )
        return [{"name": n, "text": t, "digest": skill_digest(n, t)} for n, t in rows]

    def drop_workspace(self, workspace_id: str) -> None:
        """Unbind or leave: no skill is offered to that workspace any more (same transaction as the caller's)."""
        self._a._w("DELETE FROM org_skill_offer WHERE workspace_id=?", (_hex(workspace_id, "workspace_id"),))

    def bundle_row(self, workspace_id: str) -> tuple[int, str, str] | None:
        rows = self._a._q(
            "SELECT version, fingerprint, signed FROM org_skills_bundle WHERE workspace_id=?", (workspace_id,)
        )
        return None if not rows else rows[0]


def org_skills(a: Access) -> OrgSkillStore:
    """One store per Access (the tables are created once)."""
    s = getattr(a, "_org_skills", None)
    if s is None:
        s = a._org_skills = OrgSkillStore(a)  # type: ignore[attr-defined]
    return s
