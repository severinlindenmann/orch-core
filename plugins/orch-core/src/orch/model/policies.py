"""Gate policies (ticket-format §5.7): the effective policy is the intersection of workspace default and ticket
override, recomputed whenever either side changes, so an override can never end up looser.
"""

from __future__ import annotations

from typing import Any

from orch.canon import canonical_policy, policy_hash

from . import identity
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


def person_tokens(ws: WsCore, t: TCore, person: str, role: Any = identity.LOOKUP) -> set[str]:
    """Approver tokens a person holds on a ticket; a viewer holds none (§5.7). ``role`` is the signer's effective role
    (``identity.Signer.role``, which for a hub user is capped by the hub, §15.4); by default ``identity.role_of``."""
    person = identity.canonical(ws, person)
    if role is identity.LOOKUP:
        role = identity.role_of(ws, person)
    if role is None or role == "viewer":
        return set()
    tokens = {role}
    if identity.canonical(ws, t.owner) == person:
        tokens.add("ticket_owner")
    for role in ("assignees", "reviewers", "watchers"):
        if person in identity.canonical_set(ws, t.people[role]):
            tokens.add(role)
    return tokens


def eligible(policy: dict[str, Any], tokens: set[str]) -> bool:
    return bool(tokens & set(policy["approvers"])) and not tokens & set(policy["not"])


def solo(ws: WsCore) -> bool:
    """A workspace with exactly one member: separation of duties has nobody to separate (B5b)."""
    return len(ws.members) == 1


def may_decide(
    ws: WsCore, t: TCore, gate: str, person: str, pol: dict[str, Any] | None = None, role: Any = identity.LOOKUP
) -> bool:
    """Token eligibility for a decision. In a one-member workspace that member (unless a viewer) may decide every
    gate, so a solo owner can give a verdict on a ticket they claimed. With two or more members the policy's
    ``approvers`` and ``not`` apply as written. ``independent`` is checked separately and is not relaxed."""
    pol = effective(ws, t, gate) if pol is None else pol
    person = identity.canonical(ws, person)
    if role is identity.LOOKUP:
        role = identity.role_of(ws, person)
    if solo(ws) and person in ws.members:  # a hub user who is not the one member falls to the policy as written
        return role is not None and role != "viewer"
    return eligible(pol, person_tokens(ws, t, person, role))


def eligible_persons(ws: WsCore, t: TCore, gate: str) -> list[str]:
    p = effective(ws, t, gate)
    return sorted(m for m in ws.members if may_decide(ws, t, gate, m, p))


# --- D66 factor floor (ticket-format 15.4) ---------------------------------------------------------------------------

#: the lowest ``channel_factor`` a policy's optional ``factor`` key lets decide (D66, spec 5), weakest first
FACTORS = ("password+totp", "se-unlocked", "passkey")
_FACTOR_FOR = {"secure-enclave": "passkey", "secure-enclave-unlocked": "se-unlocked"}
DECISION_GATE = {"verdict.given": "verify"}


def factor_floor(ws: WsCore, t: TCore | None, typ: str, gate: str | None = None) -> str:
    """The ``min_factor`` for a hub-signed event of ``typ`` (``gate`` is the decision's gate; ``verdict.given`` is
    ``verify``): the stricter of the workspace gate policy's and the ticket override's optional ``factor`` key (D66:
    ``secure-enclave`` accepts only ``passkey``, ``secure-enclave-unlocked`` ``se-unlocked`` or ``passkey``), else the
    weakest, ``password+totp``. The ONE place a floor is computed: replay applies it at the line's position and the
    host passes its result to the contract verifier, never a literal. Only decisions have a floor; an unknown value
    in the key is the strongest floor (fail closed)."""
    gate = DECISION_GATE.get(typ, gate)
    if gate not in GATES or typ not in ("gate.approved", "gate.changes_requested", "verdict.given"):
        return FACTORS[0]
    out = FACTORS[0]
    for pol in (ws.policies.get(gate), (t.overrides.get(gate) if t is not None else None)):
        want = (pol or {}).get("factor")
        if want is None:
            continue
        floor = _FACTOR_FOR.get(want, FACTORS[-1])
        if FACTORS.index(floor) > FACTORS.index(out):
            out = floor
    return out
