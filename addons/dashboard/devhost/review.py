"""DEV ONLY, never shipped: the review maths of host contract v1.4 section 0.8.1, in one place.

The stub host and the stub signer both import this, but each recomputes from what it received: the signer trusts
nothing the host sends (the digest, the fingerprint and every `Review` are recomputed and compared).

* `cj` is canonical JSON (RFC 8785 on the restricted subset): `orch.canon.jcs.dumps`.
* `review_digest = "sha256:" + sha256(cj(material))`.
* `fingerprint` = the first 12 hex digits of `sha256(cj({"review_digest": ..., "events": [each event without sig,
  actor.device, auth (and, for grant.issued, secret_hash)]}))`.
* `Review.payload` is the event minus the envelope keys; `derived` is computed by the signer from event and material.
"""

import copy
import hashlib
from typing import Any

from orch import canon

ENVELOPE = ("v", "id", "type", "actor", "auth", "hash_v", "roster_v", "based_on", "gate_gen", "sig")
DECISIONS = ("gate.approved", "gate.changes_requested", "verdict.given", "question.answered")
SECTIONED = ("ticket.updated", "ticket.created", "question.asked")
# The frozen operation -> event types table (F1 10.7); the dev stub serves only these person operations.
EMITS = {
    "approve": ("gate.approved",),
    "request_changes": ("gate.changes_requested",),
    "verdict": ("verdict.given",),
    "answer": ("question.answered",),
    "close": ("ticket.closed",),
    "reopen": ("ticket.reopened",),
    "log": ("log.added",),
    "set": ("ticket.updated",),
    "new": ("ticket.created",),
}


def cj(value: Any) -> bytes:
    return canon.jcs.dumps(value)


def digest_of(value: Any) -> str:
    return "sha256:" + hashlib.sha256(cj(value)).hexdigest()


def stripped(event: dict[str, Any]) -> dict[str, Any]:
    """An event as the fingerprint covers it: without `sig`, `actor.device` and `auth` (and a grant's `secret_hash`)."""
    out = copy.deepcopy(event)
    out.pop("sig", None)
    out.pop("auth", None)
    if isinstance(out.get("actor"), dict):
        out["actor"].pop("device", None)
    if out.get("type") == "grant.issued":
        out.pop("secret_hash", None)
    return out


def fingerprint(review_digest: str, events: list[dict[str, Any]]) -> str:
    body = {"review_digest": review_digest, "events": [stripped(e) for e in events]}
    return hashlib.sha256(cj(body)).hexdigest()[:12]


def derived_of(event: dict[str, Any], material: dict[str, Any]) -> dict[str, Any]:
    typ = event["type"]
    if typ in DECISIONS:
        return {"ticket_key": (material.get("ticket") or {}).get("key")}
    if typ in SECTIONED:
        texts = material.get("texts") or {}
        sections = event.get("sections") or {}
        return {
            "sections": {
                sid: {"text": texts.get(sid), "hash": entry["hash"] if isinstance(entry, dict) else None}
                for sid, entry in sections.items()
            }
        }
    return {}


def review_of(event: dict[str, Any], material: dict[str, Any]) -> dict[str, Any]:
    payload = {k: copy.deepcopy(v) for k, v in event.items() if k not in ENVELOPE}
    return {"event_id": event["id"], "type": event["type"], "payload": payload, "derived": derived_of(event, material)}


def reviews_of(events: list[dict[str, Any]], material: dict[str, Any]) -> list[dict[str, Any]]:
    return [review_of(e, material) for e in events]
