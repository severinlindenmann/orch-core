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


def roles_after(access: Access, ws: str, e: Edit) -> dict[str, str]:
    """``{user_id: role}`` in workspace ``ws`` as :meth:`Access.roles` would answer once ``e`` was applied."""
    grants = {g["group"]: g["role"] for g in access.grants(ws)}
    if e.set_grant is not None:
        grants[e.set_grant[0]] = e.set_grant[1]
    for g in (e.drop_grant, e.drop_group):
        if g is not None:
            grants.pop(g, None)

    def members(group: str) -> set[str]:
        if group == e.drop_group:
            return set()
        out = set(access.members(group))
        if e.drop_member is not None and e.drop_member[0] == group:
            out.discard(e.drop_member[1])
        if e.add_member is not None and e.add_member[0] == group:
            out.add(e.add_member[1])
        if e.drop_user is not None:
            out.discard(e.drop_user)
        return out

    out: dict[str, str] = {u: "admin" for u in members(ADMIN_GROUP)}
    for group, role in grants.items():
        for u in members(group):
            if role_rank(role) > role_rank(out.get(u)):
                out[u] = role
    return out


# --- increases: who must approve ---------------------------------------------------------------------------------------


def _needs_of_workspace(registry: Any, ws: str, actor: str) -> tuple[str, str] | None:
    """``(workspace, owner)`` when ``ws`` is a full workspace whose owner alias exists and is not the actor."""
    if not is_full(registry, ws):
        return None
    owner = owner_of(registry, ws)
    return (ws, owner) if owner is not None and owner != actor else None


def needs_approval(access: Access, registry: Any, kind: str, body: dict[str, Any], actor: str) -> list[tuple[str, str]]:
    """THE rule. The ``(workspace_id, owner_user_id)`` pairs whose owners must approve this change before anything is
    applied (empty: it applies at once). Only INCREASES are asked about:

    * ``grant``: a new grant, or a higher role than the group holds now, on a full workspace;
    * ``member_add``: adding a person who is not in the group yet to a group that holds a grant on a full workspace.

    A workspace whose owner alias is the actor, has none, or is selective never needs approval. Reductions (revoke, a lower
    role, member / group / user removal) are never asked about here; the owner protection covers them."""
    out: list[tuple[str, str]] = []
    if kind == "grant":
        ws, group, role = body["workspace_id"], body["group"], body["role"]
        now = next((g["role"] for g in access.grants(ws) if g["group"] == group), None)
        if now is None or role_rank(role) > role_rank(now):
            need = _needs_of_workspace(registry, ws, actor)
            if need is not None:
                out.append(need)
    elif kind == "member_add":
        group, user = body["group"], body["user_id"]
        if user not in access.members(group):
            for grant in access.grants_of_group(group):
                need = _needs_of_workspace(registry, grant["workspace_id"], actor)
                if need is not None:
                    out.append(need)
    else:
        raise ValueError("unknown change kind")
    return sorted(set(out))


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
