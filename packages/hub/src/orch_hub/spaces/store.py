"""SQLite store of the shared spaces (``<state>/spaces.db``, private via fsutil, protocol.md §10.4).

Holds, per space and epoch, the member list, the signed ``hub.space`` record, ``SK_e`` (the hub holds every key,
option B; the file is 0600 like the hub's key files) and one signed ``hub.space_wrap`` per member; per space the items
(sealed bytes, as put) with their ``seq``; and the ``space.accept`` objects selective workspaces gave. No clock, no
policy: :mod:`orch_hub.spaces.service` decides, this only stores. One transaction per method.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from typing import Any

from ..contract.canon import cj
from ..fsutil import open_private_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS space (
  space_id TEXT PRIMARY KEY, name TEXT NOT NULL, epoch INTEGER NOT NULL, next_seq INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS space_epoch (
  space_id TEXT NOT NULL, epoch INTEGER NOT NULL, members TEXT NOT NULL, sk BLOB NOT NULL, record TEXT NOT NULL,
  PRIMARY KEY (space_id, epoch)
);
CREATE TABLE IF NOT EXISTS space_wrap (
  space_id TEXT NOT NULL, epoch INTEGER NOT NULL, workspace_id TEXT NOT NULL, wrap TEXT NOT NULL,
  PRIMARY KEY (space_id, epoch, workspace_id)
);
CREATE TABLE IF NOT EXISTS space_item (
  space_id TEXT NOT NULL, item_id TEXT NOT NULL, seq INTEGER NOT NULL, epoch INTEGER NOT NULL,
  workspace_id TEXT NOT NULL, digest TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, sealed TEXT NOT NULL,
  PRIMARY KEY (space_id, item_id)
);
CREATE TABLE IF NOT EXISTS space_mode (
  space_id TEXT NOT NULL, workspace_id TEXT NOT NULL, mode TEXT NOT NULL CHECK (mode IN ('reads','edits')),
  PRIMARY KEY (space_id, workspace_id)
);
CREATE TABLE IF NOT EXISTS space_archive (space_id TEXT PRIMARY KEY, archived_ms INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS space_item_seq ON space_item (space_id, seq);
CREATE TABLE IF NOT EXISTS space_accept (
  space_id TEXT NOT NULL, workspace_id TEXT NOT NULL, accept TEXT NOT NULL, PRIMARY KEY (space_id, workspace_id)
);
"""


