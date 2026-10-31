"""Publish through the hub (V3 C7; hub contract section 11, spec 8 "Publish (D76)", V3-D4).

The host builds the same request it would send to orch-publish directly, signs it with ``WSK`` under its own label and
sends it to the hub over the link (``publish_out``); the hub forwards it unchanged and adds ``Orch-Hub`` /
``Orch-Hub-Signature``. Three pieces, all pure of host state:

* :func:`hub_path_signed_bytes`: the bytes ``WSK`` signs (11.1), byte-equal to orch-publish's verifier (pinned by the
  vectors)::

      "orch/v2/publish-hub|" || suite || ws(16) || u64(ts) || nonce(16) || lp16(METHOD) || lp16(host) || lp16(target)
          || H(body) || hub_id(16) || approval_hash          (approval_hash = H(cj(approval)) or 32 zero bytes)

* :class:`HubPublishSigner`: the purpose-built ``WSK`` signer. Same rules as :class:`orch.wsproto.pubsign.PublishSigner`
  (host derived from the signed publish setting, target pinned to the workspace's routes, a fresh nonce and clock per
  call) plus the hub: the signature commits to the hub that forwards it. The request content is whatever the host's
  publish op passes in; nothing here takes a blob to sign.
* :class:`HubPublishTransport`: ``PublishClient``'s transport, one ``publish_out`` per call. A hub that cannot be
  reached raises :class:`HubRefused` ``hub_unreachable``; it is **never** turned into a direct request.

Public access (11.3). No publish route grants public access today (``PUBLIC_ROUTES`` is empty, as in orch-publish's
``hub_path``). A route listed there is refused here unless the owner's ``hub.publish_approval`` naming this exact
request (``request_hash``) is supplied by the ``approver``; no host op passes one yet, so such a request fails closed
(``approval_required``). The approval, when there is one, is inside the ``WSK`` signature (``approval_hash``).
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from orch.canon.jcs import JcsError
from orch.canon.jcs import dumps as cj
from orch.crypto import suite as _suite
from orch.crypto.encoding import b64u, unhex
from orch.crypto.labels import L
from orch.wsproto.pubsign import METHODS, PublishContext, SignedPublish, _target_ok
from orch.wsproto.sign import SignerRefused

from .errors import HubRefused

__all__ = [
    "HubPublishSigner",
    "HubPublishTransport",
    "PUBLIC_ROUTES",
    "approval_hash",
    "card_put",
    "hub_path_signed_bytes",
    "request_access",
    "request_hash",
]

#: ``(method, regex over the path without query)`` of routes that grant public access: today none (11.2, F1 fixes the
#: list in orch-publish). A route added here needs the owner's approval on every request.
PUBLIC_ROUTES: tuple[tuple[str, str], ...] = ()
_NONCE_LEN = 16
_H_REQUEST = b"orch/v2/hub-publish-request|"
#: the link frame is 1 MiB; leave room for the envelope, the headers and the signature
REQUEST_MAX = 900_000

Approver = Callable[[str], "Mapping[str, Any] | None"]


def _lp16(b: bytes) -> bytes:
    if len(b) > 0xFFFF:
        raise SignerRefused("field over 65535 bytes")
    return len(b).to_bytes(2, "big") + b


def _fields(ws: bytes, ts_ms: int, nonce: bytes, method: str, host: str, target: str, body: bytes) -> bytes:
    if type(ws) is not bytes or len(ws) != 16 or type(nonce) is not bytes or len(nonce) != _NONCE_LEN:
        raise SignerRefused("workspace id or nonce")
    if type(ts_ms) is not int or not 0 <= ts_ms < 2**53 or method not in METHODS or type(body) is not bytes:
        raise SignerRefused("timestamp, method or body")
    try:
        parts = (method.encode("ascii"), host.encode("ascii"), target.encode("ascii"))
    except (UnicodeEncodeError, AttributeError):
        raise SignerRefused("host or target is not ASCII") from None
    return (
        bytes([_suite.SUITE_ID])
        + ws
        + ts_ms.to_bytes(8, "big")
        + nonce
        + b"".join(_lp16(p) for p in parts)
        + _suite.sha256(body)
    )


def approval_hash(approval: Mapping[str, Any] | None) -> bytes:
    """``H(cj(approval))``, or 32 zero bytes when the request carries none."""
    if approval is None:
        return bytes(32)
    try:
        return _suite.sha256(cj(dict(approval)))
    except (JcsError, TypeError, ValueError):
        raise SignerRefused("approval is not canonical JSON") from None


def hub_path_signed_bytes(
    ws: bytes,
    ts_ms: int,
    nonce: bytes,
    method: str,
    host: str,
    target: str,
    body: bytes,
    hub_id: str,
    approval: Mapping[str, Any] | None,
) -> bytes:
    """What ``WSK`` signs on the hub path (contract 11.1)."""
    try:
        hub = unhex(hub_id, 16)
    except Exception:  # noqa: BLE001 - any malformed hub id
        raise SignerRefused("hub id") from None
    return L["sig_publish_hub"] + _fields(ws, ts_ms, nonce, method, host, target, body) + hub + approval_hash(approval)


def request_hash(
    ws: bytes, ts_ms: int, nonce: bytes, method: str, host: str, target: str, body: bytes, hub_id: str
) -> bytes:
    """What the owner's approval names (11.3): the one exact request (``ts``, ``nonce``), through this hub."""
    try:
        hub = unhex(hub_id, 16)
    except Exception:  # noqa: BLE001
        raise SignerRefused("hub id") from None
    return _suite.sha256(_H_REQUEST + _fields(ws, ts_ms, nonce, method, host, target, body) + hub)


