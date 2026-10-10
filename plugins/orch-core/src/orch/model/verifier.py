"""The injected signature verifier (model/ does no crypto, ticket-format §5.3, §5.11).

The model decides *what* must be verified and under which certificate; the verifier (crypto/, C2) decides whether a
signature holds. :class:`FakeVerifier` accepts everything unless told otherwise, for tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class SigContext:
    """What a person signature is checked against: the certificate of the signing device (its ``dk_sig_pub``), the
    workspace id and the log (``"workspace"`` or the ticket uid) that go into the signed bytes (§5.3)."""

    workspace_id: str
    log: str
    cert: Mapping[str, Any]


class Verifier(Protocol):
    def verify_person(self, event: Mapping[str, Any], context: SigContext) -> bool:
        """``sig`` of a person event under ``context.cert``'s device signing key."""

    def verify_host(self, event: Mapping[str, Any]) -> bool:
        """``host_sig`` of an event under the workspace key."""

    def verify_embedded(self, event: Mapping[str, Any], pk_pub: str | None) -> bool:
        """The signed objects an event carries (genesis delegation and ``device_cert``, ``member.added``
        ``device_cert``, ``device.added`` ``cert``, ``device.revoked`` ``revocation``), under the person key
        ``pk_pub`` (the member's key, or the one the event itself introduces)."""


@dataclass
class FakeVerifier:
    """Accepts every signature; ``bad_person``/``bad_host``/``bad_embedded`` are sets of event ids that fail."""

    bad_person: set[str] = field(default_factory=set)
    bad_host: set[str] = field(default_factory=set)
    bad_embedded: set[str] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def verify_person(self, event: Mapping[str, Any], context: SigContext) -> bool:
        self.calls.append(("person", event["id"]))
        return event["id"] not in self.bad_person

    def verify_host(self, event: Mapping[str, Any]) -> bool:
        self.calls.append(("host", event["id"]))
        return event["id"] not in self.bad_host

    def verify_embedded(self, event: Mapping[str, Any], pk_pub: str | None) -> bool:
        self.calls.append(("embedded", event["id"]))
        return event["id"] not in self.bad_embedded