class SpaceStore:
    def __init__(self, path: str = ":memory:") -> None:
        self.lock = threading.RLock()
        self._db = open_private_db(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self.lock:
            self._db.close()

    # --- spaces and epochs ---------------------------------------------------------------------------------------

    def space_ids(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self._db.execute("SELECT space_id FROM space ORDER BY space_id")]

    # --- the admin's per-member mode and the archive flag (M3, #1444 #1447): hub-side policy, not part of any signed record ---

    def mode(self, space_id: str, ws: str) -> str:
        """``edits`` (the default: puts as before) or ``reads`` (the hub refuses this workspace's puts and revokes)."""
        with self.lock:
            r = self._db.execute(
                "SELECT mode FROM space_mode WHERE space_id=? AND workspace_id=?", (space_id, ws)
            ).fetchone()
        return "edits" if r is None else r[0]

    def modes(self, space_id: str) -> dict[str, str]:
        with self.lock:
            return dict(
                self._db.execute("SELECT workspace_id, mode FROM space_mode WHERE space_id=?", (space_id,)).fetchall()
            )

    def set_mode(self, space_id: str, ws: str, mode: str) -> None:
        with self.lock, self._db:
            if mode == "edits":
                self._db.execute("DELETE FROM space_mode WHERE space_id=? AND workspace_id=?", (space_id, ws))
            else:
                self._db.execute(
                    "INSERT INTO space_mode VALUES (?,?,?) ON CONFLICT(space_id, workspace_id) DO UPDATE SET mode=excluded.mode",
                    (space_id, ws, mode),
                )

    def drop_mode(self, space_id: str, ws: str) -> None:
        """A member left (removed, unbound): its mode goes with it, so a later return starts from the default."""
        with self.lock, self._db:
            self._db.execute("DELETE FROM space_mode WHERE space_id=? AND workspace_id=?", (space_id, ws))

    def archived_ms(self, space_id: str) -> int | None:
        with self.lock:
            r = self._db.execute("SELECT archived_ms FROM space_archive WHERE space_id=?", (space_id,)).fetchone()
        return None if r is None else r[0]

    def set_archived(self, space_id: str, ms: int | None) -> None:
        with self.lock, self._db:
            if ms is None:
                self._db.execute("DELETE FROM space_archive WHERE space_id=?", (space_id,))
            else:
                self._db.execute("INSERT OR REPLACE INTO space_archive VALUES (?,?)", (space_id, ms))

    def exists(self, space_id: str) -> bool:
        with self.lock:
            return self._db.execute("SELECT 1 FROM space WHERE space_id=?", (space_id,)).fetchone() is not None

    def name(self, space_id: str) -> str | None:
        with self.lock:
            r = self._db.execute("SELECT name FROM space WHERE space_id=?", (space_id,)).fetchone()
        return None if r is None else r[0]

    def current_epoch(self, space_id: str) -> int | None:
        with self.lock:
            r = self._db.execute("SELECT epoch FROM space WHERE space_id=?", (space_id,)).fetchone()
        return None if r is None else r[0]

    def add_epoch(
        self,
        space_id: str,
        epoch: int,
        members: list[str],
        sk: bytes,
        record: Mapping[str, Any],
        wraps: Mapping[str, Mapping[str, Any]],
        *,
        name: str | None = None,
    ) -> None:
        """One transaction: the new epoch (or the space itself when ``name`` is given for epoch 1), its key, its record
        and every member's wrap. Epochs only go up by one."""
        with self.lock, self._db:
            cur = self.current_epoch(space_id)
            if name is not None:
                if cur is not None or epoch != 1:
                    raise ValueError("space exists")
                self._db.execute("INSERT INTO space(space_id, name, epoch) VALUES (?,?,?)", (space_id, name, 1))
            else:
                if cur is None or epoch != cur + 1:
                    raise ValueError("epoch must rise by one")
                self._db.execute("UPDATE space SET epoch=? WHERE space_id=?", (epoch, space_id))
            self._db.execute(
                "INSERT INTO space_epoch VALUES (?,?,?,?,?)",
                (space_id, epoch, json.dumps(members), sk, cj(dict(record)).decode()),
            )
            for ws, w in wraps.items():
                self._db.execute("INSERT INTO space_wrap VALUES (?,?,?,?)", (space_id, epoch, ws, cj(dict(w)).decode()))

    def delete_space(self, space_id: str) -> None:
        """The last member left: the space, its keys, wraps and items go (what was read stays read)."""
        with self.lock, self._db:
            for t in (
                "space",
                "space_epoch",
                "space_wrap",
                "space_item",
                "space_accept",
                "space_mode",
                "space_archive",
            ):
                self._db.execute(f"DELETE FROM {t} WHERE space_id=?", (space_id,))

    def epoch_row(self, space_id: str, epoch: int) -> tuple[list[str], bytes, dict[str, Any]] | None:
        with self.lock:
            r = self._db.execute(
                "SELECT members, sk, record FROM space_epoch WHERE space_id=? AND epoch=?", (space_id, epoch)
            ).fetchone()
        return None if r is None else (json.loads(r[0]), bytes(r[1]), json.loads(r[2]))

    def history(self, space_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self._db.execute(
                "SELECT epoch, members FROM space_epoch WHERE space_id=? ORDER BY epoch", (space_id,)
            ).fetchall()
        return [{"epoch": e, "members": json.loads(m)} for e, m in rows]

    def wrap(self, space_id: str, epoch: int, ws: str) -> dict[str, Any] | None:
        with self.lock:
            r = self._db.execute(
                "SELECT wrap FROM space_wrap WHERE space_id=? AND epoch=? AND workspace_id=?", (space_id, epoch, ws)
            ).fetchone()
        return None if r is None else json.loads(r[0])

    def spaces_of(self, ws: str) -> list[str]:
        """Spaces whose CURRENT epoch lists ``ws``."""
        out = []
        for sid in self.space_ids():
            e = self.current_epoch(sid)
            row = None if e is None else self.epoch_row(sid, e)
            if row is not None and ws in row[0]:
                out.append(sid)
        return out

    # --- items ----------------------------------------------------------------------------------------------------

    def items(self, space_id: str) -> dict[str, dict[str, Any]]:
        with self.lock:
            rows = self._db.execute(
                "SELECT item_id, seq, epoch, workspace_id, digest, revoked FROM space_item WHERE space_id=?",
                (space_id,),
            ).fetchall()
        return {i: {"seq": s, "epoch": e, "ws": w, "digest": d, "revoked": bool(r)} for i, s, e, w, d, r in rows}

    def next_seq(self, space_id: str) -> int:
        with self.lock:
            r = self._db.execute("SELECT next_seq FROM space WHERE space_id=?", (space_id,)).fetchone()
        return 1 if r is None else r[0]

    def sealed(self, space_id: str, item_id: str) -> str | None:
        with self.lock:
            r = self._db.execute(
                "SELECT sealed FROM space_item WHERE space_id=? AND item_id=?", (space_id, item_id)
            ).fetchone()
        return None if r is None else r[0]

    def put_item(self, space_id: str, item_id: str, row: Mapping[str, Any], sealed: str, next_seq: int) -> None:
        with self.lock, self._db:
            self._db.execute(
                "INSERT INTO space_item VALUES (?,?,?,?,?,?,0,?)",
                (space_id, item_id, row["seq"], row["epoch"], row["ws"], row["digest"], sealed),
            )
            self._db.execute("UPDATE space SET next_seq=? WHERE space_id=?", (next_seq, space_id))

    def revoke_item(self, space_id: str, item_id: str, seq: int, next_seq: int) -> None:
        """The item keeps its row (the id stays taken, so the same bytes put again stay revoked) but loses its bytes."""
        with self.lock, self._db:
            self._db.execute(
                "UPDATE space_item SET revoked=1, seq=?, sealed='' WHERE space_id=? AND item_id=?",
                (seq, space_id, item_id),
            )
            self._db.execute("UPDATE space SET next_seq=? WHERE space_id=?", (next_seq, space_id))

    # --- accepts --------------------------------------------------------------------------------------------------

    def put_accept(self, space_id: str, ws: str, signed: Mapping[str, Any]) -> None:
        with self.lock, self._db:
            self._db.execute(
                "INSERT INTO space_accept VALUES (?,?,?) ON CONFLICT(space_id, workspace_id) "
                "DO UPDATE SET accept=excluded.accept",
                (space_id, ws, cj(dict(signed)).decode()),
            )

    def accept(self, space_id: str, ws: str) -> dict[str, Any] | None:
        with self.lock:
            r = self._db.execute(
                "SELECT accept FROM space_accept WHERE space_id=? AND workspace_id=?", (space_id, ws)
            ).fetchone()
        return None if r is None else json.loads(r[0])

    def drop_accept(self, space_id: str, ws: str) -> None:
        with self.lock, self._db:
            self._db.execute("DELETE FROM space_accept WHERE space_id=? AND workspace_id=?", (space_id, ws))
