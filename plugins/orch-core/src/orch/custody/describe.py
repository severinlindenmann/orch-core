"""What a person sees before signing, derived from the bytes that are signed (D41: "a prompt naming the action and
hash"; security review R3). Never from caller text.

:func:`describe_payload` parses the signing bytes (``label || cj(...)``), and returns ``(field, value)`` pairs whose
*names* come from the fixed lists here, never from the payload; the values are raw and are escaped once, in
:func:`orch.custody.passphrase.render_prompt`.
"""

from __future__ import annotations

from typing import Any

from orch import canon
from orch.crypto import labels as protocol_labels

__all__ = ["describe_payload"]

_EVENT_FIELDS = (
    "type",
    "auth",
    "gate",
    "hash",
    "gate_gen",
    "task",
    "ticket",
    "question",
    "answer",
    "outcome",
    "resolution",
    "person",
    "role",
    "device",
    "scope",
    "verbs",
    "hours",
    "grant",
    "receipt",
    "source",
    "sha",
    "commit",
    "diffstat",
    "branch",
    "repo",
    "name",
    "reason",
)
_OBJECT_FIELDS = (
    "kind",
    "device_id",
    "person_id",
    "workspace_id",
    "scopes_max",
    "expires_ms",
    "created_ms",
    "revoked_ms",
    "reason",
    "client_hosted",
    "owner_person_id",
)


def _label_names() -> dict[bytes, str]:
    names = {
        protocol_labels.L[k]: k.removeprefix("sig_").replace("_", "-") for k in protocol_labels.SIGNATURE_LABEL_KEYS
    }
    for k, v in canon.LABELS.items():
        if k.startswith("sig_"):
            names[v.encode("ascii")] = k.removeprefix("sig_").replace("_", "-")
    return names


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return canon.dumps(value).decode("utf-8")
    except Exception:  # noqa: BLE001 - display only; an unserialisable value is shown as its type
        return f"<{type(value).__name__}>"


def describe_payload(payload: bytes) -> list[tuple[str, str]]:
    """Fixed-name fields of the signing bytes; always starts with ``signs`` (the label's name)."""
    names = _label_names()
    label = next((lab for lab in names if payload.startswith(lab)), None)
    if label is None:
        return [("signs", "unknown label")]
    out = [("signs", names[label])]
    try:
        body = canon.loads_strict(payload[len(label) :])
    except Exception:  # noqa: BLE001
        return [*out, ("payload", "not parseable")]
    if not isinstance(body, dict):
        return [*out, ("payload", "not an object")]
    if isinstance(body.get("event"), dict):
        event = body["event"]
        out += [("workspace", _text(body.get("workspace_id"))), ("log", _text(body.get("log")))]
        out += [(f, _text(event[f])) for f in _EVENT_FIELDS if f in event]
    else:
        out += [(f, _text(body[f])) for f in _OBJECT_FIELDS if f in body]
    return out