def request_access(method: str, target: str) -> str | None:
    """``"public"`` when this route grants public access (needs the owner's approval), else ``None``."""
    path = target.partition("?")[0]
    for m, rx in PUBLIC_ROUTES:
        if method == m and re.fullmatch(rx, path):
            return "public"
    return None


class HubPublishSigner:
    """Purpose-specific ``WSK`` signatures of publish requests that go through ``hub_id``. Duck-types
    :class:`~orch.wsproto.pubsign.PublishSigner` for :class:`~orch.publishclient.client.PublishClient`."""

    __slots__ = ("_approver", "_clock", "_lock", "_pub", "_rand", "_seen", "_sign", "ctx", "hub_id")

    def __init__(
        self,
        *,
        public_key: bytes,
        sign: Callable[[bytes], bytes],
        ctx: PublishContext,
        hub_id: str,
        approver: Approver | None = None,
        clock_ms: Callable[[], int] | None = None,
        rand: Callable[[int], bytes] | None = None,
    ) -> None:
        try:
            unhex(hub_id, 16)
        except Exception:  # noqa: BLE001
            raise SignerRefused("hub id") from None
        self.ctx, self.hub_id = ctx, hub_id
        self._pub, self._sign, self._approver = bytes(public_key), sign, approver
        self._clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._rand = rand or os.urandom
        self._seen: set[bytes] = set()
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"HubPublishSigner(workspace={self.ctx.workspace_id}, hub={self.hub_id})"

    @property
    def public_key(self) -> bytes:
        return self._pub

    def pin(self, ts_ms: int, nonce: bytes, approval: Mapping[str, Any]) -> _PinnedSigner:
        """A one-shot signer for a request whose ``ts``, ``nonce`` and approval were drawn and signed before."""
        return _PinnedSigner(self, ts_ms, nonce, approval)

    def _request_pinned(
        self, method: str, target: str, body: bytes, ts: int, nonce: bytes, approval: Mapping[str, Any]
    ) -> SignedPublish:
        _target_ok(target, self.ctx.workspace_id)
        if method not in METHODS or type(body) is not bytes or len(nonce) != _NONCE_LEN:
            raise SignerRefused("method, body or nonce")
        ws = bytes.fromhex(self.ctx.workspace_id)
        want = b64u(request_hash(ws, ts, nonce, method, self.ctx.host, target, body, self.hub_id))
        o = approval.get("o")
        if not isinstance(o, Mapping) or o.get("request_hash") != want or o.get("hub_id") != self.hub_id:
            raise SignerRefused("the approval names another request")
        return self._sign_request(method, target, body, ws, ts, nonce, approval)

    def request(self, method: str, target: str, body: bytes = b"") -> SignedPublish:
        _target_ok(target, self.ctx.workspace_id)
        if method not in METHODS or type(body) is not bytes:
            raise SignerRefused("method or body")
        ws = bytes.fromhex(self.ctx.workspace_id)
        ts, nonce = self._clock(), self._rand(_NONCE_LEN)
        approval: Mapping[str, Any] | None = None
        if request_access(method, target) == "public":
            # the owner sees and signs after ts and nonce are drawn (11.3); no approver, no approval, no request
            rh = b64u(request_hash(ws, ts, nonce, method, self.ctx.host, target, body, self.hub_id))
            approval = self._approver(rh) if self._approver is not None else None
            if approval is None:
                raise SignerRefused("approval_required")
        return self._sign_request(method, target, body, ws, ts, nonce, approval)

    def _sign_request(
        self,
        method: str,
        target: str,
        body: bytes,
        ws: bytes,
        ts: int,
        nonce: bytes,
        approval: Mapping[str, Any] | None,
    ) -> SignedPublish:
        signed = hub_path_signed_bytes(ws, ts, nonce, method, self.ctx.host, target, body, self.hub_id, approval)
        with self._lock:
            if nonce in self._seen:
                raise SignerRefused("nonce reuse")
            sig = self._sign(signed)
            if type(sig) is not bytes or len(sig) != _suite.SIG_LEN:
                raise SignerRefused("the key returned a malformed signature")
            self._seen.add(nonce)
        headers = {
            "Orch-Workspace": self.ctx.workspace_id,
            "Orch-Ts": str(ts),
            "Orch-Nonce": nonce.hex(),
            "Orch-Signature": b64u(sig),
        }  # exactly the host's set (contract 11.1): Orch-Hub and Orch-Hub-Signature are the hub's alone
        if approval is not None:
            headers["Orch-Owner-Approval"] = b64u(cj(dict(approval)))
        return SignedPublish(method, self.ctx.host, target, headers)


