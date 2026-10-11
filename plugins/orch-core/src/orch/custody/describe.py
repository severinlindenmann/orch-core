"""What a person sees before signing, derived from the bytes that are signed (D41; security review R3 and the
signing-prompt-omission finding). Never from caller text, and **fail closed**: a payload this module does not fully
understand raises :class:`CustodyError` and no prompt is shown.

:func:`describe_payload` parses the signing bytes (``label || cj(...)``) and returns ``(name, value)`` lines. Names
are fixed strings from the tables here, or a fixed field name plus a path inside the value (``source[1].sha``); values
are raw and escaped once in :func:`orch.custody.passphrase.render_prompt`.

Rules that keep it complete:

* Every person-signable event type has an explicit table of its payload fields (:data:`EVENT_FIELDS`). *Every* field
  of the table that is present is shown; a payload field that is in neither the table nor the envelope list
  (:data:`ENVELOPE`) refuses (nothing can be left out). Nested values are flattened to one line per leaf; more than
  :data:`MAX_LINES` lines refuses rather than truncating. Embedded signed objects (``delegation``, ``device_cert``,
  ``cert``, ``revocation``) are shown by their decisive fields.
* Always shown: the label name, workspace, log (ticket uid or ``workspace``), event ``type``, ``auth``, ``roster_v``,
  and the actor's person and device.
* A ticket-format §5.4 diffstat is not part of any signed payload, so it cannot be shown from the signed bytes; the
  source list (every repo, ref and sha) and the gate hash that bind the decision are.
* Event types an agent or the host appends (``task.*``, ``claim.*``, ``artifact.*``, ``edit.external`` ...) are never
  signed by a person and refuse here, as does any unknown type, label or shape.
"""

from __future__ import annotations

from typing import Any

from orch import canon
from orch.crypto import labels as protocol_labels

from .base import CustodyError

__all__ = ["ENVELOPE", "EVENT_FIELDS", "MAX_LINES", "describe_payload"]

MAX_LINES = 60
ENVELOPE = frozenset({"v", "id", "type", "actor", "based_on", "hash_v", "auth", "roster_v"})

_GATE = ("gate", "gate_gen", "hash", "policy_hash")
EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    # ticket log, actor P
    "ticket.closed": ("resolution", "duplicate_of", "text"),
    "ticket.reopened": ("text",),
    "visibility.changed": ("visibility",),
    "people.changed": ("role", "add", "remove"),
    "policy.changed": ("gates",),
    "question.answered": ("question", "hash", "option", "text"),
    "gate.approved": (*_GATE, "source_sha"),
    "gate.changes_requested": (*_GATE, "text"),
    "verdict.given": ("outcome", "gate_gen", "hash", "policy_hash", "source_sha", "text"),
    # both logs
    "restore": ("from_seq", "head", "abandoned", "abandoned_decisions", "reason"),
    "invalid.acknowledged": ("invalid_seq", "invalid_head", "reason"),
    # workspace log
    "workspace.created": ("workspace_id", "prefix", "host_id", "wsk_pub", "owner", "delegation", "device_cert"),
    "member.added": ("person", "name", "role", "pk_pub", "device_cert"),
    "member.removed": ("person",),
    "role.changed": ("person", "role"),
    "device.added": ("device", "cert"),
    "device.removed": ("device", "reason"),
    "device.revoked": ("device", "reason", "revocation"),
    "settings.changed": ("set",),
    "grant.issued": ("grant", "scope", "verbs", "issued_at", "hours", "expires_at", "secret_hash", "label"),
    "grant.revoked": ("grant", "reason"),
    "addon.granted": ("name", "version", "package_sha256", "capabilities", "fields", "sections", "artifact_kinds"),
    "addon.disabled": ("name",),
    "addon.purged": ("name",),
}
_WORKSPACE_ONLY = frozenset(
    {
        "workspace.created",
        "member.added",
        "member.removed",
        "role.changed",
        "device.added",
        "device.removed",
        "device.revoked",
        "settings.changed",
        "grant.issued",
        "grant.revoked",
        "addon.granted",
        "addon.disabled",
        "addon.purged",
    }
)
_TICKET_ONLY = frozenset(set(EVENT_FIELDS) - _WORKSPACE_ONLY - {"restore", "invalid.acknowledged", "policy.changed"})
_SIGNED_OBJECT_FIELDS: dict[str, tuple[str, ...]] = {
    "device_cert": (
        "device_id",
        "person_id",
        "dk_sig_pub",
        "dk_kx_pub",
        "created_ms",
        "expires_ms",
        "scopes_max",
    ),
    "revocation": ("person_id", "device_id", "revoked_ms", "reason"),
    "ws_delegation": ("workspace_id", "wsk_pub", "owner_person_id", "client_hosted", "issued_ms"),
}
_OBJECT_LABEL_KIND = {
    "sig_device_cert": "device_cert",
    "sig_revocation": "revocation",
    "sig_ws_delegation": "ws_delegation",
}
_EMBEDDED = {
    "delegation": "ws_delegation",
    "device_cert": "device_cert",
    "cert": "device_cert",
    "revocation": "revocation",
}


def _refuse(why: str):
    raise CustodyError(f"refusing to prompt: {why}")


def _label_names() -> dict[bytes, str]:
    names = {protocol_labels.L[k]: k for k in protocol_labels.SIGNATURE_LABEL_KEYS}
    for k, v in canon.LABELS.items():
        if k.startswith("sig_"):
            names[v.encode("ascii")] = k
    return names


