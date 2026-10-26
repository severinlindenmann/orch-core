"""The :class:`~orch_hub.link.ops.SnapshotProvider` of the access model: a ``hub.snapshot`` per workspace exactly as
contract §7, signed only by the typed ``HK_op`` signer.

Versioning. The newest issued snapshot of each workspace is persisted with a fingerprint of what it says (roles,
revoked channels, op cert, marketplace policy hash). A request recomputes the content; unchanged content returns the stored signed snapshot
byte for byte (hosts treat the same ``version`` with different bytes as ``snapshot_conflict``), changed content gets
``version + 1``, a fresh ``issued_ms`` and a new signature, stored in the same transaction. Versions therefore only go
up and survive restarts. Any failure (store, keys, revoked op cert) refuses with ``unavailable``.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from ..config import dev_of
from ..contract import hub as H
from ..contract.canon import cj
from ..contract.hub import Refused
from ..keys import HubKeys, KeysError
from .store import Access, AccessError


class AccessSnapshots:
    def __init__(self, access: Access, keys: HubKeys, now_ms: Callable[[], int] | None = None) -> None:
        self._a, self._keys = access, keys
        self._now = now_ms or (lambda: int(time.time() * 1000))

    def snapshot(self, workspace_id: str, mode: str) -> dict[str, Any]:
        try:
            return self._snapshot(workspace_id)
        except AccessError, KeysError, sqlite3.Error, OSError, ValueError:
            raise Refused("unavailable") from None

    def _snapshot(self, workspace_id: str) -> dict[str, Any]:
        signer = self._keys.op_signer  # refuses once HK_op is revoked
        op_cert_id = self._keys.op_cert()["o"]["cert_id"]
        a = self._a
        with a.tx() as db:  # roles, channels, the stored row and the new row are one consistent step
            roles = a.roles(workspace_id)
            revoked = a.revoked_channels()
            mp = a.market_policy()
            fp = hashlib.sha256(
                cj(
                    {
                        "roles": roles,
                        "revoked": revoked,
                        "op": op_cert_id,
                        "policy": mp["hash"],
                        "dev": dev_of(self._keys),
                    }
                )
            ).hexdigest()
            row = a.snapshot_row(workspace_id)
            if row is not None and row[1] == fp:
                return H.parse(row[2].encode())
            o = {
                "v": 2,
                "suite": 2,
                "kind": "hub.snapshot",
                "hv": H.HV,
                "hub_id": self._keys.hub_id,
                "op_cert_id": op_cert_id,
                "workspace_id": workspace_id,
                "generation": 0,
                "version": (row[0] if row else 0) + 1,
                "issued_ms": self._now(),
                "roles": roles,
                "revoked_channels": revoked,
                "market_policy": mp["policy"],
                "dev": dev_of(self._keys),
            }
            signed = signer.sign_object(o)
            db.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET "
                "version=excluded.version, fingerprint=excluded.fingerprint, signed=excluded.signed",
                (workspace_id, o["version"], fp, cj(signed).decode()),
            )
            return signed
