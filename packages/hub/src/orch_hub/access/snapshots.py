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
from .orgmcp import org_mcp
from .orgsecrets import org_secrets
from .orgskills import org_skills
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
            mp = a.effective_market_policy(workspace_id)  # the hub-wide policy plus this workspace's add-on extras
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

    def org_skills(self, workspace_id: str, mode: str) -> dict[str, Any]:
        """The signed ``hub.org_skills`` of ONE workspace (M3 #1430): only the skills offered to it."""
        store = org_skills(self._a)  # (creates its tables: never inside the transaction below)
        return self._bundle(
            workspace_id, "hub.org_skills", "skills", "org_skills_bundle", lambda: store.offered_to(workspace_id)
        )

    def mcp_servers(self, workspace_id: str, mode: str) -> dict[str, Any]:
        """The signed ``hub.mcp_servers`` of ONE workspace (M3 #1435): only the remote servers offered to it."""
        store = org_mcp(self._a)
        return self._bundle(
            workspace_id, "hub.mcp_servers", "servers", "org_mcp_bundle", lambda: store.offered_to(workspace_id)
        )

    def org_secrets(self, workspace_id: str, mode: str) -> dict[str, Any]:
        """The signed ``hub.org_secrets`` of ONE workspace (M3 #1431): only ITS sealed wraps, of live unexpired secrets. The
        hub cannot open them. An expiry changes the content, so it gets a new version like any other change."""
        store = org_secrets(self._a)
        return self._bundle(
            workspace_id,
            "hub.org_secrets",
            "secrets",
            "org_secrets_bundle",
            lambda: store.wraps_for(workspace_id, self._now()),
        )

    def _bundle(
        self, workspace_id: str, kind: str, field: str, table: str, items: Callable[[], list[dict[str, Any]]]
    ) -> dict[str, Any]:
        """A per-workspace bundle versioned like the snapshot: unchanged content returns the stored bytes, changed content
        (an item added, edited, withdrawn) gets ``version + 1``, so a host never accepts an older bundle. Any failure
        refuses with ``unavailable``. ``table`` is one of two constants, never input."""
        try:
            signer = self._keys.op_signer
            op_cert_id = self._keys.op_cert()["o"]["cert_id"]
            with self._a.tx() as db:
                content = items()
                fp = hashlib.sha256(cj({field: content, "op": op_cert_id, "dev": dev_of(self._keys)})).hexdigest()
                row = db.execute(
                    f"SELECT version, fingerprint, signed FROM {table} WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()
                if row is not None and row[1] == fp:
                    return H.parse(row[2].encode())
                o = {
                    "v": 2,
                    "suite": 2,
                    "kind": kind,
                    "hv": H.HV,
                    "hub_id": self._keys.hub_id,
                    "op_cert_id": op_cert_id,
                    "workspace_id": workspace_id,
                    "version": (row[0] if row else 0) + 1,
                    "issued_ms": self._now(),
                    field: content,
                    "dev": dev_of(self._keys),
                }
                signed = signer.sign_object(o)
                db.execute(
                    f"INSERT INTO {table} VALUES (?,?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET "
                    "version=excluded.version, fingerprint=excluded.fingerprint, signed=excluded.signed",
                    (workspace_id, o["version"], fp, cj(signed).decode()),
                )
                return signed
        except AccessError, KeysError, sqlite3.Error, OSError, ValueError:
            raise Refused("unavailable") from None