def _scalar(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int)):
        return canon.dumps(value).decode("ascii")
    _refuse(f"unexpected value type {type(value).__name__}")
    return ""


def _flatten(name: str, value: Any, out: list[tuple[str, str]], depth: int = 0) -> None:
    if depth > 6:
        _refuse("value nested too deeply")
    if isinstance(value, dict):
        if not value:
            out.append((name, "{}"))
        for k in sorted(value):
            if not isinstance(k, str):
                _refuse("non-text key")
            _flatten(f"{name}.{k}", value[k], out, depth + 1)
    elif isinstance(value, list):
        if not value:
            out.append((name, "[]"))
        for i, item in enumerate(value, 1):
            _flatten(f"{name}[{i}]", item, out, depth + 1)
    else:
        out.append((name, _scalar(value)))


def _object_lines(prefix: str, kind: str, signed: Any, out: list[tuple[str, str]]) -> None:
    """The decisive fields of an embedded signed object; refuses a different shape or an unexpected field."""
    if not isinstance(signed, dict) or set(signed) != {"o", "sig"} or not isinstance(signed["o"], dict):
        _refuse(f"{prefix} is not a signed object")
    o = signed["o"]
    allowed = set(_SIGNED_OBJECT_FIELDS[kind]) | {"v", "suite", "kind", "label_sealed"}
    if o.get("kind") != kind or set(o) - allowed:
        _refuse(f"{prefix} is not a plain {kind}")
    for f in _SIGNED_OBJECT_FIELDS[kind]:
        if f in o:
            _flatten(f"{prefix}.{f}", o[f], out)


def _event_lines(body: Any) -> list[tuple[str, str]]:
    if (
        not isinstance(body, dict)
        or set(body) != {"contract", "suite", "workspace_id", "log", "event"}
        or body["contract"] != 1
        or body["suite"] != 2
        or not isinstance(body["event"], dict)
    ):
        _refuse("not a contract-1 signed event")
    ev, log = body["event"], body["log"]
    etype = ev.get("type")
    if type(etype) is not str or etype not in EVENT_FIELDS:
        _refuse(f"event type {etype!r} is not one a person signs")
    if etype in _WORKSPACE_ONLY and log != "workspace":
        _refuse(f"event type {etype} belongs in the workspace log")
    if etype in _TICKET_ONLY and log == "workspace":
        _refuse(f"event type {etype} belongs in a ticket log")
    extra = set(ev) - ENVELOPE - set(EVENT_FIELDS[etype])
    if extra:
        _refuse(f"{etype} carries fields the prompt does not show: {sorted(extra)[:3]}")
    actor = ev.get("actor")
    if not isinstance(actor, dict) or set(actor) != {"kind", "id", "device"} or actor["kind"] != "person":
        _refuse("actor must be exactly {kind: person, id, device}")
    out: list[tuple[str, str]] = [
        ("workspace", _scalar(body["workspace_id"])),
        ("log", _scalar(log)),
        ("type", etype),
        ("auth", _scalar(ev.get("auth"))),
        ("roster_v", _scalar(ev.get("roster_v"))),
        ("actor.person", _scalar(actor.get("id"))),
        ("actor.device", _scalar(actor.get("device"))),
    ]
    for field in EVENT_FIELDS[etype]:
        if field not in ev:
            continue
        if field in _EMBEDDED:
            _object_lines(field, _EMBEDDED[field], ev[field], out)
        elif field == "owner" and isinstance(ev[field], dict):
            if set(ev[field]) - {"person", "name", "pk_pub"}:
                _refuse("owner has unexpected fields")
            _flatten(field, ev[field], out)
        else:
            _flatten(field, ev[field], out)
    return out


def describe_payload(payload: bytes) -> list[tuple[str, str]]:
    """Fixed-name lines for the signing bytes, starting with ``signs``; raises :class:`CustodyError` for anything not
    fully understood (unknown label, unparseable body, unknown event type, unshown field, too many lines)."""
    names = _label_names()
    label = next((lab for lab in names if payload.startswith(lab)), None)
    if label is None:
        _refuse("unknown signature label")
    key = names[label]
    raw = payload[len(label) :]
    try:
        body = canon.loads_strict(raw)
        canonical = canon.cj_checked(body) == raw
    except Exception:  # noqa: BLE001
        _refuse("signing bytes are not canonical JSON")
    if not canonical:
        _refuse("signing bytes are not the canonical form of what they say")
    out: list[tuple[str, str]] = [("signs", key.removeprefix("sig_").replace("_", "-"))]
    if key in ("sig_ticket_event", "sig_ws_event"):
        out += _event_lines(body)
        if (key == "sig_ws_event") != (body["log"] == "workspace"):
            _refuse("label and log disagree")
    elif key in _OBJECT_LABEL_KIND:
        kind = _OBJECT_LABEL_KIND[key]
        if (
            not isinstance(body, dict)
            or body.get("kind") != kind
            or set(body)
            - set(_SIGNED_OBJECT_FIELDS[kind])
            - {
                "v",
                "suite",
                "kind",
                "label_sealed",
            }
        ):
            _refuse(f"not a plain {kind}")
        for f in _SIGNED_OBJECT_FIELDS[kind]:
            if f in body:
                _flatten(f, body[f], out)
    else:
        _refuse(f"{key.removeprefix('sig_')} signatures are not prompted for in P1")
    if len(out) > MAX_LINES:
        _refuse("too many lines to show faithfully")
    return out
