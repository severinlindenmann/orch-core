"""`human.prepare` / `human.get` / `human.pending` results as the UI's hand-off types (design appendix B).

The server does not trust the host's derived values: it recomputes `review_digest` and the 12-hex fingerprint from
`material` and `events` (host contract v1.4 section 0.8.1) and refuses an answer that disagrees. The browser recomputes
them once more. The preview is only ever `material`; nothing here reads a cached ticket.
"""

import hashlib
import json
import re

from ..errors import fail

REQUEST_ID = re.compile(r"^hr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
CODE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$")
FINGERPRINT = re.compile(r"^[0-9a-f]{12}$")
HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
STATES = ("prepared", "bound", "submitted", "cancelled", "expired")
GATE_EVENTS = ("gate.approved", "gate.changes_requested", "verdict.given")


def cj(value) -> bytes:
    """Canonical JSON on the restricted subset (integers, ASCII keys): equal to RFC 8785 there."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def review_digest(material: dict) -> str:
    return "sha256:" + hashlib.sha256(cj(material)).hexdigest()


def stripped(event: dict) -> dict:
    out = {k: v for k, v in event.items() if k not in ("sig", "auth")}
    if isinstance(out.get("actor"), dict):
        out["actor"] = {k: v for k, v in out["actor"].items() if k != "device"}
    if out.get("type") == "grant.issued":
        out.pop("secret_hash", None)
    return out


def fingerprint(digest: str, events: list) -> str:
    return hashlib.sha256(cj({"review_digest": digest, "events": [stripped(e) for e in events]})).hexdigest()[:12]


def inconsistent():
    return fail("internal", "The host's sign request does not match its own hashes.", exit=1)


def request(prepared: dict, ws: str) -> dict:
    """A `Prepared` as the dashboard's `HandoffRequest`. Raises `internal` if the host's hashes do not recompute."""
    try:
        info, material, events = prepared["request"], prepared["material"], prepared["events"]
        if (
            not isinstance(info, dict)
            or not REQUEST_ID.fullmatch(info["id"])
            or not (info["code"] is None or CODE.fullmatch(info["code"]))
            or info["state"] not in STATES
            or prepared["workspace_id"] != ws
            or not isinstance(events, list)
            or not 1 <= len(events) <= 16
            or not isinstance(material, dict)
            or not HASH.fullmatch(prepared["review_digest"])
            or not FINGERPRINT.fullmatch(prepared["fingerprint"])
        ):
            raise inconsistent()
        digest = review_digest(material)
        if digest != prepared["review_digest"] or fingerprint(digest, events) != prepared["fingerprint"]:
            raise inconsistent()
        first = events[0]
        return {
            "id": info["id"],
            "code": info["code"],
            "expires_at": info["expires_at"],
            "state": info["state"],
            "op": prepared["op"],
            "key": (material.get("ticket") or {}).get("key"),
            "fingerprint": prepared["fingerprint"],
            "review_digest": digest,
            "gate_hash": first.get("hash") if first.get("type") in GATE_EVENTS else None,
            "material": material,
            "events": events,
            "origin": prepared.get("origin"),
        }
    except (KeyError, TypeError, ValueError):
        raise inconsistent() from None


def state(row: dict) -> dict:
    """One `human.pending` row as the polled `HandoffState`."""
    return {
        "id": row["id"],
        "state": row["state"],
        "op": row["op"],
        "key": row.get("key"),
        "expires_at": row["expires_at"],
        "event_ids": list(row.get("event_ids") or []),
        "seq": row.get("seq"),
    }
