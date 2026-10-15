"""Gate policies (ticket-format §5.7): the effective policy is the intersection of workspace default and ticket
override, recomputed whenever either side changes, so an override can never end up looser.
"""

from __future__ import annotations

from typing import Any

from orch.canon import canonical_policy, policy_hash

from .codes import Code, Refusal
from .types import GATES, TCore, WsCore

TICKET_TYPES = ("feature", "bug", "chore", "spike", "epic")
WS_TOKENS = ("owner", "maintainer", "member")


def _applies_union(a: Any, b: Any) -> Any:
    if a == "all" or b == "all":
        return "all"
    if a == "off" and b == "off":
        return "off"
    items = {t for x in (a, b) if isinstance(x, list) for t in x}
    return sorted(items)


def intersect(ws: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """§5.7: ``approvers`` = workspace ∩ override, ``count`` the larger, ``not`` the union, ``independent`` either,
    ``applies`` the union (``all`` if either is, ``off`` only if both are, else the sorted list)."""
    if override is None:
        return canonical_policy(ws)
    return {
        "approvers": sorted(set(ws["approvers"]) & set(override["approvers"])),
        "count": max(ws["count"], override["count"]),
        "not": sorted(set(ws["not"]) | set(override["not"])),
        "applies": _applies_union(ws["applies"], override["applies"]),
        "independent": ws["independent"] or override["independent"],
    }


def effective(ws: WsCore, t: TCore, gate: str) -> dict[str, Any]:
    return intersect(ws.policies[gate], t.overrides.get(gate))


def effective_hash(ws: WsCore, t: TCore, gate: str) -> str:
    p = effective(ws, t, gate)
    # an emptied approver set can't be hashed (canonical form needs one token): the gate is blocked anyway
    return policy_hash(gate, p if p["approvers"] else {**p, "approvers": ["owner"]})


def applies_to(policy: dict[str, Any], ticket_type: str) -> bool:
    a = policy["applies"]
    return a == "all" or (a != "off" and ticket_type in a)


def applies(ws: WsCore, t: TCore, gate: str) -> bool:
    return applies_to(effective(ws, t, gate), t.ticket_type)


def blocked(ws: WsCore, t: TCore, gate: str) -> bool:
    """No approver token is left (``gate.no_eligible``, §5.7)."""
    return not effective(ws, t, gate)["approvers"]


def named_roles(policy: dict[str, Any]) -> set[str]:
    """The ticket roles the policy names (``approvers``, ``not``) plus ``assignees`` when ``independent``."""
    named = {x for x in (*policy["approvers"], *policy["not"]) if x not in WS_TOKENS}
    if policy["independent"]:
        named.add("assignees")
    return named


def check_change(gate: str, policy: dict[str, Any]) -> Refusal | None:
    """The host's rules for a policy in an event (the canonical form is the schema's): D59 for ``code``."""
    if gate not in GATES:
        return Refusal(Code.POLICY_INVALID, f"unknown gate {gate!r}")
    if gate == "code" and ("assignees" not in policy["not"] or policy["independent"] is not True):
        return Refusal(Code.POLICY_INVALID, "code: `not` must include assignees and independent must be true (D59)")
    return None


def check_override(ws: WsCore, t: TCore, gates: dict[str, Any]) -> Refusal | None:
    """A ticket override that leaves no approver token is refused (§5.7, ``gate.no_eligible``)."""
    for g, p in gates.items():
        if (r := check_change(g, p)) is not None:
            return r
        if not intersect(ws.policies[g], p)["approvers"]:
            return Refusal(Code.GATE_NO_ELIGIBLE, f"{g}: the override leaves no approver token")
    return None


def person_tokens(ws: WsCore, t: TCore, person: str) -> set[str]:
    """Approver tokens a person holds on a ticket; a viewer holds none (§5.7)."""
    m = ws.members.get(person)
    if m is None or m.role == "viewer":
        return set()
    tokens = {m.role}
    if t.owner == person:
        tokens.add("ticket_owner")
    for role in ("assignees", "reviewers", "watchers"):
        if person in t.people[role]:
            tokens.add(role)
    return tokens


def eligible(policy: dict[str, Any], tokens: set[str]) -> bool:
    return bool(tokens & set(policy["approvers"])) and not tokens & set(policy["not"])


def eligible_persons(ws: WsCore, t: TCore, gate: str) -> list[str]:
    p = effective(ws, t, gate)
    return sorted(m for m in ws.members if eligible(p, person_tokens(ws, t, m)))
