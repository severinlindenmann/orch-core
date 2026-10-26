"""``wk_seal`` (protocol.md §10.2, full mode only): a workspace hands the hub its epoch key ``WK_e`` sealed to ``HXK``
(v2 §5.3, purpose ``wk``, recipient = the hub id, object = the workspace id, epoch = the stated epoch).

The hub verifies that the wrap opens under ``HXK`` and is bound to THIS hub, THIS workspace and the stated epoch, then
stores the SEALED wrap, never the opened key (the opened bytes are dropped at once and never logged). State lives in
its own private SQLite file (``fsutil.open_private_db``). Per workspace the stored epochs only grow; after
``hub.unbound`` (or a switch to selective) the rows are retired: kept, never served and never counted as current.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..contract import suite as S
from ..contract.hub import Refused
from ..fsutil import open_private_db

WRAP_LEN = S.PUB_LEN + 32 + 16  # eph_pub || AES-GCM(32-byte key) with its 16-byte tag
MAX_EPOCH = 2**32 - 1
KDF_SEAL = "orch/v2/seal"
AAD_SEAL = b"orch/v2/seal-aad|"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wk_seal (
  ws TEXT NOT NULL, epoch INTEGER NOT NULL, wrap BLOB NOT NULL, stored_ms INTEGER NOT NULL,
  retired INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (ws, epoch)
)
"""


class WkSeals:
    def __init__(
        self,
        path: str,
        *,
        hub_id: str,
        hxk_pub: bytes,
        exchange: Callable[[bytes], bytes],
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self._hub, self._hxk, self._ex = hub_id, hxk_pub, exchange
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._db = open_private_db(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        with self.lock:
            self._db.close()

    def _check_wrap(self, ws: str, epoch: int, wrap: bytes) -> None:
        """Opens the wrap with HXK to prove the binding (hub, workspace, epoch); the plaintext is dropped here."""
        self._open_wrap(ws, epoch, wrap)

    def _open_wrap(self, ws: str, epoch: int, wrap: bytes) -> bytes:
        if len(wrap) != WRAP_LEN:
            raise Refused("bad_wrap")
        eph, ct = wrap[: S.PUB_LEN], wrap[S.PUB_LEN :]
        if not S.valid_public_key(eph):
            raise Refused("bad_wrap")
        try:
            ss = self._ex(eph)
            info = f"{KDF_SEAL}|wk|{self._hub}".encode("ascii")
            key = HKDF(hashes.SHA256(), 32, eph + self._hxk, info).derive(ss)
            aad = AAD_SEAL + bytes([S.SUITE_ID, 2]) + b"wk" + bytes.fromhex(ws) + epoch.to_bytes(4, "big")
            return AESGCM(key).decrypt(bytes(12), ct, aad)
        except InvalidTag, ValueError, Refused:
            raise Refused("bad_wrap") from None
        except Exception:  # noqa: BLE001 (any crypto failure is a bad wrap, never a crash that leaks detail)
            raise Refused("bad_wrap") from None

    def accept(self, ws: str, epoch: object, wrap: bytes) -> None:
        """Verify and store. The caller holds the registry lock and has checked the binding is full and bound."""
        if type(epoch) is not int or not 1 <= epoch <= MAX_EPOCH:
            raise Refused("malformed")
        self._check_wrap(ws, epoch, wrap)
        with self.lock, self._db:
            rows = self._db.execute("SELECT epoch, wrap FROM wk_seal WHERE ws=? AND retired=0", (ws,)).fetchall()
            last = max((r[0] for r in rows), default=0)
            if epoch < last:
                raise Refused("bad_epoch")
            if epoch == last:
                held = next(r[1] for r in rows if r[0] == epoch)
                if bytes(held) != wrap:
                    raise Refused("bad_wrap")  # a different wrap for an epoch already sealed
                return  # identical bytes: idempotent
            self._db.execute(
                "INSERT INTO wk_seal(ws, epoch, wrap, stored_ms, retired) VALUES (?,?,?,?,0) "
                "ON CONFLICT(ws, epoch) DO UPDATE SET wrap=excluded.wrap, stored_ms=excluded.stored_ms, retired=0",
                (ws, epoch, wrap, self._now()),
            )

    def retire(self, ws: str) -> None:
        """The workspace left (or is no longer full): its stored wraps stop counting."""
        try:
            with self.lock, self._db:
                self._db.execute("UPDATE wk_seal SET retired=1 WHERE ws=?", (ws,))
        except sqlite3.Error:
            raise Refused("internal") from None

    def live_workspaces(self) -> list[str]:
        """Workspaces that still hold a live (not retired) wrap: the startup reconcile checks them against the registry."""
        with self.lock:
            return [r[0] for r in self._db.execute("SELECT DISTINCT ws FROM wk_seal WHERE retired=0 ORDER BY ws")]

    def epochs(self, ws: str) -> list[int]:
        with self.lock:
            rows = self._db.execute("SELECT epoch FROM wk_seal WHERE ws=? AND retired=0 ORDER BY epoch", (ws,))
            return [r[0] for r in rows.fetchall()]

    def open_current(self, ws: str) -> tuple[int, bytearray]:
        """The newest live epoch and its ``WK_e``, opened with HXK for ONE in-memory use (the custodial engine's
        build). The caller zeroises the bytearray when done; the key is never stored or logged. ``Refused`` if the
        workspace has no live wrap."""
        with self.lock:
            row = self._db.execute(
                "SELECT epoch, wrap FROM wk_seal WHERE ws=? AND retired=0 ORDER BY epoch DESC LIMIT 1", (ws,)
            ).fetchone()
        if row is None:
            raise Refused("unavailable")
        return row[0], bytearray(self._open_wrap(ws, row[0], bytes(row[1])))

    def open_for_view(self, ws: str, epoch: int) -> bytearray:
        """``WK_e`` of one LIVE epoch (not retired), opened for ONE in-memory use like :meth:`open_current`. For the
        view (``view.service.Keyring``) ONLY: the stored view pages stay sealed under the epoch they arrived in, and a
        read opens each epoch it needs this way. The caller wipes the returned bytearray."""
        with self.lock:
            row = self._db.execute(
                "SELECT wrap FROM wk_seal WHERE ws=? AND epoch=? AND retired=0", (ws, epoch)
            ).fetchone()
        if row is None:
            raise Refused("unavailable")
        return bytearray(self._open_wrap(ws, epoch, bytes(row[0])))