class _PinnedSigner:
    """One request with a ``ts``, a ``nonce`` and an owner's approval that were fixed BEFORE (#1455, hub contract 11.3):
    an app commit, rollback or start. The approval names this request's ``request_hash``; it is checked here against the
    request about to be signed, so a mismatch fails closed before anything is sent. It signs exactly once and never
    retries with a new ``ts`` (that would be another request than the one the owner approved)."""

    __slots__ = ("_approval", "_nonce", "_parent", "_ts", "_used", "ctx")

    def __init__(self, parent: HubPublishSigner, ts_ms: int, nonce: bytes, approval: Mapping[str, Any]) -> None:
        self._parent, self._ts, self._nonce, self._approval = parent, ts_ms, bytes(nonce), dict(approval)
        self._used = False
        self.ctx = parent.ctx

    @property
    def public_key(self) -> bytes:
        return self._parent.public_key

    def request(self, method: str, target: str, body: bytes = b"") -> SignedPublish:
        if self._used:
            raise SignerRefused("an approved request is sent once")
        self._used = True
        return self._parent._request_pinned(method, target, body, self._ts, self._nonce, self._approval)  # noqa: SLF001


def card_put(link: Any, card: Mapping[str, Any], owner_pk_pub: str) -> dict[str, Any]:
    """Hand the workspace's v2 card to the hub (``card_put``, contract section 10); the hub forwards it to orch-publish,
    which verifies it and admits the workspace (R5a, V3-D36). A refusal of the hub or of orch-publish raises
    :class:`HubRefused` with its code; an unreachable hub is ``hub_unreachable``. Idempotent."""
    link.call("card_put", {"card": dict(card), "pk_pub": owner_pk_pub})
    return {"result": "registered"}


class HubPublishTransport:
    """``PublishClient``'s transport over the hub link: ``(scheme, netloc, method, target, headers, body, timeout)`` ->
    ``(status, body)``. ``link`` is a :class:`~orch.hubclient.link.HubLink`. :class:`HubRefused` is not caught here: an
    unreachable hub or a hub refusal reaches the host's publish op, which reports it and sends nothing direct."""

    def __init__(self, link: Any) -> None:
        self._link = link

    def __call__(
        self,
        scheme: str,
        netloc: str,
        method: str,
        target: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> tuple[int, bytes]:
        request = {
            "method": method,
            "host": netloc,
            "target": target,
            "headers": dict(headers),
            "body_hex": body.hex(),
        }
        if len(body) * 2 + sum(len(k) + len(v) for k, v in headers.items()) + len(target) > REQUEST_MAX:
            raise HubRefused("too_large")  # the link frame is 1 MiB; nothing was sent
        reply = self._link.call("publish_out", {"request": request})
        status, body_hex = reply.get("status"), reply.get("body_hex")
        if type(status) is not int or not 100 <= status <= 599 or type(body_hex) is not str:
            raise HubRefused("malformed")
        try:
            data = bytes.fromhex(body_hex)
        except ValueError:
            raise HubRefused("malformed") from None
        return status, data
