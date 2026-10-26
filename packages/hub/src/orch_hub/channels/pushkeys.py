"""The persistent push-key store (H1, protocol §6.4): ``channel_id -> push_pub`` plus the ``issued_ms`` of the
``hub.channel_push`` that named it, in ``channels.db`` (table ``push_keys``). A mapping for :class:`PushNotifier`
(``get`` / ``[]=`` / ``pop``); :meth:`held_issued_ms` is what ``verify_channel_push`` compares a new registration with,
so an old, captured registration cannot be replayed back after a newer one."""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping

from ..contract.canon import b64u, unb64u
from .store import ChannelsDB


class PushKeyStore(MutableMapping[str, bytes]):
    def __init__(self, db: ChannelsDB) -> None:
        self._db = db

    def __getitem__(self, channel_id: str) -> bytes:
        rows = self._db.q("SELECT push_pub FROM push_keys WHERE channel_id=?", (channel_id,))
        if not rows:
            raise KeyError(channel_id)
        return unb64u(rows[0][0], 65)

    def __setitem__(self, channel_id: str, push_pub: bytes) -> None:
        """Replace the key, keeping the held ``issued_ms`` (0 for a new row). The web API uses :meth:`put`."""
        with self._db.tx() as db:
            row = db.execute("SELECT issued_ms FROM push_keys WHERE channel_id=?", (channel_id,)).fetchone()
            db.execute(
                "INSERT OR REPLACE INTO push_keys(channel_id, push_pub, issued_ms) VALUES (?,?,?)",
                (channel_id, b64u(push_pub), row[0] if row else 0),
            )

    def put(self, channel_id: str, push_pub: bytes, issued_ms: int) -> bool:
        """Store the key only if ``issued_ms`` is newer than the held one (one transaction); False = not stored."""
        with self._db.tx() as db:
            row = db.execute("SELECT issued_ms FROM push_keys WHERE channel_id=?", (channel_id,)).fetchone()
            if row is not None and issued_ms <= row[0]:
                return False
            db.execute(
                "INSERT OR REPLACE INTO push_keys(channel_id, push_pub, issued_ms) VALUES (?,?,?)",
                (channel_id, b64u(push_pub), issued_ms),
            )
            return True

    def held_issued_ms(self, channel_id: str) -> int | None:
        rows = self._db.q("SELECT issued_ms FROM push_keys WHERE channel_id=?", (channel_id,))
        return rows[0][0] if rows else None

    def __delitem__(self, channel_id: str) -> None:
        if self._db.w("DELETE FROM push_keys WHERE channel_id=?", (channel_id,)) != 1:
            raise KeyError(channel_id)

    def __iter__(self) -> Iterator[str]:
        return iter([r[0] for r in self._db.q("SELECT channel_id FROM push_keys ORDER BY channel_id")])

    def __len__(self) -> int:
        return self._db.q("SELECT COUNT(*) FROM push_keys")[0][0]
