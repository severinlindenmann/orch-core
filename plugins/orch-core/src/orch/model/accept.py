"""Pure manual-acceptance labels, bound to the criterion's exact current wording."""

from __future__ import annotations

import hashlib
import re

_LABEL = re.compile(r"accepted (AC[1-9][0-9]*) ([0-9a-f]{16})")


def criterion_hash(ac_id: str, text: str) -> str:
    return hashlib.sha256(b"orch.accept.v1\n" + ac_id.encode("ascii") + b"\n" + text.encode("utf-8")).hexdigest()[:16]


def label(ac_id: str, text: str) -> str:
    return f"accepted {ac_id} {criterion_hash(ac_id, text)}"


def parse_label(value: str | None) -> tuple[str, str] | None:
    match = _LABEL.fullmatch(value) if isinstance(value, str) else None
    return (match[1], match[2]) if match else None


def parse_withdraw_label(value: str | None) -> str | None:
    match = re.fullmatch(r"withdrawn (AC[1-9][0-9]*)", value) if isinstance(value, str) else None
    return match[1] if match else None


def withdraw_label(ac_id: str) -> str:
    return f"withdrawn {ac_id}"


#: the ticket statuses in which a person's manual acceptance counts (the host handler refuses the others too)
WORKABLE = ("open", "in_progress", "testing")


def may_accept(ws, t, person: str) -> bool:
    """Who may accept a criterion manually or mark a task done without a receipt (R3 owner decision): the ticket
    owner, a ticket reviewer, a workspace owner or maintainer; never a viewer, never a non-member. The model applies
    it to the event itself, so a signed event of anyone else never counts, whatever a host or a client allowed."""
    from . import claims

    member = ws.members.get(person)
    if member is None or member.role == "viewer":
        return False
    return claims.may_manage(ws, t, person) or person in t.people["reviewers"]
