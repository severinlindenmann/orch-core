"""`human.prepare` / `human.get` / `human.pending` results as the UI's hand-off types (design appendix B).

The server does not trust the host's derived values: it recomputes `review_digest` and the 12-hex fingerprint from
`material` and `events` (host contract v1.4 section 0.8.1) and refuses an answer that disagrees. The browser recomputes
them once more. The preview is only ever `material`; nothing here reads a cached ticket.
"""

import hashlib
import json
import re
import time
from datetime import UTC, datetime

from ..errors import fail

REQUEST_ID = re.compile(r"^hr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
CODE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$")
FINGERPRINT = re.compile(r"^[0-9a-f]{12}$")
HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
STATES = ("prepared", "bound", "submitted", "cancelled", "expired")
ENVELOPE = ("v", "id", "type", "actor", "auth", "hash_v", "roster_v", "based_on", "sig")
TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
MAX_LIFE = 900  # the host lets a request live 10 minutes; anything much longer is not a request of this host
#: kind -> (required keys, allowed keys): core signer `_MATERIAL_KEYS` for these kinds (a test keeps them equal)
MATERIAL_KEYS = {
    "gate": (
        {"kind", "gate", "input", "policy", "texts", "ticket", "review"},
        {"kind", "gate", "input", "policy", "texts", "ticket", "review", "manual_acceptances"},
    ),
    "question": ({"kind", "ticket", "question", "review"},) * 2,
    "payload": (
        {"kind", "ticket", "review"},
        {
            "kind",
            "ticket",
            "review",
            "texts",
            "before",
            "sequence",
            "statement",
            "grants",
            "market",
            "deps",
            "removed_device",
        },
    ),
}
#: the only ops whose material may carry a manual acceptance `statement` (core signer `_person_op`)
STATEMENT_OPS = ("ac.accept", "ac.unaccept")
GATE_EVENTS = ("gate.approved", "gate.changes_requested", "verdict.given")
#: the hub events whose ``owner_proof`` the signer fills after the review (core review.OWNER_PROOF_TYPES): the host's
#: prepared event still carries ``owner_proof: null``, the review payload and the fingerprint leave it out
OWNER_PROOF_TYPES = ("hub.joined", "hub.left")


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
    if out.get("type") in OWNER_PROOF_TYPES and out.get("owner_proof") is None:
        out.pop("owner_proof", None)  # only the host's null placeholder; a filled value stays and fails the review
    return out


def fingerprint(digest: str, events: list, op: str) -> str:
    """The first 12 hex of sha256(cj({op, review_digest, events: [V(e)]})) (host contract 0.8.1)."""
    return hashlib.sha256(cj({"op": op, "review_digest": digest, "events": [stripped(e) for e in events]})).hexdigest()[
        :12
    ]


def inconsistent():
    return fail("internal", "The host's sign request does not match its own hashes.", exit=1)


def expires_ok(value, open_request: bool = True) -> bool:
    if not isinstance(value, str) or not TIME.fullmatch(value):
        return False
    if not open_request:
        return True  # an ended request keeps the time it had; only its shape is checked
    end = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    return time.time() - 60 < end < time.time() + MAX_LIFE


def _manual_row_ok(row) -> bool:
    return isinstance(row, dict) and set(row) == {"ac", "name", "by"} and all(isinstance(v, str) for v in row.values())


def material_keys_ok(material: dict) -> bool:
    """Fail closed on a top-level key outside the `Material` schema (host contract 0.8.1)."""
    keys = MATERIAL_KEYS.get(material.get("kind"))
    if keys is None or not keys[0] <= set(material) <= keys[1]:
        return False
    if "manual_acceptances" in material:  # host-told rows of a verify gate, closed as the signer closes them
        manual = material["manual_acceptances"]
        return (
            material.get("gate") == "verify" and isinstance(manual, list) and all(_manual_row_ok(row) for row in manual)
        )
    return True


def review_matches(material: dict, events: list) -> bool:
    """Each review block describes exactly its event: same id and type, and payload = event minus envelope."""
    reviews = material.get("review")
    if not isinstance(reviews, list) or len(reviews) != len(events):
        return False
    for block, event in zip(reviews, events, strict=True):
        skip = ENVELOPE + (("gate_gen",) if event.get("type") in GATE_EVENTS else ())
        if event.get("type") in OWNER_PROOF_TYPES and event.get("owner_proof") is None:
            skip += ("owner_proof",)
        payload = {k: v for k, v in event.items() if k not in skip}
        if (
            block.get("event_id") != event.get("id")
            or block.get("type") != event.get("type")
            or block.get("payload") != payload
        ):
            return False
    return True


def request(
    prepared: dict,
    ws: str,
    expect_op: str | None = None,
    expect_key: str | None = None,
    expect_events=None,
    expect_material=None,
) -> dict:
    """A `Prepared` as the dashboard's `HandoffRequest`. Raises `internal` if the host's answer is not the request that
    was asked for, or its hashes or its review do not recompute (fail closed)."""
    try:
        info, material, events = prepared["request"], prepared["material"], prepared["events"]
        if (
            (expect_op is not None and prepared["op"] != expect_op)
            or (expect_key is not None and (material.get("ticket") or {}).get("key") != expect_key)
            or not isinstance(info, dict)
            # A retry with the same idem returns the request in its CURRENT state (host contract 0.7): open, or ended.
            or info["state"] not in STATES
            or not expires_ok(info["expires_at"], info["state"] in ("prepared", "bound"))
            or not isinstance(events, list)
            or not all(isinstance(e, dict) for e in events)
            or not isinstance(material, dict)
            or not material_keys_ok(material)
            # a statement only on a manual acceptance whose action checks it against what was asked (expect_material)
            or ("statement" in material and (prepared["op"] not in STATEMENT_OPS or expect_material is None))
            or not review_matches(material, events)
            or (expect_events is not None and not expect_events(events))
            or (expect_material is not None and not expect_material(material, events))
        ):
            raise inconsistent()
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
        if (
            digest != prepared["review_digest"]
            or fingerprint(digest, events, prepared["op"]) != prepared["fingerprint"]
        ):
            raise inconsistent()
        first = events[0]
        from orch.host import signcontent, signer

        try:  # B6: the plain view, derived from this very material and these events, only after the signer's own
            # material checks (gate, policy, question and section hashes, every `before` against base_rev) hold on them
            checked = {"op": prepared["op"], "log": prepared["log"], "workspace_id": ws, **prepared}
            signer.verify_material(checked, workspace_id=ws)
            content = signcontent.clean_tree(signcontent.build(checked, verified=True))
        except (signer.SignerRefused, KeyError, TypeError, ValueError, AttributeError):
            content = None  # the UI falls back to the host's material preview, as before
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
            **({"headline": content["headline"], "content": content} if content is not None else {}),
            "material": material,
            "events": events,
            "origin": prepared.get("origin"),
        }
    except (KeyError, TypeError, ValueError, AttributeError):
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
        # v1.5, optional: why a signer's submit failed (a catalog code); absent is not an error.
        "error": {"code": row["error"]["code"]}
        if isinstance(row.get("error"), dict) and re.fullmatch(r"[a-z_]+(\.[a-z_]+)*", str(row["error"].get("code")))
        else None,
    }
