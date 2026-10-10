"""The signature verifier that ``model/`` injects (ticket-format §5.3, §5.11): ``CryptoVerifier``.

The protocol it satisfies is defined by C4 (``orch.model.verifier.Verifier``, PR #347). It is repeated here so this
module imports nothing from ``model/`` at runtime::

    @dataclass(frozen=True)
    class SigContext:
        workspace_id: str; log: str; cert: Mapping[str, Any]      # the signing device's certificate

    class Verifier(Protocol):
        def verify_person(self, event, context: SigContext) -> bool: ...
        def verify_host(self, event) -> bool: ...
        def verify_embedded(self, event, pk_pub: str | None) -> bool: ...

``tests/identity/test_verifier.py::test_satisfies_c4_protocol`` runs against the real module once C4 is merged (it
skips until then). :meth:`verify_person` reads only ``context.workspace_id``, ``context.log`` and ``context.cert``, so
any object with those attributes works.

Decisions:

* Every method returns ``bool`` and never raises: a malformed event, key or signature is simply ``False``.
* ``verify_person`` checks the signature under the certificate's ``dk_sig_pub`` over
  :func:`orch.canon.person_signing_bytes`
  **and** that ``event["actor"]["device"] == "d_" + device_id(dk_sig_pub)``, so a certificate for another device can't
  stand in. ``context.cert`` may be the signed object ``{"o", "sig"}`` or its ``o``. The certificate's own signature,
  scopes, expiry and revocation at the event's position are ``model/``'s (it checks them with
  :func:`orch.identity.certs.check_cert` through ``verify_embedded`` and its own replay).
* ``verify_host`` needs the workspace key and the log the event belongs to, which the protocol's one-argument form
  doesn't carry: they are constructor state (``for_log`` makes a view per log). With ``log=None`` the log is
  ``"workspace"`` if the event has no ``ws_seq`` and unknown (``False``) if it has one, because the ticket uid
  can't be recovered from the event. ``wsk_pub=None`` is accepted only for the genesis event, which carries its own
  ``wsk_pub`` (bound by the delegation, checked by :func:`orch.identity.members.check_genesis`).
* ``verify_embedded`` verifies the signed objects an event carries under the person key: ``workspace.created``
  (delegation and ``device_cert`` under ``owner.pk_pub``, with the genesis bindings), ``member.added``
  (``device_cert`` under the event's ``pk_pub``), ``device.added`` (``cert`` under ``pk_pub``) and ``device.revoked``
  (``revocation`` under ``pk_pub``). When the event introduces the key itself and ``pk_pub`` is also given they must
  be equal. Other event types carry nothing embedded and pass.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orch import canon, crypto

from . import members
from .certs import check_cert, check_delegation, delegation_binds
from .errors import Refused

__all__ = ["CryptoVerifier"]

_EMBEDDED_TYPES = ("workspace.created", "member.added", "device.added", "device.revoked")


def _cert_o(cert: Mapping[str, Any]) -> Mapping[str, Any]:
    inner = cert.get("o")
    return inner if isinstance(inner, Mapping) and "dk_sig_pub" in inner else cert


class CryptoVerifier:
    def __init__(self, workspace_id: str, wsk_pub: bytes | str | None = None, *, log: str | None = None) -> None:
        self.workspace_id = workspace_id
        self.wsk_pub = crypto.unb64u(wsk_pub, crypto.PUB_LEN) if isinstance(wsk_pub, str) else wsk_pub
        if self.wsk_pub is not None:
            crypto.validate_public_key(self.wsk_pub)
        self.log = log

    def for_log(self, log: str) -> CryptoVerifier:
        """The same verifier bound to one log (``"workspace"`` or a ticket uid)."""
        return CryptoVerifier(self.workspace_id, self.wsk_pub, log=log)

    # -- person ---------------------------------------------------------------------------------------------------

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

    # -- host -----------------------------------------------------------------------------------------------------

    def verify_host(self, event: Mapping[str, Any]) -> bool:
        try:
            log = self.log
            if log is None:
                if "ws_seq" in event:
                    return False
                log = "workspace"
            wsk = self.wsk_pub
            if wsk is None:
                if event.get("type") != "workspace.created":
                    return False
                wsk = crypto.validate_public_key(crypto.unb64u(event["wsk_pub"], crypto.PUB_LEN))
            sig = crypto.unb64u(event["host_sig"], crypto.SIG_LEN)
            return crypto.verify(wsk, sig, canon.host_signing_bytes(self.workspace_id, log, event))
        except (crypto.EncodingError, crypto.CryptoError, canon.HashError, KeyError, TypeError, AttributeError):
            return False

    # -- embedded objects -----------------------------------------------------------------------------------------

    def verify_embedded(self, event: Mapping[str, Any], pk_pub: str | None) -> bool:
        try:
            etype = event.get("type")
            if etype not in _EMBEDDED_TYPES:
                return True
            given = crypto.validate_public_key(crypto.unb64u(pk_pub, crypto.PUB_LEN)) if pk_pub is not None else None
            if etype == "workspace.created":
                return self._embedded_genesis(event, given)
            introduced = event.get("pk_pub") if etype == "member.added" else None
            if introduced is not None:
                key = crypto.validate_public_key(crypto.unb64u(introduced, crypto.PUB_LEN))
                if given is not None and given != key:
                    return False
            else:
                key = given
            if key is None:
                return False
            if etype == "member.added":
                members.check_member_added(event)
            elif etype == "device.added":
                members.check_device_added(event, key)
            else:
                members.check_device_revoked(event, key)
            return True
        except (Refused, crypto.EncodingError, crypto.CryptoError, KeyError, TypeError, AttributeError):
            return False

    def _embedded_genesis(self, event: Mapping[str, Any], given: bytes | None) -> bool:
        owner = event["owner"]
        key = crypto.validate_public_key(crypto.unb64u(owner["pk_pub"], crypto.PUB_LEN))
        if given is not None and given != key:
            return False
        pid = crypto.person_id(key).hex()
        d_o = check_delegation(event["delegation"], key)
        delegation_binds(d_o, workspace_id=event["workspace_id"], wsk_pub_b64u=event["wsk_pub"], owner_person_id=pid)
        c_o = check_cert(event["device_cert"], key)
        return (
            owner["person"] == "p_" + pid
            and c_o["person_id"] == pid
            and event["actor"]["device"] == "d_" + c_o["device_id"]
            and event["workspace_id"] == self.workspace_id
        )
