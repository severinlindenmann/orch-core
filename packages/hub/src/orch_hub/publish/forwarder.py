"""``publish_out``: forward a host-signed request to orch-publish with the hub's transport signature (§11.1).

The hub adds ``Orch-Hub`` and ``Orch-Hub-Signature`` and nothing else: it never alters the host's headers, target or
body, and the signature it adds carries no authority (orch-publish re-checks the host's signature, the binding and,
for public access, the owner's approval). Fail closed: anything that is not the exact request shape is refused.
"""

from __future__ import annotations

import http.client
import ipaddress
import logging
import os
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..contract.canon import b64u, cj
from ..contract.hub import Refused, hub_path_signed_bytes
from ..contract.keystore import KeyUseError, OpSigner
from ..keys.store import HubKeys
from ..localdial import dialling_locally
from . import request as R
from .sync import PublishSync, PublishSyncError

MAX_REQUEST_BODY = 8 * 1024 * 1024
MAX_RESPONSE_BODY = 16 * 1024 * 1024
#: orch-publish answers with these when the hub's card or the binding is not (or no longer) held: forget what we synced
#: refusal codes of contracts/hub/protocol.md §14 / v2 that orch-publish's store can answer a card or binding with; any
#: other code from the sync is reported as ``unavailable``
SYNC_CODES = frozenset(
    [
        "malformed",
        "wrong_suite",
        "other_hub",
        "other_workspace",
        "hub_id_mismatch",
        "wsk_mismatch",
        "not_yet_valid",
        "binding_conflict",
        "stale_binding",
        "unbound",
        "stale_card",
        "stale_op_cert",
        "dev_refused",
        "dev_mismatch",
        "cert_invalid",
        "cert_expired",
        "revoked",
        "unknown_workspace",
        "unknown_hub",
        "bad_signature",
        "not_bound",
        "other_person",
        "delegation_mismatch",
        "delegation_changed",
        "workspace_blocked",
        "owner_not_allowed",
        "delegation_refused",
    ]
)
_RESYNC_CODES = (b"not_bound", b"unknown_hub")


@dataclass(frozen=True)
class PublishConfig:
    base_url: str  # where orch-publish listens; https, or http on loopback, private or CGNAT (100.64/10) addresses
    api_host: (
        str  # the host the workspaces sign (orch-publish's configured api_host): requests naming another are refused
    )
    timeout_s: float = 15.0  # per socket operation
    deadline_s: float = 30.0  # for the whole forward, sync included
    max_request_body: int = MAX_REQUEST_BODY
    max_response_body: int = MAX_RESPONSE_BODY


_PLAIN_HTTP_NETS = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
)


def _endpoint(base_url: str) -> tuple[str, str, int]:
    u = urllib.parse.urlsplit(base_url)
    if u.scheme not in ("http", "https") or not u.hostname or u.path not in ("", "/") or u.query or u.fragment:
        raise ValueError("base_url must be http(s)://host[:port]")
    if u.username or u.password:
        raise ValueError("base_url carries no credentials")
    if u.scheme == "http":
        try:
            ip = None if u.hostname == "localhost" else ipaddress.ip_address(u.hostname)
            ok = ip is None or ip.is_loopback or any(ip in n for n in _PLAIN_HTTP_NETS if ip.version == n.version)
        except ValueError:
            ok = False
        if not ok:
            raise ValueError("plain http only on a loopback, private or CGNAT address")
    return u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80)


