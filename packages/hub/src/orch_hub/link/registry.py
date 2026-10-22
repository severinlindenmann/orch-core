"""Workspace registry of the link server: per ``workspace_id`` the pinned owner and ``WSK`` (first come, §9.5),
the owner's PK, the held v2 card and the binding store state (§9.4). SQLite (a file, or ``:memory:``); every
read-modify-write happens under :attr:`Registry.lock`, so a check and its write cannot be separated.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from typing import Any

from ..contract.canon import cj

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace (
  workspace_id TEXT PRIMARY KEY, owner_person_id TEXT NOT NULL, wsk_pub TEXT NOT NULL, pk_pub TEXT NOT NULL,
  card TEXT NOT NULL, binding_state TEXT NOT NULL
)
"""


class Registry:
    def __init__(self, path: str = ":memory:") -> None:
        self.lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self.lock:
            self._db.close()

    def _row(self, ws: str) -> sqlite3.Row | tuple | None:
        return self._db.execute(
            "SELECT owner_person_id, wsk_pub, pk_pub, card, binding_state FROM workspace WHERE workspace_id=?", (ws,)
        ).fetchone()

    def pins(self) -> dict[str, dict[str, str]]:
        """``{workspace_id: {owner_person_id, wsk_pub}}``, the shape ``check_hub_join`` takes."""
        with self.lock:
            rows = self._db.execute("SELECT workspace_id, owner_person_id, wsk_pub FROM workspace").fetchall()
        return {ws: {"owner_person_id": o, "wsk_pub": w} for ws, o, w in rows}

    def pin(self, ws: str) -> dict[str, str] | None:
        with self.lock:
            r = self._row(ws)
        return None if r is None else {"owner_person_id": r[0], "wsk_pub": r[1], "pk_pub": r[2]}

    def card(self, ws: str) -> dict[str, Any] | None:
        with self.lock:
            r = self._row(ws)
        return None if r is None else json.loads(r[3])

    def binding_state(self, ws: str) -> dict[str, Any] | None:
        """``{"binding_seq", "status", "binding"}`` or None (never joined)."""
        with self.lock:
            r = self._row(ws)
        return None if r is None else json.loads(r[4])

    def mode(self, ws: str) -> str | None:
        """The mode of the live binding; None unless bound."""
        st = self.binding_state(ws)
        return st["binding"]["mode"] if st and st["status"] == "bound" else None

    def commit_join(
        self,
        ws: str,
        owner_person_id: str,
        wsk_pub: str,
        pk_pub: str,
        card: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> None:
        """Pin (first come) or re-confirm the pin, store card and binding state, in one transaction. The caller holds
        :attr:`lock` and has already checked the pin against :meth:`pins`."""
        with self.lock, self._db:
            r = self._row(ws)
            if r is not None and (r[0], r[1]) != (owner_person_id, wsk_pub):
                raise RuntimeError("pin conflict")  # the caller's check inside the lock makes this unreachable
            self._db.execute(
                "INSERT INTO workspace VALUES (?,?,?,?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET "
                "pk_pub=excluded.pk_pub, card=excluded.card, binding_state=excluded.binding_state",
                (ws, owner_person_id, wsk_pub, pk_pub, cj(dict(card)).decode(), cj(dict(state)).decode()),
            )

    def put_card(self, ws: str, pk_pub: str, card: Mapping[str, Any]) -> None:
        with self.lock, self._db:
            self._db.execute(
                "UPDATE workspace SET pk_pub=?, card=? WHERE workspace_id=?", (pk_pub, cj(dict(card)).decode(), ws)
            )

    def set_binding_state(self, ws: str, state: Mapping[str, Any]) -> None:
        with self.lock, self._db:
            self._db.execute(
                "UPDATE workspace SET binding_state=? WHERE workspace_id=?", (cj(dict(state)).decode(), ws)
            )
