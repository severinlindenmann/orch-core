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

    def verify_host(self, event: Mapping[str, Any], *, log: str, wsk_pub: bytes | None) -> bool:
        """``host_sig`` of an event of ``log`` (``WORKSPACE`` or the ticket uid) under the workspace key ``wsk_pub``
        (raw bytes, taken from the replayed genesis). ``wsk_pub`` is ``None`` only for the genesis event itself,
        which carries its own key."""

    def verify_embedded(self, event: Mapping[str, Any]) -> bool:
        """The signed objects an event carries (genesis delegation and ``device_cert``, ``member.added``
        ``device_cert``, ``device.added`` ``cert``, ``device.revoked`` ``revocation``). ``False`` for an event
        that carries none or an unknown type."""


@dataclass
class FakeVerifier:
    """Accepts every signature; ``bad_person``/``bad_host``/``bad_embedded`` are sets of event ids that fail."""

    bad_person: set[str] = field(default_factory=set)
    bad_host: set[str] = field(default_factory=set)
    bad_embedded: set[str] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)
    host_calls: list[tuple[str, str, bytes | None]] = field(default_factory=list)  # (event id, log, wsk_pub)

    def verify_person(self, event: Mapping[str, Any], context: SigContext) -> bool:
        self.calls.append(("person", event["id"]))
        return event["id"] not in self.bad_person

    def verify_host(self, event: Mapping[str, Any], *, log: str, wsk_pub: bytes | None) -> bool:
        self.calls.append(("host", event["id"]))
        self.host_calls.append((event["id"], log, wsk_pub))
        return event["id"] not in self.bad_host

    def verify_embedded(self, event: Mapping[str, Any]) -> bool:
        self.calls.append(("embedded", event["id"]))
        return event["id"] not in self.bad_embedded
