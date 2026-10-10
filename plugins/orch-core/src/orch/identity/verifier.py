"""The signature verifier that ``model/`` injects (ticket-format §5.3, §5.11): :class:`CryptoVerifier`.

The protocol it satisfies is C4's ``orch.model.verifier.Verifier`` (PR #347), as amended by the C4 security rulings::

    class Verifier(Protocol):
        def verify_person(self, event, context: SigContext) -> bool: ...
        def verify_host(self, event, *, log: str, wsk_pub: bytes | None) -> bool: ...
        def verify_embedded(self, event) -> bool: ...

``SigContext`` has ``workspace_id``, ``log`` and ``cert`` (the signing device's certificate); this module reads only
those attributes, so it imports nothing from ``model/`` at runtime. ``tests/identity/test_verifier.py`` compares the
signatures with :func:`inspect.signature` now and runs against the real module once C4 is merged.

Decisions:

* **Stateless and immutable.** No constructor state: the workspace key comes from the replayed genesis through
  ``wsk_pub`` (``None`` only for the genesis event itself, which carries its own, bound by the PK-signed
  delegation).
* **``workspace_id``.** Ticket events do not carry the workspace id, but it is in the signed bytes (§5.5), so
  ``verify_host`` takes it as an optional keyword (taken from the event when it has one, as workspace-log
  events do; otherwise required; without it the event is ``False``). It is optional, so a call that follows the C4 interface is accepted.
* **Fail closed.** Every method returns ``bool`` and never raises; a malformed event, key or signature is ``False``.
  ``verify_embedded`` is ``True`` only for an event whose ``type`` is a known string that carries embedded objects and
  whose objects all verify. An event with a missing, non-string or unknown type is ``False``.
* ``verify_person`` checks the signature under the certificate's ``dk_sig_pub`` over
  :func:`orch.canon.person_signing_bytes` **and**
  that ``actor.device == "d_" + device_id(dk_sig_pub)``. ``context.cert``
  may be the signed object ``{"o", "sig"}`` or its ``o``. The certificate's own signature, scopes, expiry and
  revocation at the event's position are ``model/``'s.
* ``verify_embedded`` is self-contained for ``workspace.created`` (the one genesis implementation,
  :func:`orch.identity.members.check_genesis`, in F1 §5.11 order, including both signatures) and ``member.added`` (the
  event carries ``pk_pub``). ``device.added`` and ``device.revoked`` need the person's key, and ``device.revoked`` also
  the revoked device's certificate, which only the replayed state has: pass them as the keyword-only ``pk_pub`` and
  ``device_cert``. Without them these types are ``False``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orch import canon, crypto

from . import members
from .errors import Refused

__all__ = ["CryptoVerifier"]

_KNOWN = ("workspace.created", "member.added", "device.added", "device.revoked")


def _cert_o(cert: Mapping[str, Any]) -> Mapping[str, Any]:
    inner = cert.get("o")
    return inner if isinstance(inner, Mapping) and "dk_sig_pub" in inner else cert


def _key(value: object) -> bytes:
    if isinstance(value, str):
        value = crypto.unb64u(value, crypto.PUB_LEN)
    return crypto.validate_public_key(value)


class CryptoVerifier:
    __slots__ = ()

    def verify_person(self, event: Mapping[str, Any], context: Any) -> bool:
        try:
            o = _cert_o(context.cert)
            pub = crypto.validate_public_key(crypto.unb64u(o["dk_sig_pub"], crypto.PUB_LEN))
            actor = event["actor"]
            if actor.get("kind") != "person" or actor.get("device") != "d_" + crypto.device_id(pub).hex():
                return False
            sig = crypto.unb64u(event["sig"], crypto.SIG_LEN)
            msg = canon.person_signing_bytes(context.workspace_id, context.log, event)
            return crypto.verify(pub, sig, msg)
        except (crypto.EncodingError, crypto.CryptoError, canon.HashError, KeyError, TypeError, AttributeError):
            return False

    def verify_host(
        self, event: Mapping[str, Any], *, log: str, wsk_pub: bytes | None, workspace_id: str | None = None
    ) -> bool:
        try:
            if wsk_pub is None:
                if event.get("type") != "workspace.created" or log != "workspace":
                    return False
                wsk_pub = _key(event["wsk_pub"])
                workspace_id = event["workspace_id"]  # the genesis names its own workspace
            else:
                wsk_pub = _key(wsk_pub)
                if workspace_id is None:
                    workspace_id = event.get("workspace_id")  # only events that carry one (workspace log)
            sig = crypto.unb64u(event["host_sig"], crypto.SIG_LEN)
            return crypto.verify(wsk_pub, sig, canon.host_signing_bytes(workspace_id, log, event))
        except (crypto.EncodingError, crypto.CryptoError, canon.HashError, KeyError, TypeError, AttributeError):
            return False

    def verify_embedded(
        self,
        event: Mapping[str, Any],
        *,
        pk_pub: bytes | str | None = None,
        device_cert: Mapping[str, Any] | None = None,
    ) -> bool:
        try:
            etype = event.get("type")
            if type(etype) is not str or etype not in _KNOWN:
                return False
            given = _key(pk_pub) if pk_pub is not None else None
            if etype == "workspace.created":
                r = members.check_genesis(event)
                return given is None or given == r["owner_pk_pub"]
            if etype == "member.added":
                introduced = _key(event["pk_pub"])
                members.check_member_added(event)
                return given is None or given == introduced
            if given is None:
                return False
            if etype == "device.added":
                members.check_device_added(event, given)
            else:
                if device_cert is None:
                    return False
                members.check_device_revoked(event, given, _cert_o(device_cert))
            return True
        except (Refused, crypto.EncodingError, crypto.CryptoError, KeyError, TypeError, AttributeError):
            return False
