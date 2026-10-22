"""``publish_out``: forward a host-signed request to orch-publish with the hub's transport signature (§11.1).

The hub adds ``Orch-Hub`` and ``Orch-Hub-Signature`` and nothing else: it never alters the host's headers, target or
body, and the signature it adds carries no authority (orch-publish re-checks the host's signature, the binding and,
for public access, the owner's approval). Fail closed: anything that is not the exact request shape is refused.
"""

from __future__ import annotations

import http.client
import ipaddress
import threading
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..contract.canon import b64u, cj
from ..contract.hub import Refused, hub_path_signed_bytes
from ..contract.keystore import KeyUseError, OpSigner
from ..keys.store import HubKeys
from . import request as R
from .sync import PublishSync, PublishSyncError

MAX_REQUEST_BODY = 8 * 1024 * 1024
MAX_RESPONSE_BODY = 16 * 1024 * 1024
#: orch-publish answers with these when the hub's card or the binding is not (or no longer) held: forget what we synced
_RESYNC_CODES = (b"not_bound", b"unknown_hub")


@dataclass(frozen=True)
class PublishConfig:
    base_url: str  # where orch-publish listens; https, or http on a loopback address only
    api_host: (
        str  # the host the workspaces sign (orch-publish's configured api_host): requests naming another are refused
    )
    timeout_s: float = 15.0
    max_request_body: int = MAX_REQUEST_BODY
    max_response_body: int = MAX_RESPONSE_BODY


def _endpoint(base_url: str) -> tuple[str, str, int]:
    u = urllib.parse.urlsplit(base_url)
    if u.scheme not in ("http", "https") or not u.hostname or u.path not in ("", "/") or u.query or u.fragment:
        raise ValueError("base_url must be http(s)://host[:port]")
    if u.username or u.password:
        raise ValueError("base_url carries no credentials")
    if u.scheme == "http":
        try:
            loopback = u.hostname == "localhost" or ipaddress.ip_address(u.hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("plain http only on a loopback address")
    return u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80)


class HubPublish:
    """Implements ``PublishForwarder`` (link server) and the link's ``Downstream`` (it learns the bindings)."""

    def __init__(
        self, keys: HubKeys | Callable[[], tuple[OpSigner, dict[str, Any], str]], sync: PublishSync, cfg: PublishConfig
    ):
        self._scheme, self._host, self._port = _endpoint(cfg.base_url)
        self._cfg = cfg
        self._sync = sync
        if isinstance(keys, HubKeys):
            hk = keys

            def current() -> tuple[OpSigner, dict[str, Any], str]:
                return hk.op_signer, hk.card(), hk.hub_id

            self._keys = current
        else:
            self._keys = keys
        self._lock = threading.Lock()
        self._bindings: dict[str, dict[str, Any]] = {}  # workspace -> latest binding / unbound object, as accepted
        self._synced: dict[str, bytes] = {}  # workspace -> cj of the object orch-publish holds
        self._card_synced: bytes | None = None

    # --- Downstream (the link server calls these after it accepted the object) -------------------------------------

    def card(self, workspace_id: str, card: Mapping[str, Any], pk_pub: str) -> None:
        pass  # the workspace's v2 card reaches orch-publish through its own register flow, not through the hub

    def binding(self, workspace_id: str, obj: Mapping[str, Any]) -> None:
        with self._lock:
            self._bindings[workspace_id] = dict(obj)
        try:
            self._ensure(workspace_id, need_bound=False)
        except Refused:
            pass  # not reachable now: the next forward syncs again, and refuses if it cannot

    # --- PublishForwarder ------------------------------------------------------------------------------------------

    def forward(self, workspace_id: str, request: dict[str, Any]) -> dict[str, Any]:
        p = R.parse(request, max_body=self._cfg.max_request_body)
        if p.workspace_id != workspace_id:
            raise Refused("other_workspace")
        if p.host != self._cfg.api_host:
            raise Refused("malformed")
        signer, _, hub_id = self._keys()
        self._ensure(workspace_id, need_bound=True)
        pbytes = hub_path_signed_bytes(
            bytes.fromhex(p.workspace_id), p.ts_ms, p.nonce, p.method, p.host, p.target, p.body, hub_id, p.approval
        )
        try:
            hsig = signer.publish_transport(hub_id, pbytes)
        except KeyUseError:
            raise Refused("malformed") from None
        headers = dict(p.headers)
        headers["Orch-Hub"] = hub_id
        headers["Orch-Hub-Signature"] = b64u(hsig)
        status, body = self._send(p, headers)
        if status in (401, 403) and any(c in body for c in _RESYNC_CODES):
            with self._lock:
                self._synced.pop(workspace_id, None)
                self._card_synced = None
        return {"status": status, "body_hex": body.hex()}

    # --- internals -------------------------------------------------------------------------------------------------

    def _ensure(self, workspace_id: str, *, need_bound: bool) -> None:
        """Push the hub's card and the workspace's latest binding object when orch-publish does not hold them yet."""
        _, card, _ = self._keys()
        with self._lock:
            obj = self._bindings.get(workspace_id)
            if need_bound and (obj is None or obj.get("o", {}).get("kind") != "hub.binding"):
                raise Refused("not_bound")
            try:
                c = cj(card)
                if self._card_synced != c:
                    self._sync.put_hub_card(card)
                    self._card_synced = c
                if obj is not None:
                    b = cj(obj)
                    if self._synced.get(workspace_id) != b:
                        self._sync.put_binding(workspace_id, obj)
                        self._synced[workspace_id] = b
            except PublishSyncError as e:
                if e.code == "unavailable":
                    raise Refused("unavailable") from None
                raise Refused(e.code) from None

    def _send(self, p: R.Parsed, headers: Mapping[str, str]) -> tuple[int, bytes]:
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=self._cfg.timeout_s)
        try:
            conn.request(p.method, p.target, body=p.body or None, headers=dict(headers))
            resp = conn.getresponse()  # a 3xx is returned as is: nothing here follows a redirect
            limit = self._cfg.max_response_body
            body = resp.read(limit + 1)
            if len(body) > limit:
                raise Refused("unavailable")
            return resp.status, body
        except OSError, http.client.HTTPException:
            raise Refused("unavailable") from None
        finally:
            conn.close()
