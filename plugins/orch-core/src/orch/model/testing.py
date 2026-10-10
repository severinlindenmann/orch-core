"""Test doubles for the injected verifier. Never import this from production code."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .verifier import SigContext


@dataclass
class FakeVerifier:
    """Accepts every signature; ``bad_person``/``bad_host``/``bad_embedded`` are sets of event ids that fail."""

    bad_person: set[str] = field(default_factory=set)
    bad_host: set[str] = field(default_factory=set)
    bad_embedded: set[str] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)
    embedded_keys: list[tuple[str, str]] = field(default_factory=list)
    host_calls: list[tuple[str, str, bytes | None]] = field(default_factory=list)  # (event id, log, wsk_pub)

    def verify_person(self, event: Mapping[str, Any], context: SigContext) -> bool:
        self.calls.append(("person", event["id"]))
        return event["id"] not in self.bad_person

    def verify_host(self, event: Mapping[str, Any], *, log: str, wsk_pub: bytes | None) -> bool:
        self.calls.append(("host", event["id"]))
        self.host_calls.append((event["id"], log, wsk_pub))
        return event["id"] not in self.bad_host

    def verify_embedded(self, event: Mapping[str, Any], *, pk_pub: str) -> bool:
        self.calls.append(("embedded", event["id"]))
        self.embedded_keys.append((event["id"], pk_pub))
        return event["id"] not in self.bad_embedded
