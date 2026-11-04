"""What a workspace that unbinds or leaves must not keep: its company secrets (shares, sealed wraps, use counts), the skills
and the tool servers offered to it (Opus M3 condition 3). The drop runs after the registry commit, so it can fail on its own;
a failure is therefore REMEMBERED (in memory at once, in the access database when it can be written) and the drop is retried
until it is done. While a drop is pending for a workspace the hub serves it no company content at all (fail closed)."""

from __future__ import annotations

import logging
import threading

from ..contract.hub import Refused
from .orgmcp import org_mcp
from .orgsecrets import org_secrets
from .orgskills import org_skills
from .store import Access, AccessError

_log = logging.getLogger("orch_hub.access")
_SCHEMA = "CREATE TABLE IF NOT EXISTS org_cleanup_pending (workspace_id TEXT PRIMARY KEY)"


class OrgCleanup:
    def __init__(self, a: Access) -> None:
        self._a = a
        self._mem: set[str] = set()
        self._lock = threading.Lock()
        with a.lock:
            a._db.executescript(_SCHEMA)
        # the three stores create their tables now, never inside a transaction later
        org_secrets(a), org_skills(a), org_mcp(a)

    def _stored(self) -> set[str]:
        try:
            return {r[0] for r in self._a._q("SELECT workspace_id FROM org_cleanup_pending")}
        except AccessError:
            return set()

    def pending(self) -> set[str]:
        with self._lock:
            mem = set(self._mem)
        return mem | self._stored()

    def is_pending(self, workspace_id: str) -> bool:
        """True while a drop for this workspace is not done. A store that cannot be read counts as pending."""
        with self._lock:
            if workspace_id in self._mem:
                return True
        try:
            return bool(self._a._q("SELECT 1 FROM org_cleanup_pending WHERE workspace_id=?", (workspace_id,)))
        except AccessError:
            return True

    def mark(self, workspace_id: str) -> None:
        """Say a drop is owed BEFORE the registry commit that makes it necessary: a crash between the commit and the drop then
        still leaves the mark on disk (and nothing is served meanwhile). If the mark cannot be written this REFUSES
        (``unavailable``) so the caller aborts before it changes anything: fail closed. ``drop`` clears the mark."""
        with self._lock:
            had = workspace_id in self._mem
            self._mem.add(workspace_id)
        try:
            self._a._w("INSERT OR IGNORE INTO org_cleanup_pending VALUES (?)", (workspace_id,))
        except AccessError:
            if not had:
                with self._lock:
                    self._mem.discard(workspace_id)  # nothing changed, so nothing is owed
            raise Refused("unavailable") from None

    def drop(self, workspace_id: str) -> bool:
        """Run all three drops (one failing never skips the others). True when everything is gone; otherwise the workspace
        stays pending and the next ``retry`` runs it again."""
        done = True
        for what, fn in (
            ("secrets", org_secrets(self._a).drop_workspace),
            ("skills", org_skills(self._a).drop_workspace),
            ("tool servers", org_mcp(self._a).drop_workspace),
        ):
            try:
                fn(workspace_id)
            except Exception:  # noqa: BLE001 - recorded as pending below
                done = False
                _log.exception("dropping the company %s of a workspace failed; it is retried", what)
        if done:
            with self._lock:
                self._mem.discard(workspace_id)
            try:
                self._a._w("DELETE FROM org_cleanup_pending WHERE workspace_id=?", (workspace_id,))
            except AccessError:
                with self._lock:
                    self._mem.add(workspace_id)  # still marked on disk: try the clearing again at the next tick
            return workspace_id not in self._mem
        with self._lock:
            self._mem.add(workspace_id)
        try:
            self._a._w("INSERT OR IGNORE INTO org_cleanup_pending VALUES (?)", (workspace_id,))
        except AccessError:
            pass  # the in-memory mark holds for this process
        return False

    def retry(self) -> None:
        """Timer tick and startup: run every pending drop again."""
        for ws in sorted(self.pending()):
            self.drop(ws)


def org_cleanup(a: Access) -> OrgCleanup:
    s = getattr(a, "_org_cleanup", None)
    if s is None:
        s = a._org_cleanup = OrgCleanup(a)  # type: ignore[attr-defined]
    return s
