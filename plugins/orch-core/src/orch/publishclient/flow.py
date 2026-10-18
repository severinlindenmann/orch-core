"""The calls the Publish add-on makes to orch-publish, one function each (design-publish-addon §2.5).

Thin on purpose: a :class:`~orch.publishclient.client.PublishClient` does the signing, the retries and the error
codes; these functions only name the routes and shape the bodies. Nothing here logs, and no function takes or returns
a share token: the server only ever sees ``token_hash`` (sha256 hex of the token string).

A share goes up as: ``PUT shares/<id>`` (the manifest), one ``PUT shares/<id>/files/<name>`` per file, then
``POST shares/<id>/commit``. Every step is safe to send again (the server answers ``unchanged``), so a lost answer
is simply retried with the same ``meta`` bytes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from .client import PublishClient, PublishError, share_target

__all__ = ["commit", "list_shares", "meta_bytes", "put_file", "put_meta", "register", "revoke", "token_hash"]


def token_hash(token: str) -> str:
    """The server's contract (orch-publish ``pubsrv``): sha256 hex of the token **string**."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def meta_bytes(
    *, ticket: str, kind: str, expires_ms: int, view_limit: int, token_hash_hex: str, files: Iterable[Mapping[str, Any]]
) -> bytes:
    """The manifest body, byte-stable: the same arguments always give the same bytes (a re-PUT is ``unchanged``)."""
    doc = {
        "ticket": ticket,
        "kind": kind,
        "expires_ms": expires_ms,
        "view_limit": view_limit,
        "token_hash": token_hash_hex,
        "files": [{"path": f["name"], "size": f["size"], "sha256": f["sha256"]} for f in files],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


def register(
    client: PublishClient, workspace_id: str, delegation: Mapping[str, Any], owner_pk_b64u: str
) -> dict[str, Any]:
    """``POST register {delegation, owner_pk}`` (design §2.6b): binds the workspace, or confirms it (``unchanged``).
    Exactly these two keys, copied from the genesis. A 401 ``unknown_workspace`` means the server's admin has not
    allowed this workspace yet; 409 ``delegation_changed`` that another delegation is bound."""
    body = json.dumps(
        {"delegation": dict(delegation), "owner_pk": owner_pk_b64u}, sort_keys=True, separators=(",", ":")
    ).encode()
    got = client.request("POST", share_target(workspace_id, "register"), body, idempotent=True).json()
    return got if isinstance(got, dict) else {}


def put_meta(client: PublishClient, workspace_id: str, share_id: str, meta: bytes) -> dict[str, Any]:
    got = client.request("PUT", share_target(workspace_id, "shares", share_id), meta).json()
    return got if isinstance(got, dict) else {}


def put_file(client: PublishClient, workspace_id: str, share_id: str, name: str, data: bytes) -> dict[str, Any]:
    got = client.request("PUT", share_target(workspace_id, "shares", share_id, "files", name), data).json()
    return got if isinstance(got, dict) else {}


def commit(client: PublishClient, workspace_id: str, share_id: str) -> dict[str, Any]:
    got = client.request("POST", share_target(workspace_id, "shares", share_id, "commit"), b"", idempotent=True).json()
    return got if isinstance(got, dict) else {}


def revoke(client: PublishClient, workspace_id: str, share_id: str) -> dict[str, Any]:
    got = client.request("DELETE", share_target(workspace_id, "shares", share_id)).json()
    return got if isinstance(got, dict) else {}


def list_shares(client: PublishClient, workspace_id: str) -> list[dict[str, Any]]:
    got = client.request("GET", share_target(workspace_id, "shares")).json()
    rows = got.get("shares") if isinstance(got, dict) else None
    if not isinstance(rows, list):
        raise PublishError("bad_reply", 200, method="GET")
    return [r for r in rows if isinstance(r, dict)]
