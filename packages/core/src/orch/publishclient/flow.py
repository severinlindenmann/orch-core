"""The calls the Publish add-on makes to orch-publish, one function each (design-publish-addon §2.5).

Thin on purpose: a :class:`~orch.publishclient.client.PublishClient` does the signing, the retries and the error
codes; these functions name the routes and shape the bodies. Part uploads own their resume and retry loop.
Nothing here logs, and no function takes or returns a share token: the server only ever sees ``token_hash``
(sha256 hex of the token string).

A share goes up as: ``PUT shares/<id>`` (the manifest), one ``PUT shares/<id>/files/<name>`` per file, then
``POST shares/<id>/commit``. Every step is safe to send again (the server answers ``unchanged``), so a lost answer
is simply retried with the same ``meta`` bytes.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from orch.hubclient.errors import HubRefused

from .client import PublishClient, PublishError, share_target

__all__ = [
    "commit",
    "extend",
    "list_shares",
    "meta_bytes",
    "put_app_file",
    "put_app_meta",
    "put_file",
    "put_file_parts",
    "put_meta",
    "revoke",
    "set_password",
    "token_hash",
]


def token_hash(token: str) -> str:
    """The server's contract (orch-publish ``pubsrv``): sha256 hex of the token **string**."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def meta_bytes(
    *,
    ticket: str,
    kind: str,
    expires_ms: int,
    view_limit: int,
    token_hash_hex: str,
    files: Iterable[Mapping[str, Any]],
    label: str | None = None,
    by_kind: str | None = None,
    by_name: str | None = None,
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
    if label is not None:  # display members (the company list): asserted text only, omitted when not given
        doc["label"] = label
    if by_kind is not None:
        doc["by_kind"] = by_kind
    if by_name is not None:
        doc["by_name"] = by_name
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


def put_meta(client: PublishClient, workspace_id: str, share_id: str, meta: bytes) -> dict[str, Any]:
    got = client.request("PUT", share_target(workspace_id, "shares", share_id), meta).json()
    return got if isinstance(got, dict) else {}


def put_file(client: PublishClient, workspace_id: str, share_id: str, name: str, data: bytes) -> dict[str, Any]:
    got = client.request("PUT", share_target(workspace_id, "shares", share_id, "files", name), data).json()
    return got if isinstance(got, dict) else {}


