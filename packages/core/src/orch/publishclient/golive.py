"""Making an app live (#1455, HUB-PU-05; design ``state/lanes/devB-design-1455-golive.md``): the pure half.

orch-publish serves a version only after a ``commit`` (or a ``rollback`` or ``start``) that carries the owner's
``hub.publish_approval`` (hub contract 11.3): the device key's signature over ONE exact request (its ``ts``, ``nonce``,
method, host, target and body hash). The person has already signed a release note whose words
:func:`orch.publishclient.approval.app_release_text` builds from what goes live. This module is the check the signer
runs before it makes the second signature in the same click session: it rebuilds the words and the request body
itself, from the facts of the release, and refuses unless

* the words equal the note this client signed (byte for byte),
* the body equals the one rebuilt from the release (op, app id, version, ``meta_hash``, ``reach_hash``, access,
  audience),
* the target is this workspace's route for that op and app, the host is the signed publish setting's host, the hub is
  the workspace's hub,
* the clock agrees (:data:`SKEW_MS`), the ``request_hash`` is the one of this request, and the object names this device,
  this workspace and the body's access.

Nothing in the host's answer is trusted: a compromised host can only make the check fail. No I/O, no key, no clock; the
caller passes ``now_ms`` and a ``sign`` callable. Imported by the CLI and by the dashboard's signing server, so the rule
is written once.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Mapping
from typing import Any

from orch.canon.jcs import dumps as cj
from orch.crypto.encoding import b64u
from orch.crypto.labels import L
from orch.hubclient import published as pub
from orch.hubclient.errors import HubRefused

from . import approval

__all__ = [
    "KIND",
    "SKEW_MS",
    "approval_object",
    "build_approval",
    "check_commit_request",
    "commit_body",
    "commit_target",
    "request_hash_b64",
]

KIND = "hub.publish_approval"
#: how far this computer's clock may differ from the ``ts`` of the pending request the host drew
SKEW_MS = 60_000
_OPS = approval.APP_OPS


def commit_body(meta: Mapping[str, Any], op: str, app_id: str, version: int) -> bytes:
    """The canonical body of a commit, rollback or start of ``version`` whose stored meta is ``meta``."""
    if op not in _OPS:
        raise HubRefused("malformed")
    return cj(pub.release_body(op, app_id, version, meta))


def commit_target(workspace_id: str, app_id: str, op: str) -> str:
    return f"/v1/ws/{workspace_id}/apps/{app_id}/{op}"


def request_hash_b64(
    workspace_id: str, hub_id: str | None, ts_ms: int, nonce: bytes, host: str, target: str, body: bytes
) -> str:
    """What the owner's approval names: ``b64u(request_hash)`` of this POST on the hub path (``hub_id``) or the direct
    one (``None``)."""
    return b64u(pub.app_request_hash(bytes.fromhex(workspace_id), ts_ms, nonce, "POST", host, target, body, hub_id))


def approval_object(
    workspace_id: str, hub_id: str | None, device_id: str, request_hash: str, access: str
) -> dict[str, Any]:
    """The unsigned ``o`` of a ``hub.publish_approval`` (contract 11.3)."""
    return {
        "v": 2, "suite": 2, "kind": KIND, "hv": 0, "workspace_id": workspace_id, "hub_id": hub_id,
        "device_id": device_id, "request_hash": request_hash, "access": access,
    }  # fmt: skip


def check_commit_request(
    req: Mapping[str, Any],
    facts: Mapping[str, Any],
    note_text: str,
    *,
    workspace_id: str,
    hub_id: str | None,
    device_id: str,
    now_ms: int,
) -> dict[str, Any]:
    """Every check of the second signature, and the object to sign.

    **The invariant: every signed field comes from local trusted state, or is checked equal to it.** ``workspace_id``,
    ``hub_id`` and ``device_id`` are the CALLER's own (the workspace the CLI is pinned to, the hub in this computer's
    join state, this computer's key): ``hub_id`` is required, and ``None`` (no hub joined here) refuses. The host's
    ``req`` = {target, body (bytes), ts_ms, nonce (bytes), host, hub_id} may only *agree*: its ``hub_id`` must equal the
    local one, its ``host`` the host of the server in the release the person signed (``facts["server"]``), its
    ``target`` this workspace's route for that op and app, its ``body`` the one rebuilt from the release. The approval
    object ``o`` is built here from those local values and the request hash of the checked fields, never taken from the
    host. ``facts`` = {op, app_id, version, meta, previous, staged_id, ticket_key, origin, server, share};
    ``note_text`` is the text of the note this client signed. Raises :class:`HubRefused` ``malformed`` (the caller says
    only that the request could not be checked); nothing is signed unless this returns."""
    try:
        if type(hub_id) is not str or len(hub_id) != 32:
            raise ValueError("no hub joined here")
        op, app_id, version, meta = facts["op"], facts["app_id"], facts["version"], facts["meta"]
        want_words = approval.app_release_text(
            staged_id=facts["staged_id"], ticket_key=facts["ticket_key"], origin=facts["origin"], op=op,
            app_id=app_id, version=version, meta=meta, previous=facts.get("previous"),
            server=facts["server"], share=facts["share"],
        )  # fmt: skip
        if type(note_text) is not str or not hmac.compare_digest(want_words.encode(), note_text.encode()):
            raise ValueError("words")
        body = commit_body(meta, op, app_id, version)
        if req["body"] != body:
            raise ValueError("body")
        if req["target"] != commit_target(workspace_id, app_id, op):
            raise ValueError("target")
        from orch.wsproto.pubsign import PublishContext

        host = PublishContext(workspace_id, facts["server"]).host  # the server the person's words name
        if req["host"] != host:
            raise ValueError("host")
        if req["hub_id"] != hub_id:  # the host may not choose the hub the signature is bound to
            raise ValueError("hub")
        if type(req["nonce"]) is not bytes or len(req["nonce"]) != 16 or type(req["ts_ms"]) is not int:
            raise ValueError("nonce or ts")
        if abs(req["ts_ms"] - now_ms) > SKEW_MS:
            raise ValueError("clock")
        return approval_object(
            workspace_id, hub_id, device_id,
            request_hash_b64(workspace_id, hub_id, req["ts_ms"], req["nonce"], host, req["target"], body),
            meta["access"],
        )  # fmt: skip
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HubRefused("malformed") from None


def build_approval(
    o: Mapping[str, Any], sign: Callable[[bytes], bytes], device_cert: Mapping[str, Any], pk_pub: str
) -> dict[str, Any]:
    """The signed approval ``{o, sig, device_cert, pk_pub}``: ``sign`` is the device key's (dk_sig) signature of
    ``label || cj(o)`` under ``sig_hub_publish_approval``. Call it only with an object :func:`check_commit_request`
    returned."""
    sig = sign(L["sig_hub_publish_approval"] + cj(dict(o)))
    return {"o": dict(o), "sig": b64u(sig), "device_cert": dict(device_cert), "pk_pub": pk_pub}
