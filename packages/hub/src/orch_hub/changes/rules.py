"""The access-change rules, in one place (M2-8a, orch-core#1409 and #1411). Pure reads of the access model and the
registry; nothing here writes.

* :func:`needs_approval` is the ONLY function that decides whether a change must wait for a workspace owner. The admin
  routes call it (through ``Changes``); nothing else repeats the rule.
* :func:`roles_after` computes who holds which role in a workspace AFTER an edit, without making it, so the owner
  protection (``owner_protected``) and the last-admin guard (``last_admin``) look at the real outcome.

Roles in a FULL workspace belong to the hub (grants, groups); in a selective workspace the hub roles grant nothing, so no
rule here applies to one (``is_full``). ``is_full`` reads the NEWEST binding mode, bound or not, so a workspace that left and
comes back cannot be used to slip an increase past its owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..access.store import ADMIN_GROUP, Access, role_rank
from .store import ChangeError

ADMIN_RANK = role_rank("admin")


@dataclass(frozen=True)
class Edit:
    """One hypothetical access change; unused fields stay empty."""

    set_grant: tuple[str, str] | None = None  # (group, role)
    drop_grant: str | None = None  # group
    drop_group: str | None = None
    drop_user: str | None = None
    drop_member: tuple[str, str] | None = None  # (group, user)
    add_member: tuple[str, str] | None = None  # (group, user)


def is_full(registry: Any, ws: str) -> bool:
    try:
        return registry.last_mode(ws) == "full"
    except KeyError, AttributeError:
        return False


def owner_of(registry: Any, ws: str) -> str | None:
    try:
        return registry.owner_alias(ws)
    except KeyError:
        return None


def full_workspaces(registry: Any) -> list[str]:
    return sorted(ws for ws in registry.pins() if is_full(registry, ws))


def roles_after(access: Access, ws: str, e: Edit, *, include_disabled: bool = False) -> dict[str, str]:
    """``{user_id: role}`` in workspace ``ws`` as :meth:`Access.roles` would answer once ``e`` was applied: the access
    model's own computation (:meth:`Access.roles_overlay`), not a second copy of it."""
    return access.roles_overlay(
        ws,
        set_grant=e.set_grant,
        drop_grant=e.drop_grant,
        drop_group=e.drop_group,
        drop_user=e.drop_user,
        drop_member=e.drop_member,
        add_member=e.add_member,
        include_disabled=include_disabled,
    )


# --- increases: who must approve ---------------------------------------------------------------------------------------


def end_change(access: Access, body: dict[str, Any]) -> str | None:
    """For a ``member_add`` body that carries ``ends_ms``: how it changes an ACTIVE membership: ``"later"`` (a later end, or
    permanent: an increase), ``"earlier"`` (a shortening: a reduction) or ``None`` (a new member, an ended row that counts as
    missing, no ``ends_ms`` in the body, or no change)."""
    if "ends_ms" not in body:
        return None
    cur = access.member_end(body["group"], body["user_id"])
    new = body["ends_ms"]
    if cur == "absent" or (cur is not None and cur <= access._now()):  # noqa: SLF001 - the model's clock
        return None
    if cur is None:
        return None if new is None else "earlier"
    if new is None or new > cur:
        return "later"
    return "earlier" if new < cur else None


def _edit_of(kind: str, body: dict[str, Any]) -> Edit:
    if kind == "grant":
        return Edit(set_grant=(body["group"], body["role"]))
    if kind == "member_add":
        return Edit(add_member=(body["group"], body["user_id"]))
    raise ValueError("unknown change kind")


def _candidates(access: Access, registry: Any, kind: str, body: dict[str, Any]) -> list[str]:
    """The full workspaces a change can reach: the one it names, or everything the group reaches (the hub-admin group
    reaches every full workspace)."""
    if kind == "grant":
        return [body["workspace_id"]] if is_full(registry, body["workspace_id"]) else []
    if kind == "member_add":
        return affected_by_group(access, registry, body["group"])
    raise ValueError("unknown change kind")


