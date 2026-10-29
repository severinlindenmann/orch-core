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
from ..fsutil import open_private_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace (
  workspace_id TEXT PRIMARY KEY, owner_person_id TEXT NOT NULL, wsk_pub TEXT NOT NULL, pk_pub TEXT NOT NULL,
  card TEXT NOT NULL, binding_state TEXT NOT NULL, binding_obj TEXT
);
CREATE TABLE IF NOT EXISTS owner_alias (
  workspace_id TEXT PRIMARY KEY, user_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS op_counts (
  ws TEXT NOT NULL, op TEXT NOT NULL, outcome TEXT NOT NULL CHECK (outcome IN ('accepted','refused')),
  count INTEGER NOT NULL, PRIMARY KEY (ws, op, outcome)
)
"""


class Registry:
    def __init__(self, path: str = ":memory:") -> None:
        self.lock = threading.RLock()
        self._db = open_private_db(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        if "binding_obj" not in {r[1] for r in self._db.execute("PRAGMA table_info(workspace)")}:
            self._db.execute("ALTER TABLE workspace ADD COLUMN binding_obj TEXT")  # registry from before D11
        columns = {r[1] for r in self._db.execute("PRAGMA table_info(workspace)")}
        for name, kind in (
            ("profile", "TEXT"),
            ("version", "TEXT"),
            ("commit", "TEXT"),
            ("host", "TEXT"),
            ("name", "TEXT"),
            ("own_name", "TEXT"),
            ("last_seen_ms", "INTEGER"),
            ("peer_addr", "TEXT"),
        ):
            if name not in columns:
                self._db.execute(f'ALTER TABLE workspace ADD COLUMN "{name}" {kind}')
        self._db.commit()

    def close(self) -> None:
        with self.lock:
            self._db.close()

    def owner_alias(self, ws: str) -> str | None:
        with self.lock:
            r = self._db.execute("SELECT user_id FROM owner_alias WHERE workspace_id=?", (ws,)).fetchone()
        return None if r is None else r[0]

    def count_op(self, ws: str, op: str, accepted: bool) -> None:
        """Aggregate counter (one row per ws/op/outcome, never per op) read by ``orch-hub inspect`` (dev). The caller
        passes only a pinned ``ws`` (or ``""`` for unjoined links) and an op from the closed list (or ``other``), so
        the table cannot grow with attacker input. Never raises into the link."""
        try:
            with self.lock, self._db:
                self._db.execute(
                    "INSERT INTO op_counts(ws, op, outcome, count) VALUES (?,?,?,1) "
                    "ON CONFLICT(ws, op, outcome) DO UPDATE SET count = count + 1",
                    (ws, op, "accepted" if accepted else "refused"),
                )
        except sqlite3.Error:
            pass

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

    def unbound_ms(self, ws: str) -> int | None:
        """The leave time from the signed unbound object, only while unbound."""
        with self.lock:
            row = self._db.execute(
                "SELECT binding_state, binding_obj FROM workspace WHERE workspace_id=?", (ws,)
            ).fetchone()
        if row is None or json.loads(row[0]).get("status") != "unbound" or row[1] is None:
            return None
        value = json.loads(row[1]).get("o", {}).get("unbound_ms")
        return value if type(value) is int else None

    def forget(self, ws: str) -> None:
        """Delete the workspace and owner alias, retaining operation counts."""
        with self.lock, self._db:
            self._db.execute("DELETE FROM workspace WHERE workspace_id=?", (ws,))
            self._db.execute("DELETE FROM owner_alias WHERE workspace_id=?", (ws,))

    def mode(self, ws: str) -> str | None:
        """The mode of the live binding; None unless bound."""
        st = self.binding_state(ws)
        return st["binding"]["mode"] if st and st["status"] == "bound" else None

    def last_mode(self, ws: str) -> str | None:
        """The mode of the newest binding, bound or not (None if never joined)."""
        st = self.binding_state(ws)
        b = st.get("binding") if st else None
        return b["mode"] if type(b) is dict else None

    def commit_join(
        self,
        ws: str,
        owner_person_id: str,
        wsk_pub: str,
        pk_pub: str,
        card: Mapping[str, Any],
        state: Mapping[str, Any],
        binding_obj: Mapping[str, Any] | None = None,
        owner_alias: str | None = None,
    ) -> None:
        """Pin (first come) or re-confirm the pin, store card and binding state, in one transaction.
        ``owner_alias`` (V3-D19: the hub user the admin's invite names as the owner) is exactly a function of THIS join:
        stored with the pin and binding, and the row is deleted when this join's invite carried none. The caller holds
        :attr:`lock` and has already checked the pin against :meth:`pins`."""
        with self.lock, self._db:
            r = self._row(ws)
            if r is not None and (r[0], r[1]) != (owner_person_id, wsk_pub):
                raise RuntimeError("pin conflict")  # the caller's check inside the lock makes this unreachable
            self._db.execute(
                "INSERT INTO workspace(workspace_id, owner_person_id, wsk_pub, pk_pub, card, binding_state, binding_obj) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET "
                "pk_pub=excluded.pk_pub, card=excluded.card, binding_state=excluded.binding_state, "
                "binding_obj=COALESCE(excluded.binding_obj, binding_obj)",
                (
                    ws,
                    owner_person_id,
                    wsk_pub,
                    pk_pub,
                    cj(dict(card)).decode(),
                    cj(dict(state)).decode(),
                    None if binding_obj is None else cj(dict(binding_obj)).decode(),
                ),
            )
            if state.get("status") != "bound":
                self._db.execute("UPDATE workspace SET profile=NULL WHERE workspace_id=?", (ws,))
            if self._mode_locked(ws):
                self._db.execute("UPDATE workspace SET host='', name='' WHERE workspace_id=?", (ws,))
            if owner_alias is None:
                self._db.execute("DELETE FROM owner_alias WHERE workspace_id=?", (ws,))
            else:
                self._db.execute(
                    "INSERT INTO owner_alias(workspace_id, user_id) VALUES (?,?) "
                    "ON CONFLICT(workspace_id) DO UPDATE SET user_id=excluded.user_id",
                    (ws, owner_alias),
                )

    def put_card(self, ws: str, pk_pub: str, card: Mapping[str, Any]) -> None:
        with self.lock, self._db:
            self._db.execute(
                "UPDATE workspace SET pk_pub=?, card=? WHERE workspace_id=?", (pk_pub, cj(dict(card)).decode(), ws)
            )

    def set_binding_state(
        self, ws: str, state: Mapping[str, Any], binding_obj: Mapping[str, Any] | None = None
    ) -> None:
        with self.lock, self._db:
            self._db.execute(
                "UPDATE workspace SET binding_state=?, binding_obj=COALESCE(?, binding_obj) WHERE workspace_id=?",
                (cj(dict(state)).decode(), None if binding_obj is None else cj(dict(binding_obj)).decode(), ws),
            )
            if state.get("status") != "bound":
                self._db.execute("UPDATE workspace SET profile=NULL WHERE workspace_id=?", (ws,))
            if self._mode_locked(ws):
                self._db.execute("UPDATE workspace SET host='', name='' WHERE workspace_id=?", (ws,))

    def profile(self, ws: str) -> dict[str, Any] | None:
        with self.lock:
            row = self._db.execute("SELECT profile FROM workspace WHERE workspace_id=?", (ws,)).fetchone()
        return json.loads(row[0]) if row and row[0] is not None else None

    def put_profile(self, ws: str, body: Mapping[str, Any]) -> None:
        from ..contract.phase4 import strip_text as render_text

        clean = {
            k: [render_text(v) for v in value]
            if type(value) is list
            else render_text(value)
            if type(value) is str
            else value
            for k, value in body.items()
        }
        with self.lock, self._db:
            self._db.execute("UPDATE workspace SET profile=? WHERE workspace_id=?", (cj(clean).decode(), ws))

    def binding_objects(self) -> dict[str, dict[str, Any]]:
        """``{workspace_id: the newest accepted signed hub.binding / hub.unbound}``, to re-push after a restart."""
        with self.lock:
            rows = self._db.execute("SELECT workspace_id, binding_obj FROM workspace WHERE binding_obj IS NOT NULL")
            return {ws: json.loads(o) for ws, o in rows.fetchall()}

    def put_status(self, ws: str, body: Mapping[str, str]) -> None:
        from ..contract.phase4 import render_text

        with self.lock, self._db:
            self._db.execute(
                'UPDATE workspace SET version=?, "commit"=?, host=?, name=?, own_name=? WHERE workspace_id=?',
                (
                    render_text(body["version"])[:32],
                    render_text(body["commit"])[:40],
                    "" if self._mode_locked(ws) else render_text(body["host"])[:80],
                    "" if self._mode_locked(ws) else render_text(body["name"])[:80],
                    render_text(body["name"])[:80],  # for the owner alone (own_name); never shown to anyone else
                    ws,
                ),
            )

    def _mode_locked(self, ws: str) -> bool:
        return self.last_mode(ws) == "selective"

    def seen(self, ws: str, now_ms: int, peer_addr: str) -> None:
        """Display telemetry must never fail an authenticated operation."""
        try:
            with self.lock, self._db:
                self._db.execute(
                    "UPDATE workspace SET last_seen_ms=?, peer_addr=? WHERE workspace_id=?",
                    (now_ms, peer_addr, ws),
                )
        except Exception:  # noqa: BLE001, S110  # display telemetry must never fail the real op
            pass

    def display_name(self, ws: str) -> str | None:
        """The name the workspace reported with ``status_put``: only a full-mode workspace shares it (selective keeps
        it private, like ``host``). ``None`` when there is none: callers show a friendly fallback, never an id."""
        with self.lock:
            row = self._db.execute("SELECT name FROM workspace WHERE workspace_id=?", (ws,)).fetchone()
        return row[0] if row and isinstance(row[0], str) and row[0] else None

    def own_name(self, ws: str) -> str | None:
        """The label the workspace reported, whatever its mode. Only the workspace's owner may be shown this (callers
        check ``owner_alias``); hub admins and groups get ``display_name`` only."""
        with self.lock:
            row = self._db.execute("SELECT own_name FROM workspace WHERE workspace_id=?", (ws,)).fetchone()
        return row[0] if row and isinstance(row[0], str) and row[0] else None

    def status(self, ws: str) -> dict[str, Any]:
        with self.lock:
            row = self._db.execute(
                'SELECT version, "commit", host, peer_addr, last_seen_ms FROM workspace WHERE workspace_id=?',
                (ws,),
            ).fetchone()
        version, commit, host, address, seen = row or (None, None, None, "", None)
        return {"version": version, "commit": commit, "host": host, "address": address or "", "last_seen_ms": seen}
