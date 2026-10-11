"""The canonical review of a prepared human request (§0.8.1, D-P2-21): ``Review``, ``review_digest``, the fingerprint.

The host computes these when it prepares a request, and **every signer recomputes them** from the events and the
``material`` it received, refusing any difference (:mod:`orch.host.signer`). This module is pure: no store, no clock,
no host state. Both sides import it, so the rule is written once; what makes the signer independent is that it runs
the rule itself on what it received, never trusting a value the host computed.

* :func:`payload` is the event minus the envelope keys (:data:`ENVELOPE`), verbatim and complete: every payload key is
  shown, including keys a later F1 version adds (L2).
* :func:`derived` holds the values the signer computes itself, per event type (the frozen table of §0.8.1). It raises
  :class:`ReviewError` when the material does not back the event (a section text whose ``section_hash`` differs).
* :func:`review_digest` is ``sha256(cj(material))``; :func:`fingerprint` the first 12 hex digits of
  ``sha256(cj({op, review_digest, events}))``, each event without ``sig``, ``actor.device``, ``auth`` and, for
  ``grant.issued``, ``secret_hash`` (N2).
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from typing import Any

from orch import canon

__all__ = [
    "ENVELOPE",
    "GATE_TYPES",
    "PERSON_AGENT_OPS",
    "events_fit",
    "person_op_types",
    "ReviewError",
    "derived",
    "fingerprint",
    "fingerprint_view",
    "of",
    "payload",
    "review_digest",
    "sha256_cj",
]

#: the envelope keys a review payload leaves out (§0.8.1)
ENVELOPE = frozenset({"v", "id", "type", "actor", "auth", "hash_v", "roster_v", "based_on", "sig"})
#: the decision types whose ``gate_gen`` is an envelope key of the review: shown on its own line, labelled a host
#: attestation (v1.5 "Bound hashes"); on any other type an unexpected ``gate_gen`` stays in the payload, so it is shown
GATE_TYPES = frozenset({"gate.approved", "gate.changes_requested", "verdict.given"})
_DECISIONS = frozenset({"gate.approved", "gate.changes_requested", "verdict.given", "question.answered"})
_CERTS = {"member.added": "device_cert", "workspace.created": "device_cert", "device.added": "cert"}
_SECTIONS = frozenset({"ticket.updated", "ticket.created", "question.asked"})


#: agent ops a person may run through ``human.prepare`` (§0.8 "Person versions of agent ops"), each allowed only while
#: every type it emits passes ``orch.ops.actors.allows("human", t)``
PERSON_AGENT_OPS = frozenset(
    {"new", "set", "section.set", "ac.add", "ac.edit", "log", "ask", "artifact.add", "artifact.replace"}
)
_NOT_PERSON = frozenset({"init", "import.v1"})  # init is host.wsk_create + host.bootstrap; import v1 is direct mode


def person_op_types(op: str) -> frozenset[str] | None:
    """The event types the person op ``op`` may emit (``EMITS_V1[op]``, the frozen table, F1 §10.7), or ``None`` when
    ``op`` is not an op a person runs through ``human.prepare``."""
    from orch.model.emits import EMITS_V1, HUMAN_ONLY
    from orch.ops.actors import allows

    types = EMITS_V1.get(op)
    if types is None or op in _NOT_PERSON:
        return None
    if op in HUMAN_ONLY:
        return types
    if op in PERSON_AGENT_OPS and all(allows("human", t) for t in types):
        return types
    return None


def events_fit(op: str, types: list[str]) -> bool:
    """The number and order of a request's event types fit ``op``: one event for a human-only op, a ticket.created
    first (and only first) for ``new``, 1 to 16 events otherwise, each of a type ``op`` emits."""
    from orch.model.emits import HUMAN_ONLY

    allowed = person_op_types(op)
    if allowed is None or not 1 <= len(types) <= 16 or any(t not in allowed for t in types):
        return False
    if op in HUMAN_ONLY:
        return len(types) == 1
    if op == "new":
        return types == ["ticket.created"]
    return True


class ReviewError(ValueError):
    """The material does not back an event: the signer refuses before any prompt."""


def sha256_cj(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canon.dumps(value)).hexdigest()


def payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """The event without the envelope keys: every other key, verbatim."""
    gate = event.get("type") in GATE_TYPES
    return {k: copy.deepcopy(v) for k, v in event.items() if k not in ENVELOPE and not (gate and k == "gate_gen")}


def _cert(event: Mapping[str, Any], field: str) -> dict[str, Any]:
    signed = event.get(field)
    o = signed.get("o") if isinstance(signed, Mapping) else None
    if not isinstance(o, Mapping):
        raise ReviewError(f"{event.get('type')}: {field} is not a signed certificate")
    try:
        return {
            "cert_device": "d_" + str(o["device_id"]),
            "cert_person": "p_" + str(o["person_id"]),
            "cert_scopes_max": list(o["scopes_max"]),
            "cert_expires": o.get("expires_ms"),
        }
    except (KeyError, TypeError):
        raise ReviewError(f"{event.get('type')}: {field} lacks its decisive fields") from None


def _sections(event: Mapping[str, Any], material: Mapping[str, Any]) -> dict[str, Any]:
    texts = material.get("texts") or {}
    refs = event.get("sections") or {}
    if not isinstance(refs, Mapping):
        raise ReviewError("sections is not a map")
    out: dict[str, Any] = {}
    for sid, entry in sorted(refs.items()):
        want = entry.get("hash") if isinstance(entry, Mapping) else entry
        if sid not in texts or not isinstance(texts[sid], str):
            raise ReviewError(f"section {sid}: the material has no text for it")
        text = texts[sid]
        if canon.section_hash(text) != want:
            raise ReviewError(f"section {sid}: the text does not hash to the event's section_hash")
        out[sid] = {"text": text, "hash": want}
    return {"sections": out}


def derived(event: Mapping[str, Any], material: Mapping[str, Any]) -> dict[str, Any]:
    """The per-type ``derived`` values of §0.8.1, computed from the event and the material only."""
    typ = event.get("type")
    if typ in _CERTS:
        return _cert(event, _CERTS[typ])
    if typ in _DECISIONS:
        ticket = material.get("ticket")
        if not isinstance(ticket, Mapping) or not isinstance(ticket.get("key"), str):
            raise ReviewError(f"{typ}: the material names no ticket")
        return {"ticket_key": ticket["key"]}
    if typ in _SECTIONS:
        return _sections(event, material)
    return {}


def of(event: Mapping[str, Any], material: Mapping[str, Any]) -> dict[str, Any]:
    """``Review`` of one event: ``{event_id, type, payload, derived}``."""
    return {
        "event_id": event.get("id"),
        "type": event.get("type"),
        "payload": payload(event),
        "derived": derived(event, material),
    }


def review_digest(material: Mapping[str, Any]) -> str:
    return sha256_cj(material)


def fingerprint_view(event: Mapping[str, Any]) -> dict[str, Any]:
    """The event as the fingerprint covers it: without ``sig``, ``actor.device``, ``auth`` and, for ``grant.issued``,
    ``secret_hash`` (the fields a signer fills in)."""
    out = {k: copy.deepcopy(v) for k, v in event.items() if k not in ("sig", "auth")}
    if isinstance(out.get("actor"), dict):
        out["actor"] = {k: v for k, v in out["actor"].items() if k != "device"}
    if out.get("type") == "grant.issued":
        out.pop("secret_hash", None)
    return out


def fingerprint(digest: str, events: list[Mapping[str, Any]], op: str) -> str:
    """The first 12 hex digits of ``sha256(cj({op, review_digest, events}))``: the op is bound too (two ops emitting
    the same ``ticket.updated`` have different fingerprints)."""
    body = {"op": op, "review_digest": digest, "events": [fingerprint_view(e) for e in events]}
    return hashlib.sha256(canon.dumps(body)).hexdigest()[:12]
