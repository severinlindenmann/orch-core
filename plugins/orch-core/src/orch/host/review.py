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
    "LEVEL_MAX",
    "OWNER_PROOF_LINE",
    "OWNER_PROOF_TYPES",
    "READER_TYPES",
    "PERSON_AGENT_OPS",
    "events_fit",
    "person_op_types",
    "ReviewError",
    "derived",
    "fingerprint",
    "fingerprint_view",
    "of",
    "payload",
    "reader_line",
    "review_digest",
    "sha256_cj",
]

#: the envelope keys a review payload leaves out (§0.8.1)
ENVELOPE = frozenset({"v", "id", "type", "actor", "auth", "hash_v", "roster_v", "based_on", "sig"})
#: #1198 C9: the hub events whose ``owner_proof`` the signer fills after the review (like a grant's ``secret_hash``).
#: It is not in the review payload or the fingerprint; the review shows a derived line instead.
OWNER_PROOF_TYPES = frozenset({"hub.joined", "hub.left"})
OWNER_PROOF_LINE = "signed with your backup key"
#: the decision types whose ``gate_gen`` is an envelope key of the review: shown on its own line, labelled a host
#: attestation (v1.5 "Bound hashes"); on any other type an unexpected ``gate_gen`` stays in the payload, so it is shown
GATE_TYPES = frozenset({"gate.approved", "gate.changes_requested", "verdict.given"})
_DECISIONS = frozenset({"gate.approved", "gate.changes_requested", "verdict.given", "question.answered"})
_CERTS = {
    "member.added": "device_cert", "workspace.created": "device_cert", "device.added": "cert", "device.rotated": "cert",
}  # fmt: skip
_SECTIONS = frozenset({"ticket.updated", "ticket.created", "question.asked"})


#: agent ops a person may run through ``human.prepare`` (§0.8 "Person versions of agent ops"), each allowed only while
#: every type it emits passes ``orch.ops.actors.allows("human", t)``
PERSON_AGENT_OPS = frozenset(
    {
        "new",
        "set",
        "section.set",
        "ac.add",
        "ac.edit",
        "task.add",
        "task.edit",
        "log",
        "ask",
        "artifact.add",
        "artifact.replace",
    }
)
#: person ops of the host contract (§0.8 ``move``, ``policy.set``, ``settings.set``; H0 records them as ops). Each emits
#: one existing person-only F1 event; they are not registry (CLI) ops and not in the frozen grant-verb table
#: ``EMITS_V1``: a person event is never covered by a grant verb, so no table version changes (H6b).
HOST_PERSON_OPS: dict[str, frozenset[str]] = {
    "ac.accept": frozenset({"artifact.added"}),
    "ac.unaccept": frozenset({"artifact.replaced"}),
    "task.mark_done": frozenset({"task.done"}),
    "move": frozenset({"ticket.moved"}),
    "policy.set": frozenset({"policy.changed"}),
    "settings.set": frozenset({"settings.changed"}),
    # B12D: one role's people of a ticket (the sequence engine's last steps; also a plain person op)
    "people.set": frozenset({"people.changed"}),
    # H12c-e (11A.15.10 item 12): the paired phone's device.added, with its own material kind
    # P5: a person's signature over the exact words of one staged share (a log.added note on its ticket)
    "publish.share": frozenset({"log.added"}),
    # V3 C2a (hub contract 9.1): the owner's record that the workspace joined a hub selectively, or left it
    "hub.join": frozenset({"hub.joined"}),
    "hub.mode": frozenset({"hub.joined"}),  # V3 C2b: selective to full, the owner's own signature
    "hub.leave": frozenset({"hub.left"}),
    "device.mark_exposed": frozenset({"device.exposed"}),  # #1097: written before a key enters convenience custody
    "device.rotate": frozenset({"device.rotated"}),  # #1097: the new device replaces the old (recovery code)
    "device.remove": frozenset({"device.removed"}),  # #1198 C15: one click, with lockout guards
}
_NOT_PERSON = frozenset({"import.v1"})  # import v1 is direct mode


