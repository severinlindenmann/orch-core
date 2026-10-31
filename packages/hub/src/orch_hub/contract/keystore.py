"""One signer per key kind, with its signing domains hard-wired (contracts/hub/protocol.md §3, L2-4).

This is the only signing API the contract offers. There is no "sign these bytes under this label" call: each signer
takes the object (or the exact fields) it may sign, checks the ``kind`` (and the link direction), builds the label
itself, and raises :class:`KeyUseError` for anything else. A hub key store wraps the private key of each kind in its
signer once and never hands the raw key to other code; the hub's custodial engine holds only :class:`UkSigner`
objects, the link server only :class:`OpSigner`, and so on.

Signatures are randomised (real signers); the vector generator signs deterministically on its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar

from cryptography.hazmat.primitives.asymmetric import ec

from . import hub as H
from . import suite as S
from .canon import CanonError, b64u, cj
from .labels import L


class KeyUseError(ValueError):
    """A signer was asked to sign something outside its key kind's domains."""


class _Signer:
    #: kind -> label key; ``hub.link`` is handled by direction
    KINDS: ClassVar[Mapping[str, str]] = MappingProxyType({})
    LINK_DIR: ClassVar[str | None] = None

    def __init__(self, key: ec.EllipticCurvePrivateKey) -> None:
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise KeyUseError("not a P-256 private key")
        self.__key = key

    @property
    def public(self) -> bytes:
        return S.public_bytes(self.__key)

    def _sig(self, msg: bytes) -> bytes:
        return S.sign(self.__key, msg)

    def sign_link(self, o: Mapping[str, Any], exporter: bytes) -> dict[str, Any]:
        """Sign one ``hub.link`` message of this key's direction, bound to the connection's TLS exporter (§10.2)."""
        if self.LINK_DIR is None:
            raise KeyUseError(f"{type(self).__name__} may not sign kind 'hub.link'")
        if type(o) is not dict or o.get("kind") != "hub.link":
            raise KeyUseError("not a link message")
        if o.get("dir") != self.LINK_DIR:
            raise KeyUseError(f"this key signs link messages only in direction {self.LINK_DIR}")
        if o.get("v") != 2 or o.get("suite") != S.SUITE_ID or o.get("hv") != H.HV:
            raise KeyUseError("header is not v 2, suite 2, hv 0")
        try:
            msg = H.link_signed_bytes(self.LINK_DIR, exporter, o)
        except H.Refused as r:
            raise KeyUseError(r.code) from None
        return {"o": o, "sig": b64u(self._sig(msg))}

    def sign_object(self, o: Mapping[str, Any]) -> dict[str, Any]:
        """Sign one hub object of a kind this key may sign; returns ``{"o", "sig"}``."""
        if type(o) is not dict:
            raise KeyUseError("not an object")
        kind = o.get("kind")
        if kind == "hub.link":
            raise KeyUseError("a link message is signed with sign_link (it binds the TLS exporter)")
        if type(kind) is str and kind in self.KINDS:
            label = L[self.KINDS[kind]]
        else:
            raise KeyUseError(f"{type(self).__name__} may not sign kind {kind!r}")
        if o.get("v") != 2 or o.get("suite") != S.SUITE_ID or o.get("hv") != H.HV:
            raise KeyUseError("header is not v 2, suite 2, hv 0")
        try:
            msg = label + cj(o)
        except CanonError as e:
            raise KeyUseError(str(e)) from None
        return {"o": o, "sig": b64u(self._sig(msg))}


class RootSigner(_Signer):
    """``HK_root``: op certificates and their revocations, nothing else."""

    KINDS = MappingProxyType({"hub.op_cert": "sig_hub_op_cert", "hub.op_revoked": "sig_hub_op_revoked"})

    def sign_backup_header(self, o: Mapping[str, Any]) -> bytes:
        """The ``hub.backup`` header signature (protocol §17.1): only this key signs it, under its own label (not in
        ``HUB_LABELS``, so it is not a ``KINDS`` entry); the caller builds the envelope."""
        from . import backup as B

        if type(o) is not dict or o.get("kind") != B.KIND:
            raise KeyUseError("not a hub.backup header")
        if o.get("v") != 2 or o.get("suite") != S.SUITE_ID or o.get("hv") != H.HV:
            raise KeyUseError("header is not v 2, suite 2, hv 0")
        try:
            return self._sig(B.LABEL + cj(o))
        except CanonError as e:
            raise KeyUseError(str(e)) from None