class HubPublish:
    """Implements ``PublishForwarder`` (link server) and the link's ``Downstream`` (it learns the bindings)."""

    def __init__(
        self,
        keys: HubKeys | Callable[[], tuple[OpSigner, dict[str, Any], str]],
        sync: PublishSync,
        cfg: PublishConfig,
        revocations: Callable[[], list[dict[str, Any]]] | None = None,
        admission: Callable[[str], tuple[str, int] | None] | None = None,
    ):
        self._scheme, self._host, self._port = _endpoint(cfg.base_url)
        self._cfg = cfg
        self._sync = sync
        if isinstance(keys, HubKeys):
            hk = keys

            def current() -> tuple[OpSigner, dict[str, Any], str]:
                return hk.op_signer, hk.card(), hk.hub_id

            self._keys = current
            revocations = revocations or hk.revocations
        else:
            self._keys = keys
        self._lock = threading.Lock()
        self._ws_locks: dict[str, threading.Lock] = {}
        self._bindings: dict[str, dict[str, Any]] = {}  # workspace -> latest binding / unbound object, as accepted
        self._synced: dict[str, bytes] = {}  # workspace -> cj of the object orch-publish holds
        self._card_synced: bytes | None = None
        self._revocations = revocations or (list)
        self._revoked_synced: set[bytes] = set()
        # P02: ``admission(workspace_id) -> (owner_person_id, binding_seq)`` or None. The hub's own answer, read from its
        # registry and access store: this workspace is joined to THIS hub, bound now, and its owner is a hub admin, and
        # the hub setting lets workspaces share files. None (the default) = never send a hub.allow.
        self._admission = admission

    # --- Downstream (the link server calls these after it accepted the object) -------------------------------------

    def card(self, workspace_id: str, card: Mapping[str, Any], pk_pub: str) -> None:
        """The workspace's v2 card (``card_put``, already checked against the pin) goes to orch-publish, which verifies it
        itself and admits the workspace exactly as its pre-bind, allowlist and never-change rules say (V3-D36: this is
        the only way a workspace reaches publish). A refusal goes back to the workspace as the hub's refusal."""
        with self._ws_lock(workspace_id):
            self._allow(workspace_id)
            try:
                self._sync.put_card(workspace_id, card, pk_pub)
            except PublishSyncError as e:
                raise Refused(e.code if e.code in SYNC_CODES else "unavailable") from None

    def binding(self, workspace_id: str, obj: Mapping[str, Any]) -> None:
        """Remember the workspace's newest binding / unbound. An unbound is final: a binding at or below the unbound
        sequence never replaces it, and a lower sequence never replaces anything (contracts/hub/protocol.md §9.4)."""
        o = obj.get("o") if isinstance(obj, Mapping) else None
        if (
            not isinstance(o, Mapping)
            or o.get("kind") not in ("hub.binding", "hub.unbound")
            or type(o.get("binding_seq")) is not int
        ):
            raise Refused("malformed")
        if o.get("workspace_id") != workspace_id:
            raise Refused("other_workspace")
        with self._lock:
            held = self._bindings.get(workspace_id)
            if held is not None:
                ho = held["o"]
                if cj(dict(obj)) == cj(held):
                    return
                if ho["kind"] == "hub.unbound" and o["binding_seq"] <= ho["binding_seq"]:
                    raise Refused("unbound")
                if o["binding_seq"] < ho["binding_seq"]:
                    raise Refused("stale_binding")
                if o["binding_seq"] == ho["binding_seq"] and o["kind"] != "hub.unbound":
                    raise Refused("binding_conflict")
            self._bindings[workspace_id] = dict(obj)
        if o["kind"] == "hub.binding":  # a new binding epoch (join, re-join, mode switch) needs its own allowance
            with self._ws_lock(workspace_id):
                self._allow(workspace_id)
        try:
            self._ensure(
                workspace_id, self._keys()[1], need_bound=False, deadline=time.monotonic() + self._cfg.deadline_s
            )
        except Refused:
            pass  # not reachable now: the next forward syncs again, and refuses if it cannot

    def restore(self, bindings: Mapping[str, Mapping[str, Any]]) -> None:
        """After a restart: the signed binding / unbound objects the registry persisted (nothing is synced yet)."""
        for ws, obj in bindings.items():
            try:
                self.binding(ws, obj)
            except Refused:
                pass  # an object that no longer passes is dropped: fail closed (not_bound)

    # --- PublishForwarder ------------------------------------------------------------------------------------------

    def forward(self, workspace_id: str, request: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + self._cfg.deadline_s
        p = R.parse(request, max_body=self._cfg.max_request_body)
        if p.workspace_id != workspace_id:
            raise Refused("other_workspace")
        if p.host != self._cfg.api_host:
            raise Refused("malformed")
        signer, card, hub_id = self._keys()  # read once: card, signer and hub id belong to the same generation
        self._ensure(workspace_id, card, need_bound=True, deadline=deadline)
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
        status, body = self._send(p, headers, deadline)
        if status in (401, 403) and any(c in body for c in _RESYNC_CODES):
            with self._lock:
                self._synced.pop(workspace_id, None)
                self._card_synced = None
                self._revoked_synced.clear()
        return {"status": status, "body_hex": body.hex()}

    # --- internals -------------------------------------------------------------------------------------------------

    def _ws_lock(self, workspace_id: str) -> threading.Lock:
        with self._lock:
            return self._ws_locks.setdefault(workspace_id, threading.Lock())

    def _allow(self, workspace_id: str) -> None:
        """Tell orch-publish, signed with HK_op, that this workspace and its owner are this hub's own (``hub.allow``,
        protocol 11.9). Best effort: a refusal (the operator never pinned this hub, no network) only means the old
        admission rules still apply and the workspace is told at its first send. The caller holds the workspace lock."""
        if self._admission is None:
            return
        try:
            got = self._admission(workspace_id)
            if got is None:
                return
            owner, seq = got
            signer, card, hub_id = self._keys()
            c = cj(card)
            if self._card_synced != c:
                self._sync.put_hub_card(card)
                with self._lock:
                    self._card_synced = c
                    self._revoked_synced.clear()
            obj = signer.sign_object(
                {
                    "v": 2,
                    "suite": 2,
                    "kind": "hub.allow",
                    "hv": 0,
                    "hub_id": hub_id,
                    "workspace_id": workspace_id,
                    "owner_person_id": owner,
                    "binding_seq": seq,
                    "ts_ms": int(time.time() * 1000),
                    "nonce": b64u(os.urandom(32)),
                }
            )
            self._sync.put_allow(workspace_id, obj)
        except (PublishSyncError, KeyUseError, Refused, ValueError, KeyError, OSError) as e:
            logging.getLogger("orch_hub.publish").info("hub.allow not sent: %s", getattr(e, "code", type(e).__name__))

    def _ensure(self, workspace_id: str, card: dict[str, Any], *, need_bound: bool, deadline: float) -> None:
        """Push the hub's card and the workspace's latest binding object when orch-publish does not hold them yet.
        The state lock is never held across a sync call; syncs of one workspace are serialised by its own lock."""
        with self._ws_lock(workspace_id):
            with self._lock:
                obj = self._bindings.get(workspace_id)
                card_synced, synced = self._card_synced, self._synced.get(workspace_id)
            if need_bound and (obj is None or obj["o"]["kind"] != "hub.binding"):
                raise Refused("not_bound")
            try:
                c = cj(card)
                if card_synced != c:
                    self._sync.put_hub_card(card)
                    with self._lock:
                        self._card_synced = c
                        self._revoked_synced.clear()
                for rv in self._revocations():  # after the card: orch-publish needs the hub's card to take them
                    r = cj(rv)
                    if r not in self._revoked_synced:
                        self._sync.put_op_revoked(rv)
                        with self._lock:
                            self._revoked_synced.add(r)
                if obj is not None:
                    b = cj(obj)
                    if synced != b:
                        self._sync.put_binding(workspace_id, obj)
                        with self._lock:
                            self._synced[workspace_id] = b
            except PublishSyncError as e:
                raise Refused(e.code if e.code in SYNC_CODES else "unavailable") from None
            if time.monotonic() > deadline:
                raise Refused("unavailable")

    def _send(self, p: R.Parsed, headers: Mapping[str, str], deadline: float) -> tuple[int, bytes]:
        cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        left = deadline - time.monotonic()
        if left <= 0:
            raise Refused("unavailable")
        conn = dialling_locally(cls(self._host, self._port, timeout=min(self._cfg.timeout_s, left)))
        try:
            conn.request(p.method, p.target, body=p.body or None, headers=dict(headers))
            resp = conn.getresponse()  # a 3xx is returned as is: nothing here follows a redirect
            limit, chunks, n = self._cfg.max_response_body, [], 0
            while True:
                if time.monotonic() > deadline:
                    raise Refused("unavailable")
                chunk = resp.read1(min(65536, limit + 1 - n))
                if not chunk:
                    break
                chunks.append(chunk)
                n += len(chunk)
                if n > limit:
                    raise Refused("unavailable")
            return resp.status, b"".join(chunks)
        except OSError, http.client.HTTPException:
            raise Refused("unavailable") from None
        finally:
            conn.close()
