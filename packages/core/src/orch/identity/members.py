"""Pure checks on the identity-carrying workspace events (ticket-format §5.3, §5.4.2, §5.11): the genesis owner
checks in their stated order, ``member.added``, ``device.added`` and ``device.revoked``.

These are **binding checks, not authorization**: roles, ``roster_v``, expiry/revocation at the event's position and
"who may sign what" are ``model/``'s. Each function takes the event as a strictly parsed mapping and raises
:class:`Refused` (a ``genesis.*``, ``member.*`` or ``device.*`` code, or the protocol's code from
:mod:`orch.identity.certs`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orch import canon, crypto

from .certs import check_cert, check_delegation, delegation_binds, person_scope_ok, verify_revocation
from .errors import Refused

__all__ = [
    "check_device_added",
    "check_device_revoked",
    "check_genesis",
    "check_member_added",
]


def _pk(value: object) -> bytes:
    try:
        return crypto.validate_public_key(crypto.unb64u(value, crypto.PUB_LEN))
    except (crypto.EncodingError, crypto.CryptoError) as e:
        raise Refused("malformed", f"public key: {e}") from None


def _str(event: Mapping[str, Any], key: str) -> str:
    v = event.get(key)
    if type(v) is not str:
        raise Refused("malformed", key)
    return v


def _map(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Refused("malformed", what)
    return value


def _cert_for(person_pk: bytes, signed: object) -> dict[str, Any]:
    cert_o = check_cert(signed, person_pk)
    if not person_scope_ok(cert_o):
        raise Refused("member.cert_scope", "a person device needs a level list with 'decide'; drop: is refused")
    return cert_o


def check_genesis(event: Mapping[str, Any]) -> dict[str, Any]:
    """§5.11 in order, for a ``workspace.created`` event. Returns ``{"owner_pk_pub", "delegation", "device_cert"}`` (the
    verified objects) or raises:

    0. ``genesis.shape``: ``type`` is ``workspace.created``, ``seq`` 1, ``roster_v`` 0, a person actor;
    1. ``genesis.owner_mismatch``: ``person_id(owner.pk_pub) == delegation.o.owner_person_id ==
       device_cert.o.person_id``
       and ``owner.person == "p_" +`` that id;
    2. ``genesis.bad_delegation``: the delegation's signature under ``owner.pk_pub`` and its exact field set;
    3. ``genesis.delegation_mismatch``: ``delegation.o.workspace_id == workspace_id`` and
       ``delegation.o.wsk_pub == wsk_pub``;
    4. ``genesis.host_sig``: the event's ``host_sig`` under ``wsk_pub``;
    5. ``genesis.bad_device_cert``: ``device_cert`` under ``owner.pk_pub``, and ``actor.device == "d_" +
       device_cert.o.device_id``;
    6. ``genesis.bad_sig``: ``sig`` under ``device_cert.o.dk_sig_pub``.
    """
    try:
        workspace_id = _str(event, "workspace_id")
        wsk_b64 = _str(event, "wsk_pub")
        owner = _map(event.get("owner"), "owner")
        actor = _map(event.get("actor"), "actor")
        if (
            event.get("type") != "workspace.created"
            or event.get("seq") != 1
            or event.get("roster_v") != 0
            or actor.get("kind") != "person"
        ):
            raise Refused("genesis.shape")
        owner_pk = _pk(owner.get("pk_pub"))
        wsk_pub = _pk(wsk_b64)
        delegation = _map(event.get("delegation"), "delegation")
        device_cert = _map(event.get("device_cert"), "device_cert")
        d_o = _map(delegation.get("o"), "delegation.o")
        c_o = _map(device_cert.get("o"), "device_cert.o")
    except Refused as e:
        raise Refused("genesis.shape", e.detail or e.code) from None
    pid = crypto.person_id(owner_pk).hex()
    if not (pid == d_o.get("owner_person_id") == c_o.get("person_id") and owner.get("person") == "p_" + pid):
        raise Refused("genesis.owner_mismatch")
    try:
        d_checked = check_delegation(delegation, owner_pk)
    except Refused as e:
        raise Refused("genesis.bad_delegation", e.code) from None
    try:
        delegation_binds(d_checked, workspace_id=workspace_id, wsk_pub_b64u=wsk_b64, owner_person_id=pid)
    except Refused:
        raise Refused("genesis.delegation_mismatch") from None
    log = "workspace"
    try:
        host_ok = crypto.verify(
            wsk_pub,
            crypto.unb64u(event.get("host_sig"), crypto.SIG_LEN),
            canon.host_signing_bytes(workspace_id, log, event),
        )
    except (crypto.EncodingError, canon.HashError):
        host_ok = False
    if not host_ok:
        raise Refused("genesis.host_sig")
    try:
        cert_o = check_cert(device_cert, owner_pk)
    except Refused as e:
        raise Refused("genesis.bad_device_cert", e.code) from None
    # the genesis installs members, policies and settings, so its device needs `operate` (decide-only is refused)
    if not person_scope_ok(cert_o, needs_operate=True) or actor.get("device") != "d_" + cert_o["device_id"]:
        raise Refused("genesis.bad_device_cert", "actor.device or scopes")
    try:
        dk_pub = crypto.unb64u(cert_o["dk_sig_pub"], crypto.PUB_LEN)
        sig_ok = crypto.verify(
            dk_pub,
            crypto.unb64u(event.get("sig"), crypto.SIG_LEN),
            canon.person_signing_bytes(workspace_id, log, event),
        )
    except (crypto.EncodingError, canon.HashError):
        sig_ok = False
    if not sig_ok:
        raise Refused("genesis.bad_sig")
    return {"owner_pk_pub": owner_pk, "delegation": d_checked, "device_cert": cert_o}


def check_member_added(event: Mapping[str, Any]) -> dict[str, Any]:
    """``member.added``: ``person == "p_" + person_id(pk_pub)`` (``member.person_mismatch``) and ``device_cert`` is the
    person's first device, signed by ``pk_pub``, with person scopes (``cert_invalid`` / ``member.cert_scope``).
    Returns the verified certificate object."""
    pk = _pk(event.get("pk_pub"))
    if event.get("person") != "p_" + crypto.person_id(pk).hex():
        raise Refused("member.person_mismatch")
    return _cert_for(pk, event.get("device_cert"))


def check_device_added(event: Mapping[str, Any], person_pk_pub: bytes) -> dict[str, Any]:
    """``device.added``: ``cert`` verified under the person's key, of that person, ``device == "d_" +
    cert.o.device_id`` (``device.id_mismatch``), person scopes. Whether the signer was an existing device (or the
    new one in D50 recovery) is ``model/``'s."""
    cert_o = _cert_for(person_pk_pub, event.get("cert"))
    if event.get("device") != "d_" + cert_o["device_id"]:
        raise Refused("device.id_mismatch")
    return cert_o


def check_device_revoked(
    event: Mapping[str, Any], person_pk_pub: bytes, device_cert_o: Mapping[str, Any]
) -> dict[str, Any]:
    """``device.revoked``: the embedded revocation verified under the person's key **against the revoked device's
    certificate** (protocol §6.2: the certificate must name the same person and device, so a person's key can never
    revoke someone else's device), ``device == "d_" + revocation.device_id`` and ``reason == revocation.o.reason``
    (``device.id_mismatch`` / ``device.reason_mismatch``). ``device_cert_o`` is the certificate object the model holds
    for that device."""
    rev_o = verify_revocation(event.get("revocation"), person_pk_pub, device_cert_o)
    if event.get("device") != "d_" + rev_o["device_id"]:
        raise Refused("device.id_mismatch")
    if event.get("reason") != rev_o["reason"]:
        raise Refused("device.reason_mismatch")
    return rev_o