class OpSigner(_Signer):
    """``HK_op``: the card, UK and channel certificates, snapshots, hub->host link messages, publish transport."""

    KINDS = MappingProxyType(
        {
            "hub.card": "sig_hub_card",
            "hub.uk_cert": "sig_hub_uk_cert",
            "hub.channel_cert": "sig_hub_channel_cert",
            "hub.snapshot": "sig_hub_snapshot",
            "hub.space": "sig_hub_space",
            "hub.space_wrap": "sig_hub_space_wrap",
            "hub.audit": "sig_hub_audit",
            "hub.space_proposal": "sig_hub_space_proposal",
            "hub.closing": "sig_hub_closing",
            "hub.org_skills": "sig_hub_org_skills",
            "hub.mcp_servers": "sig_hub_mcp_servers",
            "hub.release_narrow": "sig_hub_release_narrow",
            "hub.app_pass": "sig_hub_app_pass",
            "hub.allow": "sig_hub_allow",
            "hub.publish_where": "sig_hub_publish_where",
            "hub.org_secrets": "sig_hub_org_secrets",
            "hub.api_origin": "sig_hub_api_origin",
        }
    )
    LINK_DIR = "h2w"

    def sign_card(self, o: Mapping[str, Any], op_cert: Mapping[str, Any]) -> dict[str, Any]:
        if type(o) is not dict or o.get("kind") != "hub.card":
            raise KeyUseError("not a hub.card")
        c = self.sign_object(o)
        return {"o": c["o"], "op_cert": dict(op_cert), "sig": c["sig"]}

    def sign_object(self, o: Mapping[str, Any]) -> dict[str, Any]:
        if type(o) is dict and o.get("kind") == "hub.card" and "op_cert" in o:
            raise KeyUseError("pass the card's o, not the card")
        return super().sign_object(o)

    def admin_read(self, hub_id: str, ts_ms: int, method: str, target: str) -> bytes:
        """The hub admin read signature (§10.6) over the exact bytes; the method is GET or HEAD only."""
        if method not in ("GET", "HEAD"):
            raise KeyUseError("admin reads are GET or HEAD")
        try:
            return self._sig(H.admin_read_bytes(hub_id, ts_ms, method, target))
        except H.Refused as r:
            raise KeyUseError(r.code) from None

    def relay_request(
        self, hub_id: str, op_cert_id: str, ts_ms: int, nonce: bytes, method: str, target: str, body: bytes
    ) -> bytes:
        """A hub -> relay request (§10.8) over the exact bytes; the target must be canonical and name this hub."""
        from . import phase4 as P4

        if method not in P4.RELAY_METHODS:
            raise KeyUseError("not a relay request method")
        try:
            P4.parse_relay_target(target, hub_id)
            return self._sig(P4.relay_request_bytes(hub_id, op_cert_id, ts_ms, nonce, method, target, body))
        except H.Refused as r:
            raise KeyUseError(r.code) from None

    def publish_transport(self, hub_id: str, wsk_signed_bytes: bytes) -> bytes:
        """The forwarding signature (§11.1) over hub-path WSK-signed bytes only."""
        if type(wsk_signed_bytes) is not bytes or not wsk_signed_bytes.startswith(L["sig_publish_hub"]):
            raise KeyUseError("the hub forwards hub-path publish requests only")
        return self._sig(H.hub_publish_bytes(hub_id, wsk_signed_bytes))


class UkSigner(_Signer):
    """A user's ``UK``: custodial events only, under ``orch/v2/hub-custodial|<type>|`` over the signed context."""

    KINDS = MappingProxyType({})

    def sign_event(self, workspace_id: str, log: str, e_signed: Mapping[str, Any]) -> bytes:
        if type(e_signed) is not dict or e_signed.get("auth") != "hub" or "sig" in e_signed:
            raise KeyUseError("UK signs custodial events (auth: hub, without sig) only")
        try:
            return self._sig(H.custodial_bytes(workspace_id, log, e_signed))
        except H.Refused as r:
            raise KeyUseError(r.code) from None