def put_file_parts(
    client: PublishClient,
    ws: str,
    sid: str,
    name: str,
    data: bytes,
    *,
    part_size: int,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Append bounded parts, always resuming at Publish's authoritative byte count.

    Own retries here so each failed attempt counts once and each retry is signed anew.
    Only a completed response (``done``) finishes the upload, including an empty file.
    """
    if type(part_size) is not int or part_size <= 0:
        raise ValueError("part_size must be a positive integer")
    offset = failures = 0
    restarted = False
    conflict_offset = None
    # A bound on the whole loop: no answer pattern (no progress, offsets bouncing back) can keep it running forever.
    budget = 3 * (len(data) // part_size + 1) + 20

    def received(value: Any) -> int:
        if type(value) is not int or not 0 <= value <= len(data):
            raise PublishError("bad_reply", method="PUT")
        return value

    while True:
        budget -= 1
        if budget < 0:
            raise PublishError("bad_reply", method="PUT")
        target = share_target(ws, "shares", sid, "parts", str(offset), *name.split("/"))
        try:
            reply = client.request("PUT", target, data[offset : offset + part_size], idempotent=False)
            got = reply.json()
        except (PublishError, HubRefused) as exc:
            previous_conflict = conflict_offset
            conflict_offset = None
            if isinstance(exc, PublishError) and exc.code in {"part_out_of_order", "part_conflict"}:
                if exc.code == "part_conflict":
                    if previous_conflict == offset:
                        raise
                    conflict_offset = offset
                offset = received(exc.received)
                failures = 0
                continue
            if isinstance(exc, PublishError) and exc.code == "content_mismatch":
                if restarted:
                    raise
                if received(exc.received) != 0:
                    raise PublishError("bad_reply", exc.status, method="PUT") from None
                restarted = True
                offset = failures = 0
                continue
            retryable = (
                exc.code in {"unavailable", "hub_unreachable"}
                if isinstance(exc, HubRefused)
                else exc.code in {"part_busy", "rate_limited", "network"} or exc.status in {502, 503, 504}
            )
            if not retryable:
                raise
            failures += 1
            if failures >= 5:
                raise
            sleep(min(2 ** (failures - 1), 16))
            continue
        if (
            not isinstance(got, dict)
            or type(got.get("done")) is not bool
            or got.get("result") not in {"stored", "unchanged"}
        ):
            raise PublishError("bad_reply", reply.status, method="PUT")
        before, offset = offset, received(got.get("received"))
        if got["done"] and offset != len(data):
            raise PublishError("bad_reply", reply.status, method="PUT")
        if not got["done"] and offset <= before and before < len(data):  # an answer that does not move forward
            failures += 1
            if failures >= 5:
                raise PublishError("bad_reply", reply.status, method="PUT")
            sleep(min(2 ** (failures - 1), 16))
            continue
        failures = 0
        conflict_offset = None
        if on_progress is not None:
            on_progress(offset, len(data))
        if got["done"]:
            return got


def put_app_meta(client: PublishClient, workspace_id: str, app_id: str, version: str, meta: bytes) -> dict[str, Any]:
    """``PUT apps/<id>/versions/<n>`` (#1455): the stored meta of one app version. Stores nothing live: only a commit
    carrying the owner's approval serves it."""
    got = client.request("PUT", share_target(workspace_id, "apps", app_id, "versions", version), meta).json()
    return got if isinstance(got, dict) else {}


def put_app_file(
    client: PublishClient, workspace_id: str, app_id: str, version: str, path: str, data: bytes
) -> dict[str, Any]:
    parts = ("apps", app_id, "versions", version, "files", *path.split("/"))
    got = client.request("PUT", share_target(workspace_id, *parts), data).json()
    return got if isinstance(got, dict) else {}


def commit(client: PublishClient, workspace_id: str, share_id: str) -> dict[str, Any]:
    got = client.request("POST", share_target(workspace_id, "shares", share_id, "commit"), b"", idempotent=True).json()
    return got if isinstance(got, dict) else {}


def revoke(client: PublishClient, workspace_id: str, share_id: str) -> dict[str, Any]:
    got = client.request("DELETE", share_target(workspace_id, "shares", share_id)).json()
    return got if isinstance(got, dict) else {}


def extend(client: PublishClient, workspace_id: str, share_id: str, days: int) -> dict[str, Any]:
    """``POST shares/<id>/extend {days}``: the link ends ``days`` (1..30) from the server's now. Not resent after a lost
    answer (the second try would be refused as not longer): the caller lists the shares to see what happened."""
    body = json.dumps({"days": days}, separators=(",", ":")).encode()
    got = client.request("POST", share_target(workspace_id, "shares", share_id, "extend"), body).json()
    return got if isinstance(got, dict) else {}


def set_password(client: PublishClient, workspace_id: str, share_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """``POST shares/<id>/password {pw: record}``: ``record`` is the host-made C5 record, never a plain password.
    Setting the same record again is safe, so a lost answer is retried."""
    body = json.dumps({"pw": dict(record)}, sort_keys=True, separators=(",", ":")).encode()
    got = client.request(
        "POST", share_target(workspace_id, "shares", share_id, "password"), body, idempotent=True
    ).json()
    return got if isinstance(got, dict) else {}


def list_shares(client: PublishClient, workspace_id: str) -> list[dict[str, Any]]:
    got = client.request("GET", share_target(workspace_id, "shares")).json()
    rows = got.get("shares") if isinstance(got, dict) else None
    if not isinstance(rows, list):
        raise PublishError("bad_reply", 200, method="GET")
    return [r for r in rows if isinstance(r, dict)]