def reach_delta(access: Access, registry: Any, kind: str, body: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """THE invariant. For every FULL workspace the change touches: the (person, role) pairs that would be ADDED or RAISED
    if it were applied now, simulated with the same role computation the hub signs into snapshots (``roles_after``).
    Workspaces with no added or raised pair are absent. Kind-independent: a grant, a member added to any group (the
    hub-admin group included) and a role change are all judged by what they do to who can reach a workspace."""
    e = _edit_of(kind, body)
    out: dict[str, list[dict[str, str]]] = {}
    for ws in _candidates(access, registry, kind, body):
        # disabled members count: an owner is shown everyone the change could ever give access to, not only who is active
        before = access.roles_overlay(ws, include_disabled=True)
        after = roles_after(access, ws, e, include_disabled=True)
        gained = [
            {"user_id": u, "role": r} for u, r in sorted(after.items()) if role_rank(r) > role_rank(before.get(u))
        ]
        if not gained and kind == "member_add" and end_change(access, body) == "later":
            # R3: a later end date (or none) is an increase even though no role changes today: the owner is asked
            role = next((g["role"] for g in access.grants_of_group(body["group"]) if g["workspace_id"] == ws), None)
            if role is not None:
                gained = [{"user_id": body["user_id"], "role": role}]
        if gained:
            out[ws] = gained
    return out


def needs_approval(access: Access, registry: Any, kind: str, body: dict[str, Any], actor: str) -> list[tuple[str, str]]:
    """THE rule. The ``(workspace_id, owner_user_id)`` pairs whose owners must approve this change before anything is
    applied (empty: it applies at once): every full workspace with a non-empty :func:`reach_delta` whose owner alias
    exists and is not the actor. Only INCREASES are asked about; reductions (revoke, a lower role, member / group / user
    removal) never are (the owner protection covers them)."""
    out = []
    for ws in reach_delta(access, registry, kind, body):
        owner = owner_of(registry, ws)
        if owner is not None and owner != actor:
            out.append((ws, owner))
    return sorted(out)


# --- reductions: the owner is never locked out, and the last admin is not removed by accident --------------------------


def check_owner_protected(access: Access, registry: Any, e: Edit, candidates: list[str]) -> None:
    """Refuse (``owner_protected``) an edit that would remove or demote the owner alias's OWN admin-level access to their
    workspace. Absolute: no confirmation, no override gets past it (the owner's exit is switching to selective)."""
    from .store import ChangeError

    for ws in candidates:
        if not is_full(registry, ws):
            continue
        owner = owner_of(registry, ws)
        if owner is None:
            continue
        before, after = access.roles(ws), roles_after(access, ws, e)
        if role_rank(before.get(owner)) >= ADMIN_RANK and role_rank(after.get(owner)) < ADMIN_RANK:
            raise ChangeError("owner_protected")


def check_last_admin(access: Access, registry: Any, e: Edit, candidates: list[str]) -> None:
    """Refuse (``last_admin``) an edit that would leave a full workspace that has an admin-level holder now with none
    (hub-admin group members count as admins). The caller lets ``confirm`` skip this one."""
    for ws in candidates:
        if not is_full(registry, ws):
            continue
        before, after = access.roles(ws), roles_after(access, ws, e)
        if any(role_rank(r) >= ADMIN_RANK for r in before.values()) and not any(
            role_rank(r) >= ADMIN_RANK for r in after.values()
        ):
            raise ChangeError("last_admin")


def affected_by_group(access: Access, registry: Any, group: str) -> list[str]:
    """Full workspaces where a change to who is in ``group`` (or to the group itself) can change a role."""
    if group == ADMIN_GROUP:
        return full_workspaces(registry)
    return sorted(g["workspace_id"] for g in access.grants_of_group(group) if is_full(registry, g["workspace_id"]))


def affected_by_user(access: Access, registry: Any, user: str) -> list[str]:
    out: set[str] = set()
    for group in access.groups_of(user):
        out.update(affected_by_group(access, registry, group))
    return sorted(out)