class ChannelSigner(_Signer):
    """A channel key (phone, browser): the proof over a 32-byte event digest, its own registration object (§6.3), and
    (§6.4, §10.8.4) the session binding, the push-key object and the channel -> relay request bytes, each naming this
    very key or channel, nothing else."""

    KINDS = MappingProxyType(
        {
            "hub.channel_register": "sig_hub_channel_register",
            "hub.channel_auth": "sig_hub_channel_auth",
            "hub.channel_push": "sig_hub_channel_push",
        }
    )

    @property
    def channel_id(self) -> str:
        return H.channel_id(self.public)

    def sign_object(self, o: Mapping[str, Any]) -> dict[str, Any]:
        if type(o) is dict and o.get("kind") == "hub.channel_register" and o.get("key_pub") != b64u(self.public):
            raise KeyUseError("a channel key registers itself only")
        if type(o) is dict and o.get("kind") in ("hub.channel_auth", "hub.channel_push"):
            if o.get("channel_id") != self.channel_id:
                raise KeyUseError("a channel key speaks for its own channel only")
            if o.get("kind") == "hub.channel_push" and o.get("push_pub") == b64u(self.public):
                raise KeyUseError("the channel key is never its own push key")
        return super().sign_object(o)

    def proof(self, digest: bytes) -> bytes:
        if type(digest) is not bytes or len(digest) != 32:
            raise KeyUseError("a channel key signs 32-byte event digests only")
        return self._sig(H.channel_proof_bytes(digest))

    def relay_request(self, hub_id: str, ts_ms: int, nonce: bytes, method: str, target: str, body: bytes) -> bytes:
        """§10.8.4: the signature of a request to this channel's own mailbox at the relay."""
        from .channelapi import _parse_channel_target, channel_request_bytes

        try:
            hub, ch, _ = _parse_channel_target(target)
            if hub != hub_id or ch != self.channel_id:
                raise KeyUseError("a channel key reads its own mailbox only")
            return self._sig(channel_request_bytes(hub_id, ch, ts_ms, nonce, method, target, body))
        except H.Refused as r:
            raise KeyUseError(r.code) from None


class WskSigner(_Signer):
    """A workspace's ``WSK`` (host side): binding, unbound, host->hub link messages, link auth, publish requests."""

    KINDS = MappingProxyType(
        {
            "hub.binding": "sig_hub_binding",
            "hub.unbound": "sig_hub_unbound",
            "space.item": "sig_space_item",
            "space.accept": "sig_space_accept",
            "share.item": "sig_share_item",
        }
    )
    LINK_DIR = "w2h"

    def link_auth(self, hub_id: str, workspace_id: str, challenge: bytes, exporter: bytes) -> bytes:
        try:
            return self._sig(H.link_auth_bytes(hub_id, workspace_id, challenge, exporter))
        except H.Refused as r:
            raise KeyUseError(r.code) from None

    def publish_hub(
        self,
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
        return self._sig(H.hub_path_signed_bytes(ws, ts_ms, nonce, method, host, target, body, hub_id, approval))


class OwnerDeviceSigner(_Signer):
    """The owner's device key on the hub contract: the publish approval only (its person events are core's)."""

    KINDS = MappingProxyType({"hub.publish_approval": "sig_hub_publish_approval"})

    def sign_approval(self, o: Mapping[str, Any], device_cert: Mapping[str, Any], pk_pub: bytes) -> dict[str, Any]:
        a = self.sign_object(o)
        return {"o": a["o"], "sig": a["sig"], "device_cert": dict(device_cert), "pk_pub": b64u(pk_pub)}


class PersonSigner(_Signer):
    """The person key ``PK`` (derived from the recovery code) on the hub contract: the owner proof of a hub op only
    (§9.6). Its other objects (device certs, the workspace delegation) are core's. orch-core's signer holds ``PK``
    only for the moment of one proof and wipes it; this class exists for the reference and the tests."""

    KINDS = MappingProxyType({"hub.owner_proof": "sig_hub_owner_proof"})


SIGNERS = (RootSigner, OpSigner, UkSigner, ChannelSigner, WskSigner, OwnerDeviceSigner, PersonSigner)
