"""Signed objects, device certificates, revocations and the workspace delegation (protocol §2.4, §6, §7.1).

Pure functions plus signing through an injected ``sign(payload) -> 64 bytes`` (a custody backend's ``sign`` bound to a
key). Verification is exactly protocol §6.1/§6.2/§7.1, with the reference's refusal codes.

Decisions:

* A signed object is signed over ``label || cj(o)`` with ``cj`` = :func:`orch.canon.cj_checked` (RFC 8785 subset plus
  the text rules); for the object kinds here every string is ASCII, so it equals the protocol's ``cj``
  (tests compare against the vectors' ``signed_bytes``).
* ``verify_cert`` raises :class:`Refused` ``cert_invalid`` / ``cert_expired`` / ``revoked`` in the order of the
  reference. :func:`check_cert` is the time-free part (signature, field set, ids bound to keys, scopes) that the
  event verifier uses, because expiry and revocation are decided *at the event's position* by ``model/``.
* A ``drop:`` certificate can never sign a person event or enter ``device.added`` (ticket-format §5.3):
  :func:`person_scope_ok`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from typing import Any

from orch import canon, crypto
from orch.crypto.labels import L

from .errors import Refused

__all__ = [
    "REVOKE_REASONS",
    "SCOPE_ORDER",
    "SKEW_MS",
    "check_cert",
    "check_delegation",
    "check_revocation",
    "delegation_binds",
    "device_ref",
    "make_delegation",
    "make_device_cert",
    "make_revocation",
    "open_device_label",
    "person_ref",
    "person_scope_ok",
    "scopes_ok",
    "seal_device_label",
    "sign_object",
    "verify_cert",
    "verify_object",
    "verify_revocation",
]

SKEW_MS = 300_000
SCOPE_ORDER = ["look", "decide", "operate", "type"]
REVOKE_REASONS = ("lost", "retired", "compromised")

_KIND_LABEL = {
    "device_cert": "sig_device_cert",
    "revocation": "sig_revocation",
    "ws_delegation": "sig_ws_delegation",
}
_DROP_SCOPE = re.compile(r"drop:[0-9a-f]{32}")
_CERT_FIELDS = {
    "v",
    "suite",
    "kind",
    "device_id",
    "person_id",
    "dk_sig_pub",
    "dk_kx_pub",
    "label_sealed",
    "created_ms",
    "expires_ms",
    "scopes_max",
}
_REVOCATION_FIELDS = {"v", "suite", "kind", "person_id", "device_id", "revoked_ms", "reason"}
_DELEGATION_FIELDS = {"v", "suite", "kind", "workspace_id", "wsk_pub", "owner_person_id", "client_hosted", "issued_ms"}

Signer = Callable[[bytes], bytes]


def person_ref(pk_pub: bytes) -> str:
    """F1 §11.1 person id: ``p_`` + the protocol ``person_id`` in hex."""
    return "p_" + crypto.person_id(pk_pub).hex()


def device_ref(dk_sig_pub: bytes) -> str:
    """F1 §11.1 device id: ``d_`` + the protocol ``device_id`` in hex."""
    return "d_" + crypto.device_id(dk_sig_pub).hex()


# --- signed objects -----------------------------------------------------------------------------------------------


def _bytes(kind: str, o: Mapping[str, Any]) -> bytes:
    return L[_KIND_LABEL[kind]] + canon.cj_checked(o)


def sign_object(o: Mapping[str, Any], sign: Signer) -> dict[str, Any]:
    """``{"o": o, "sig": b64u(sign(label(kind) || cj(o)))}``."""
    if o.get("v") != 2 or o.get("suite") != crypto.SUITE_ID or o.get("kind") not in _KIND_LABEL:
        raise Refused("malformed", "v, suite or kind")
    sig = sign(_bytes(o["kind"], o))
    if type(sig) is not bytes or len(sig) != crypto.SIG_LEN:
        raise Refused("malformed", "signer returned a bad signature")
    return {"o": dict(o), "sig": crypto.b64u(sig)}


def verify_object(pub: bytes, signed: object, kind: str) -> dict[str, Any] | None:
    """The object ``o`` if ``signed`` is exactly ``{"o", "sig"}``, ``o`` has ``v: 2``, our suite and ``kind``, and the
    signature verifies under ``pub`` for ``kind``'s label; ``None`` otherwise (protocol §2.4: checked before
    anything else)."""
    try:
        if type(signed) is not dict or set(signed) != {"o", "sig"}:
            return None
        o = signed["o"]
        if type(o) is not dict or o.get("v") != 2 or o.get("suite") != crypto.SUITE_ID or o.get("kind") != kind:
            return None
        sig = crypto.unb64u(signed["sig"], crypto.SIG_LEN)
        return o if crypto.verify(pub, sig, _bytes(kind, o)) else None
    except (crypto.EncodingError, canon.HashError, KeyError, TypeError):
        return None


# --- scopes -------------------------------------------------------------------------------------------------------


def scopes_ok(scopes: object) -> bool:
    """§6.1: a non-empty prefix of ``look, decide, operate, type``, or exactly one ``drop:<space hex>``."""
    if type(scopes) is not list or not scopes:
        return False
    if len(scopes) == 1 and type(scopes[0]) is str and _DROP_SCOPE.fullmatch(scopes[0]):
        return True
    return scopes == SCOPE_ORDER[: len(scopes)]


def person_scope_ok(cert_o: Mapping[str, Any], *, needs_operate: bool = False) -> bool:
    """Ticket-format §5.3: a device signs person events only with a level list that contains ``decide`` (and
    ``operate`` for member, policy, settings, grant, addon and ``restore`` events). ``drop:`` never."""
    scopes = cert_o.get("scopes_max")
    if type(scopes) is not list or not scopes or any(type(s) is not str or s.startswith("drop:") for s in scopes):
        return False
    return scopes_ok(scopes) and "decide" in scopes and (not needs_operate or "operate" in scopes)


# --- device certificates -------------------------------------------------------------------------------------------


def check_cert(signed: object, pk_pub: bytes) -> dict[str, Any]:
    """§6.1 without the clock: signature under ``pk_pub``, exact field set, valid keys, ids bound to the keys, scopes,
    ``expires_ms`` after ``created_ms``, a ``drop:`` certificate always expiring. Raises ``cert_invalid``."""
    o = verify_object(pk_pub, signed, "device_cert")
    if o is None or set(o) != _CERT_FIELDS:
        raise Refused("cert_invalid", "signature or field set")
    try:
        sig_pub = crypto.validate_public_key(crypto.unb64u(o["dk_sig_pub"], crypto.PUB_LEN))
        crypto.validate_public_key(crypto.unb64u(o["dk_kx_pub"], crypto.PUB_LEN))
        crypto.unb64u(o["label_sealed"])
        if crypto.unhex(o["device_id"], 16) != crypto.device_id(sig_pub):
            raise Refused("cert_invalid", "device_id is not of dk_sig_pub")
        if crypto.unhex(o["person_id"], 16) != crypto.person_id(pk_pub):
            raise Refused("cert_invalid", "person_id is not of the signer")
    except (crypto.EncodingError, crypto.CryptoError) as e:
        raise Refused("cert_invalid", str(e)) from None
    created, exp, scopes = o["created_ms"], o["expires_ms"], o["scopes_max"]
    if type(created) is not int or not scopes_ok(scopes):
        raise Refused("cert_invalid", "created_ms or scopes_max")
    if exp is not None and (type(exp) is not int or exp <= created):
        raise Refused("cert_invalid", "expires_ms")
    if scopes[0].startswith("drop:") and exp is None:
        raise Refused("cert_invalid", "a scoped agent device always expires")
    return o


def verify_cert(signed: object, pk_pub: bytes, now_ms: int, revoked: Collection[str] = frozenset()) -> dict[str, Any]:
    """§6.1 at time ``now_ms``: :func:`check_cert`, then ``created_ms > now + 300 s`` is ``cert_invalid``,
    ``now >= expires_ms`` is ``cert_expired``, a known revocation of ``device_id`` is ``revoked``."""
    o = check_cert(signed, pk_pub)
    if o["created_ms"] > now_ms + SKEW_MS:
        raise Refused("cert_invalid", "created in the future")
    if o["expires_ms"] is not None and now_ms >= o["expires_ms"]:
        raise Refused("cert_expired")
    if o["device_id"] in revoked:
        raise Refused("revoked")
    return o


def make_device_cert(
    pk_pub: bytes,
    sign: Signer,
    *,
    dk_sig_pub: bytes,
    dk_kx_pub: bytes,
    label_sealed: bytes,
    created_ms: int,
    expires_ms: int | None,
    scopes_max: list[str],
) -> dict[str, Any]:
    """Build and sign a device certificate with the person key (``sign`` is the PK's custody signer). The result is
    re-checked with :func:`check_cert` so a bad input is refused here, not at the first reader."""
    o = {
        "v": 2,
        "suite": crypto.SUITE_ID,
        "kind": "device_cert",
        "device_id": crypto.device_id(dk_sig_pub).hex(),
        "person_id": crypto.person_id(pk_pub).hex(),
        "dk_sig_pub": crypto.b64u(dk_sig_pub),
        "dk_kx_pub": crypto.b64u(dk_kx_pub),
        "label_sealed": crypto.b64u(label_sealed),
        "created_ms": created_ms,
        "expires_ms": expires_ms,
        "scopes_max": list(scopes_max),
    }
    signed = sign_object(o, sign)
    check_cert(signed, pk_pub)
    return signed


# --- sealed device labels (§5.4) -----------------------------------------------------------------------------------


def _label_key(pvk: bytes, person_id: bytes) -> bytes:
    return crypto.hkdf(pvk, b"", f"orch/v2/label|{person_id.hex()}".encode("ascii"))


def _label_aad(device_id: bytes) -> bytes:
    return L["aad_label"] + bytes([crypto.SUITE_ID]) + device_id


def seal_device_label(pvk: bytes, person_id: bytes, device_id: bytes, label: str) -> bytes:
    """``SALTED-AEAD(HKDF(PVK, "", "orch/v2/label|" person), "orch/v2/label-msg", aad, UTF-8(label))``."""
    return crypto.salted_seal(
        _label_key(pvk, person_id), L["kdf_label_msg"], _label_aad(device_id), label.encode("utf-8")
    )


def open_device_label(pvk: bytes, person_id: bytes, device_id: bytes, blob: bytes) -> str:
    return crypto.salted_open(_label_key(pvk, person_id), L["kdf_label_msg"], _label_aad(device_id), blob).decode(
        "utf-8"
    )


# --- revocation (§6.2) ----------------------------------------------------------------------------------------------


def make_revocation(pk_pub: bytes, sign: Signer, *, device_id_hex: str, revoked_ms: int, reason: str) -> dict[str, Any]:
    if reason not in REVOKE_REASONS:
        raise Refused("malformed", "reason")
    o = {
        "v": 2,
        "suite": crypto.SUITE_ID,
        "kind": "revocation",
        "person_id": crypto.person_id(pk_pub).hex(),
        "device_id": device_id_hex,
        "revoked_ms": revoked_ms,
        "reason": reason,
    }
    signed = sign_object(o, sign)
    check_revocation(signed, pk_pub)
    return signed


def check_revocation(signed: object, pk_pub: bytes) -> dict[str, Any]:
    """Protocol §6.2 checks that need no certificate, in the host's order: signature under the person key
    (``bad_signature``), exact field set and reason (``malformed``), ``person_id`` is that person's
    (``other_person``)."""
    o = verify_object(pk_pub, signed, "revocation")
    if o is None:
        raise Refused("bad_signature")
    if set(o) != _REVOCATION_FIELDS or o["reason"] not in REVOKE_REASONS or type(o["revoked_ms"]) is not int:
        raise Refused("malformed")
    try:
        crypto.unhex(o["device_id"], 16)
        crypto.unhex(o["person_id"], 16)
    except crypto.EncodingError:
        raise Refused("malformed") from None
    if o["person_id"] != crypto.person_id(pk_pub).hex():
        raise Refused("other_person")
    return o


def verify_revocation(signed: object, pk_pub: bytes, cert_o: Mapping[str, Any] | None) -> dict[str, Any]:
    """:func:`check_revocation`, then the device's certificate must name the same person and device
    (``other_person`` otherwise, or when no certificate is known)."""
    o = check_revocation(signed, pk_pub)
    if cert_o is None or cert_o.get("person_id") != o["person_id"] or cert_o.get("device_id") != o["device_id"]:
        raise Refused("other_person")
    return o


# --- workspace delegation (§7.1) ------------------------------------------------------------------------------------


def make_delegation(
    pk_pub: bytes,
    sign: Signer,
    *,
    workspace_id: str,
    wsk_pub: bytes,
    client_hosted: bool,
    issued_ms: int,
) -> dict[str, Any]:
    """The one-time delegation the owner's person key signs when the workspace is created."""
    o = {
        "v": 2,
        "suite": crypto.SUITE_ID,
        "kind": "ws_delegation",
        "workspace_id": workspace_id,
        "wsk_pub": crypto.b64u(crypto.validate_public_key(wsk_pub)),
        "owner_person_id": crypto.person_id(pk_pub).hex(),
        "client_hosted": client_hosted,
        "issued_ms": issued_ms,
    }
    signed = sign_object(o, sign)
    check_delegation(signed, pk_pub)
    return signed


def check_delegation(signed: object, owner_pk_pub: bytes) -> dict[str, Any]:
    """§7.1 steps 3: signature (``bad_signature``), exact field set (``malformed``), owner is that person
    (``other_person``)."""
    o = verify_object(owner_pk_pub, signed, "ws_delegation")
    if o is None:
        raise Refused("bad_signature")
    if set(o) != _DELEGATION_FIELDS or type(o["client_hosted"]) is not bool or type(o["issued_ms"]) is not int:
        raise Refused("malformed")
    try:
        crypto.unhex(o["workspace_id"], 16)
        crypto.validate_public_key(crypto.unb64u(o["wsk_pub"], crypto.PUB_LEN))
    except (crypto.EncodingError, crypto.CryptoError):
        raise Refused("malformed") from None
    if o["owner_person_id"] != crypto.person_id(owner_pk_pub).hex():
        raise Refused("other_person")
    return o


def delegation_binds(
    delegation_o: Mapping[str, Any], *, workspace_id: str, wsk_pub_b64u: str, owner_person_id: str
) -> None:
    """The delegation's workspace, ``wsk_pub`` and owner equal the card's / genesis event's
    (``delegation_mismatch``)."""
    if (
        delegation_o.get("workspace_id") != workspace_id
        or delegation_o.get("wsk_pub") != wsk_pub_b64u
        or delegation_o.get("owner_person_id") != owner_person_id
    ):
        raise Refused("delegation_mismatch")
