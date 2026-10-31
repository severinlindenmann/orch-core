"""R3 (#1591, design I5): the op kinds a remote-device stream may sign, in ONE table.

``classify(op, args)`` answers ``allowed`` (a click), ``presence`` (a click that also needs the device's presence
signature over this exact review, D88) or ``refused`` (never from a stream). A sequence is as strict as its strictest
step (:func:`sequence_class`). Anything not listed is refused, so a kind added later is refused until a row says
otherwise. The signer is the only place that decides (``ClickSigner`` stream branch); the dashboard imports nothing from
here to decide.

The table is closed and explicit on purpose: every person op (``review.person_op_types``), every ``CLICK_OPS`` entry
and every signer op has a row, and a test walks the catalog so a new op cannot be forgotten silently."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "ALLOWED", "PRESENCE", "REFUSED", "SIGNER_OPS", "TABLE", "classify", "secret_arg", "sequence_class", "strictest",
]  # fmt: skip

ALLOWED = "allowed"
PRESENCE = "presence"
REFUSED = "refused"
_RANK = {ALLOWED: 0, PRESENCE: 1, REFUSED: 2}

_ALLOWED_OPS = (
    "new", "set", "section.set", "ac.add", "ac.edit", "task.add", "task.edit", "log", "ask", "artifact.add",
    "artifact.replace", "follow_up.cancel", "schedule.cancel", "grant.revoke",
)  # fmt: skip
_PRESENCE_OPS = (
    "approve", "request_changes", "verdict", "answer", "close", "reopen", "move", "ac.accept", "ac.unaccept",
    "task.mark_done", "people.set", "grant", "schedule.add", "follow_up.reschedule", "follow_up.undo",
)  # fmt: skip
_REFUSED_OPS = (
    "member.add", "member.remove", "member.role", "policy.set", "settings.set",
    "device.mark_exposed", "device.rotate", "device.remove",
    "hub.join", "hub.mode", "hub.leave", "remote.set", "remote.device_confirm",
    "addon.grant", "addon.disable", "addon.purge", "publish.share", "app.release", "import.v1",
    # CLICK_OPS entries that are no person op of the host: explicit rows so the walk test sees them
    "apply", "claim", "handoff", "release", "submit",
    "task.block", "task.done", "task.reopen", "task.skip", "task.start",
)  # fmt: skip

#: op name -> class, for the person ops of the host
TABLE: dict[str, str] = {
    **dict.fromkeys(_ALLOWED_OPS, ALLOWED),
    **dict.fromkeys(_PRESENCE_OPS, PRESENCE),
    **dict.fromkeys(_REFUSED_OPS, REFUSED),
}
#: the families that are refused whatever the table says (an op added under one of them is refused too)
_REFUSED_FAMILIES = ("remote.", "device.", "hub.", "member.", "addon.", "import.")

#: the signer's own ops (``ClickSigner.handle``): the ONE allowlist of a stream: prepare, view, sign, cancel, status
#: (own workspace only) and stash; ``session_alive``, ``custody_*``, ``rotate``, ``runner``, ``github_token``,
#: ``stepup`` and ``cancel_holds`` are not
SIGNER_OPS: dict[str, str] = {
    "prepare": ALLOWED,
    "view": ALLOWED,
    "sign": ALLOWED,
    "cancel": ALLOWED,
    "status": ALLOWED,
    "session_alive": REFUSED,  # a lease of the preparing dashboard session; a stream has none
    "stash": ALLOWED,
    "custody_on": REFUSED,
    "custody_off": REFUSED,
    "rotate": REFUSED,
    "runner": REFUSED,
    "github_token": REFUSED,
    "stepup": REFUSED,
    "cancel_holds": REFUSED,
}

#: argument names that make a call a sealed, shown-once or deposited one: never from a stream
_SECRET_KEYS = ("sealed", "stepup", "reveal_key", "deposit", "reveal")


def strictest(*classes: str) -> str:
    """The strictest of the given classes (``refused`` > ``presence`` > ``allowed``); none given is ``allowed``."""
    out = ALLOWED
    for c in classes:
        if _RANK.get(c, _RANK[REFUSED]) > _RANK[out]:
            out = c if c in _RANK else REFUSED
    return out


def secret_arg(args: Mapping[str, Any] | None) -> bool:
    if not isinstance(args, Mapping):
        return False
    for k in _SECRET_KEYS:
        if k in args and args[k] not in (None, False):
            return True
    inner = args.get("args")
    return isinstance(inner, Mapping) and "sealed" in inner


def classify(op: Any, args: Mapping[str, Any] | None = None) -> str:
    """The class of one op with its call arguments (``deposit``, ``reveal``/``reveal_key`` and any ``sealed`` or
    ``stepup`` argument are read from ``args``). Unknown, malformed or secret-carrying: ``refused``."""
    if type(op) is not str:
        return REFUSED
    if secret_arg(args):
        return REFUSED
    if op.startswith(_REFUSED_FAMILIES):
        return REFUSED
    return TABLE.get(op, REFUSED)


def sequence_class(intent: Mapping[str, Any]) -> str:
    """The strictest class of the steps of a sequence intent; an intent the host's own reader cannot parse is
    refused. A grant step of a sequence is the agent start (presence)."""
    from orch.host import sequence

    try:
        steps = sequence.steps(intent)
    except Exception:  # noqa: BLE001 - fail closed on anything odd
        return REFUSED
    if not steps:
        return REFUSED
    return strictest(*(classify(s.get("op"), {"args": s.get("args")}) for s in steps))
