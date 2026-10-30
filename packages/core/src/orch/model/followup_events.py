"""The one event shape a follow-up can wait for (#717), and the hook ids of the webhook doorbell.

Pure: no clock, no I/O. An event is ``{source, kind, ref, status, at}`` with closed lists per source; nothing else of
the thing that happened (a webhook body, a ledger text) is ever part of it, so nothing else can reach an agent.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Mapping
from typing import Any

SOURCES = ("orch", "webhook")  # "addon" is reserved for #718 and refused by the model
#: closed kind -> statuses, per source
KINDS: dict[str, dict[str, tuple[str, ...]]] = {
    "orch": {
        "question": ("asked", "answered"),
        "ticket": ("changed", "closed"),
        "gate": ("approved", "changes_requested"),
        "task": ("done",),
    },
    "webhook": {"hook": ("rang",)},
}
REF_RE = re.compile(r"[A-Za-z0-9._:-]{0,64}")
HOOK_RE = re.compile(r"hk_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
HOOK_LABEL = b"orch/v2/hook|"

#: ledger event type -> (kind, status, field holding the ref)
_LEDGER = {
    "question.asked": ("question", "asked", "question"),
    "question.answered": ("question", "answered", "question"),
    "status.changed": ("ticket", "changed", "to"),
    "ticket.closed": ("ticket", "closed", "resolution"),
    "gate.approved": ("gate", "approved", "gate"),
    "gate.changes_requested": ("gate", "changes_requested", "gate"),
    "task.done": ("task", "done", "task"),
}


#: ref formats an add-on's ``events`` may declare (addon-api 1.2); each yields text that also fits ``REF_RE``
_OWNER = r"[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}"
_REPO = r"[A-Za-z0-9._-]{1,100}"
REF_FORMATS: dict[str, re.Pattern[str]] = {
    "int": re.compile(r"[0-9]{1,19}"),
    "slug": re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}"),
    "owner_repo_number": re.compile(rf"{_OWNER}:{_REPO}:[1-9][0-9]{{0,8}}"),
    "hex40": re.compile(r"[0-9a-f]{40}"),
}


def ref_fits(fmt: str, ref: Any) -> bool:
    """True when ``ref`` is exactly a ``fmt`` reference, at most 64 characters, and also a plain event ref."""
    pat = REF_FORMATS.get(fmt) if isinstance(fmt, str) else None
    if pat is None or not isinstance(ref, str) or len(ref) > 64 or ".." in ref:
        return False
    if fmt == "owner_repo_number" and ref.split(":")[1] in (".", ".."):
        return False
    return pat.fullmatch(ref) is not None and REF_RE.fullmatch(ref) is not None


def allowed_text() -> str:
    return ", ".join(f"{k}.{s}" for k, ss in KINDS["orch"].items() for s in ss) + ", webhook:<hook id>"


def new_hook_id() -> str:
    n = secrets.randbits(128)
    return "hk_" + "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


def secret_key(hook_id: str) -> str:
    """The workspace-free digest the host keeps an HMAC secret under (host-local file only)."""
    if not HOOK_RE.fullmatch(hook_id or ""):
        raise ValueError("not a webhook id")
    return hashlib.sha256(HOOK_LABEL + hook_id.encode()).hexdigest()


def hook_hash(hook_id: str, workspace_id: str) -> str:
    """H(label | workspace id | hook id): the same address hashes differently in every workspace (B3)."""
    if not HOOK_RE.fullmatch(hook_id or "") or not workspace_id:
        raise ValueError("not a webhook id")
    return hashlib.sha256(HOOK_LABEL + workspace_id.encode() + b"|" + hook_id.encode()).hexdigest()


def _clean_ref(ref: Any) -> str:
    ref = "" if ref is None else str(ref)
    return ref if REF_RE.fullmatch(ref) else ""


def parse_on(text: str, workspace_id: str = "") -> dict[str, str]:
    """``question.answered``, ``ticket.changed:testing``, ``gate.approved:requirements`` or ``webhook:<hook id>``."""
    text = (text or "").strip()
    if text.startswith("webhook:"):
        return {
            "source": "webhook",
            "kind": "hook",
            "status": "rang",
            "hook": hook_hash(text.removeprefix("webhook:"), workspace_id),
        }
    head, _, ref = text.partition(":")
    kind, _, status = head.partition(".")
    if status not in KINDS["orch"].get(kind, ()):
        raise ValueError(f"unknown event '{head}'; use one of: {allowed_text()}")
    if ref and not REF_RE.fullmatch(ref):
        raise ValueError("the reference after ':' may use letters, digits and . _ : - (64 characters at most)")
    match = {"source": "orch", "kind": kind, "status": status}
    if ref:
        match["ref"] = ref
    return match


def valid_match(match: Any) -> bool:
    if not isinstance(match, Mapping) or match.get("source") not in SOURCES:
        return False
    if match.get("status") not in KINDS[match["source"]].get(match.get("kind"), ()):
        return False
    if match["source"] == "webhook":
        return isinstance(match.get("hook"), str) and len(match["hook"]) == 64 and "ref" not in match
    return "hook" not in match and REF_RE.fullmatch(match.get("ref", "")) is not None


def valid_shape(event: Any) -> bool:
    return (
        isinstance(event, Mapping)
        and set(event) == {"source", "kind", "ref", "status", "at"}
        and event["source"] in SOURCES
        and event["status"] in KINDS[event["source"]].get(event["kind"], ())
        and isinstance(event["ref"], str)
        and REF_RE.fullmatch(event["ref"]) is not None
        and isinstance(event["at"], str)
    )


def matches(match: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    """Equal on source, kind and status, and on ref when the match names one (a webhook match names the hook hash)."""
    if not valid_shape(event) or not valid_match(match):
        return False
    if any(match[k] != event[k] for k in ("source", "kind", "status")):
        return False
    want = match["hook"][:8] if match["source"] == "webhook" else match.get("ref")
    return want is None or want == event["ref"]


def from_ledger(event: Mapping[str, Any]) -> dict[str, str] | None:
    """The shape of a ticket-log event, or ``None`` when it is not one a follow-up can wait for."""
    row = _LEDGER.get(event.get("type", ""))
    if row is None:
        return None
    kind, status, key = row
    ref = event.get(key)
    if isinstance(ref, Mapping):  # question.asked carries the whole question; only its id is the ref
        ref = ref.get("id")
    return {"source": "orch", "kind": kind, "ref": _clean_ref(ref), "status": status, "at": event["at"]}


def webhook_event(hash_: str, at: str) -> dict[str, str]:
    # the ref is a short prefix only: a reader of the ledger or a view never sees the whole hash
    return {"source": "webhook", "kind": "hook", "ref": hash_[:8], "status": "rang", "at": at}


FALLBACK_TEXT = {
    "no_event": "the event it waited for did not happen before the deadline",
    "no_ring": "the webhook did not ring before the deadline",
    "addon_silent": "the add-on did not report before the deadline",
}


def wake_line(trigger: str, fallback: str | None, event: Mapping[str, Any] | None) -> str | None:
    """The one line `follow-up show` puts before the brief; built only from closed values."""
    if trigger == "deadline" and fallback in FALLBACK_TEXT:
        return f"woken by the deadline, not the event: {FALLBACK_TEXT[fallback]} ({fallback}); check by hand"
    if event and valid_shape(event):
        ref = f" {event['ref']}" if event["ref"] and event["source"] == "orch" else ""
        return f"woken by {event['source']} event {event['kind']}.{event['status']}{ref} at {event['at']}"
    return None
