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
        ``False`` for an event that carries none or an unknown type."""
