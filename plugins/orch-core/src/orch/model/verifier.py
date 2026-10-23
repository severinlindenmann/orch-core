"""The injected signature verifier (model/ does no crypto, ticket-format §5.3, §5.11).

The model decides *what* must be verified and under which certificate; the verifier (crypto/, C2) decides whether a
signature holds. :class:`FakeVerifier` accepts everything unless told otherwise, for tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class SigContext:
    """What a person signature is checked against: the certificate of the signing device (its ``dk_sig_pub``), the
    workspace id and the log (``"workspace"`` or the ticket uid) that go into the signed bytes (§5.3)."""

    workspace_id: str
    log: str
    cert: Mapping[str, Any]


class Verifier(Protocol):
    def genesis_failure(self, event: Mapping[str, Any]) -> str | None:
        """The code of the first failing check of §5.11 for a ``workspace.created`` (``genesis.bad_sig`` for check 6),
        or ``None`` when every check holds."""

    def verify_person(self, event: Mapping[str, Any], context: SigContext) -> bool:
        """``sig`` of a person event under ``context.cert``'s device signing key."""

    def verify_host(self, event: Mapping[str, Any], *, log: str, wsk_pub: bytes | None, workspace_id: str) -> bool:
        """``host_sig`` of an event of ``log`` (``WORKSPACE`` or the ticket uid) under the workspace key ``wsk_pub``
        (raw bytes, taken from the replayed genesis). ``wsk_pub`` is ``None`` only for the genesis event itself,
        which carries its own key. ``workspace_id`` is the replayed (or, for the genesis, the pinned) id, never read
        from the event."""

    def verify_embedded(
        self, event: Mapping[str, Any], *, pk_pub: str, device_cert: Mapping[str, Any] | None = None
    ) -> bool:
        """The signed objects an event carries (genesis delegation and ``device_cert``, ``member.added``
        ``device_cert``, ``device.added`` ``cert``, ``device.revoked`` ``revocation``) under the person key
        ``pk_pub`` that the *model* vouches for (the genesis owner key, the key in ``member.added``, or the member's
        key from the replayed member list): a key can't be recovered from ``device.added``/``device.revoked`` alone.
        ``device_cert`` is required for ``device.revoked``: the revoked device's certificate from the replayed roster.
        For a restore entry the model passes ``type: restore.revocation`` with ``revocation``, ``device_cert``
        and ``pk_pub``; verify both signatures and key bindings, without expiry or person-scope authorization.
        This evidence establishes a tombstone, never membership.
        ``False`` for an event that carries none or an unknown type."""

    def verify_custodial_sigs(self, event: Mapping[str, Any], *, workspace_id: str, log: str) -> bool:
        """C3b (§15.3 line checks): the two signatures of a hub-signed line, ``sig`` under ``uk_cert.o.uk_pub`` with the
        label of ``type`` and ``channel_proof`` under ``channel_cert.o.key_pub`` over the event digest. ``False`` also
        when a certificate or key cannot be read. Whether the certificates chain to the hub is authorization."""

    def verify_hub_pin(
        self,
        obj: Mapping[str, Any],
        *,
        root: str,
        card: Mapping[str, Any] | None,
        revoked: frozenset[str],
        snapshot: Mapping[str, Any] | None,
        workspace_id: str,
        at_ms: int,
        dev: bool,
    ) -> str | None:
        """C3b (§15.2 rules 2-4): the signed hub object ``obj`` (card, op revocation or snapshot) under the pinned
        ``root`` (``hk_root_pub``), against the ledger's ``card`` (signed), ``revoked`` op cert ids and pinned
        ``snapshot`` (signed), at ``at_ms`` (the line's ``at``, never a clock). ``None`` when it holds, else the hub
        contract's refusal code (``stale_card``, ``other_hub``, ``revoked``, ``stale_snapshot``, ...)."""

    def verify_hub_actor(
        self,
        event: Mapping[str, Any],
        *,
        workspace_id: str,
        log: str,
        root: str,
        card: Mapping[str, Any] | None,
        revoked: frozenset[str],
        snapshot: Mapping[str, Any] | None,
        revoked_channels: frozenset[str],
        at_ms: int,
        dev: bool,
        mode: str,
        member_role: str | None,
    ) -> str | None:
        """C3b (§15.3 steps 2, 3 and 5) for a hub-signed ``event``: the certificate chain through the ledger card
        (``no_card``, ``revoked``, ``cert_expired``, ``bad_signature``, ``stale_op_cert``, ``dev_*``, ``other_user``,
        ``factor_mismatch``), the pinned snapshot (``unknown_snapshot``), then the role and type
        (``channel_revoked``, ``forbidden_role``, ``not_granted``, ``not_custodial``, ``factor_too_weak``).
        ``member_role`` is the F1 role of the canonical person in selective mode. ``None`` when it holds, else the
        contract code."""


class RefuseHub:
    """Fail-closed defaults for the three C3b methods: a verifier that does not inherit real ones refuses every hub
    signature, pin and actor (D79: a workspace without hub wiring never admits hub material)."""

    __slots__ = ()

    def verify_custodial_sigs(self, event: Mapping[str, Any], *, workspace_id: str, log: str) -> bool:
        return False

    def verify_hub_pin(self, obj: Mapping[str, Any], **kw: Any) -> str | None:
        return "unavailable"

    def verify_hub_actor(self, event: Mapping[str, Any], **kw: Any) -> str | None:
        return "unavailable"
