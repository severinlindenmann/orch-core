"""DEV ONLY, never shipped: the review maths of host contract v1.4 section 0.8.1, in one place.

The stub host and the stub signer both import this, but each recomputes from what it received: the signer trusts
nothing the host sends (the digest, the fingerprint and every `Review` are recomputed and compared).

* `cj` is canonical JSON (RFC 8785 on the restricted subset): `orch.canon.jcs.dumps`.
* `review_digest = "sha256:" + sha256(cj(material))`.
* `fingerprint` = the first 12 hex digits of
  `sha256(cj({"op": ..., "review_digest": ..., "events": [each event without sig, actor.device, auth (and, for
  grant.issued, secret_hash)]}))`.
* `Review.payload` is the event minus the envelope keys; `derived` is computed by the signer from event and material.
"""

import copy
import hashlib
from typing import Any

from orch import canon

ENVELOPE = ("v", "id", "type", "actor", "auth", "hash_v", "roster_v", "based_on", "sig")
# `gate_gen` leaves the payload on the three decision types only (shown on its own line); elsewhere it stays in it.
GATE_GEN_ENVELOPE = ("gate.approved", "gate.changes_requested", "verdict.given")
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
    "section.set": ("ticket.updated",),
    "new": ("ticket.created",),
    # Workspace operations (M4): one event each, in the workspace log.
    "settings.set": ("settings.changed",),
    "policy.set": ("policy.changed",),
    "member.add": ("member.added",),
    "member.role": ("role.changed",),
    "member.remove": ("member.removed",),
    "device.remove": ("device.removed",),
    "grant.revoke": ("grant.revoked",),
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


def fingerprint(review_digest: str, events: list[dict[str, Any]], op: str) -> str:
    """hex(sha256(cj({op, review_digest, events: [V(e)]})))[:12] (host contract 0.8.1, H5a)."""
    body = {"op": op, "review_digest": review_digest, "events": [stripped(e) for e in events]}
    return hashlib.sha256(cj(body)).hexdigest()[:12]


def derived_of(event: dict[str, Any], material: dict[str, Any]) -> dict[str, Any]:
    typ = event["type"]
    if typ in ("member.added", "workspace.created", "device.added"):
        # From the embedded certificate, as the real signer works them out (`orch.host.review`).
        o = (event.get("device_cert" if typ != "device.added" else "cert") or {}).get("o") or {}
        return {
            "cert_device": "d_" + str(o["device_id"]),
            "cert_person": "p_" + str(o["person_id"]),
            "cert_scopes_max": list(o["scopes_max"]),
            "cert_expires": o.get("expires_ms"),
        }
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
    skip = ENVELOPE + (("gate_gen",) if event["type"] in GATE_GEN_ENVELOPE else ())
    payload = {k: copy.deepcopy(v) for k, v in event.items() if k not in skip}
    return {"event_id": event["id"], "type": event["type"], "payload": payload, "derived": derived_of(event, material)}


def reviews_of(events: list[dict[str, Any]], material: dict[str, Any]) -> list[dict[str, Any]]:
    return [review_of(e, material) for e in events]


#: kind -> (required keys, allowed keys) of `Material` (host contract 0.8.1): a signer refuses any other top-level key.
MATERIAL_KEYS = {
    "gate": ({"kind", "gate", "input", "policy", "texts", "ticket", "review"},) * 2,
    "question": ({"kind", "ticket", "question", "review"},) * 2,
    "payload": ({"kind", "ticket", "review"}, {"kind", "ticket", "review", "texts", "before", "sequence"}),
}


def material_keys_ok(material: dict[str, Any]) -> bool:
    keys = MATERIAL_KEYS.get(material.get("kind"))
    return keys is not None and keys[0] <= set(material) <= keys[1]