def person_op_types(op: str) -> frozenset[str] | None:
    """The event types the person op ``op`` may emit (``CURRENT[op]``, F1 §10.7), or ``None`` when
    ``op`` is not an op a person runs through ``human.prepare``."""
    from orch.model.emits import CURRENT, HUMAN_ONLY
    from orch.ops.actors import allows

    if op in HOST_PERSON_OPS:  # not in EMITS_V1 (a grant verb never covers a person event): the host's own table
        return HOST_PERSON_OPS[op]
    types = CURRENT.get(op)
    if types is None or op in _NOT_PERSON:
        return None
    if op in HUMAN_ONLY:
        return types
    if op in PERSON_AGENT_OPS and all(allows("human", t) for t in types):
        return types
    return None


def events_fit(op: str, types: list[str]) -> bool:
    """The number and order of a request's event types fit ``op``: one event for a human-only op or a host person op,
    a ticket.created first (and only first) for ``new``, 1 to 16 events otherwise, each of a type ``op`` emits."""
    from orch.model.emits import HUMAN_ONLY

    allowed = person_op_types(op)
    if allowed is None or not 1 <= len(types) <= 16 or any(t not in allowed for t in types):
        return False
    if op == "grant":  # B8: Extend = the new runner grant, then the revoke of the one it replaces
        return types in (["grant.issued"], ["grant.issued", "grant.revoked"])
    if op in HUMAN_ONLY or op in HOST_PERSON_OPS:
        return len(types) == 1
    if op == "new":
        return types == ["ticket.created"]
    return True


#: H12d-2 (§0.8.6 item 9.11): the event types that add a reader; their ``derived`` carries ``reader``, the level of
#: the declaration the host verified (0: none). It is a host attestation the signer shows but cannot recompute (the
#: declaration is an argument, not material), bound into ``review_digest`` like every review block; a signer that
#: does not know the key refuses the review it cannot reproduce (update orch)
READER_TYPES = frozenset({"member.added", "device.added"})
LEVEL_MAX = 2**31 - 1


def reader_line(level: int) -> str:
    """The review line of a reader-adding event (§0.8.6 item 9.11)."""
    if level >= 1:
        return f"Reader: understands convenience signing (level {level}) (host attestation of its declaration)"
    return "Reader: old (level 0) (host attestation: no declaration of level 1)"


class ReviewError(ValueError):
    """The material does not back an event: the signer refuses before any prompt."""


def sha256_cj(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canon.dumps(value)).hexdigest()


def payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """The event without the envelope keys: every other key, verbatim."""
    gate = event.get("type") in GATE_TYPES
    proof = event.get("type") in OWNER_PROOF_TYPES
    return {
        k: copy.deepcopy(v)
        for k, v in event.items()
        if k not in ENVELOPE and not (gate and k == "gate_gen") and not (proof and k == "owner_proof")
    }


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
    if typ in ("artifact.added", "artifact.replaced") and "statement" in material:
        from . import acceptance

        statement = material["statement"]
        if not isinstance(statement, str) or not acceptance.matches(dict(event), statement):
            raise ReviewError("the statement is not the one this event signs")
        return {"statement": statement}
    if typ in OWNER_PROOF_TYPES:
        return {"owner_proof": OWNER_PROOF_LINE}
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
    ``secret_hash`` and, for ``hub.joined`` / ``hub.left``, ``owner_proof`` (the fields a signer fills in)."""
    out = {k: copy.deepcopy(v) for k, v in event.items() if k not in ("sig", "auth")}
    if isinstance(out.get("actor"), dict):
        out["actor"] = {k: v for k, v in out["actor"].items() if k != "device"}
    if out.get("type") == "grant.issued":
        out.pop("secret_hash", None)
    if out.get("type") in OWNER_PROOF_TYPES:
        out.pop("owner_proof", None)
    return out


def fingerprint(digest: str, events: list[Mapping[str, Any]], op: str) -> str:
    """The first 12 hex digits of ``sha256(cj({op, review_digest, events}))``: the op is bound too (two ops emitting
    the same ``ticket.updated`` have different fingerprints)."""
    body = {"op": op, "review_digest": digest, "events": [fingerprint_view(e) for e in events]}
    return hashlib.sha256(canon.dumps(body)).hexdigest()[:12]
