"""Live, administrator-only explanation of a person's workspace access."""

from ..access import ADMIN_GROUP, AccessError
from ..contract import phase4 as P4


def person_reach(access, registry, user_id):
    users = {u["user_id"]: u for u in access.users()}
    if user_id not in users:
        raise AccessError("unknown_user")
    groups = set(access.groups_of(user_id))
    admin = ADMIN_GROUP in groups
    rows = []
    for ws in sorted(registry.pins()):
        mode = registry.last_mode(ws)
        name = registry.display_name(ws)
        row = {
            "workspace_id": ws,
            "name": P4.render_text(name) if isinstance(name, str) and name else None,
            "mode": mode,
            "role": None,
            "via": [],
        }
        if mode == "full":
            row["role"] = access.role_of(user_id, ws)
            row["via"] = [
                {"kind": "group", "group": g["group"], "role": g["role"]}
                for g in access.grants(ws)
                if g["group"] in groups
            ]
            if admin:
                row["via"].append({"kind": "hub-admin"})
        elif mode == "selective":
            owner = registry.owner_alias(ws)
            row["owner"] = {"user_id": owner, "name": users.get(owner, {}).get("name")}
            row["note"] = "personal workspace: only its owner"
            if owner == user_id:
                row["role"] = "owner"
                row["via"] = [{"kind": "owner"}]
        rows.append(row)
    return {"user": {k: users[user_id][k] for k in ("user_id", "name")}, "hub_admin": admin, "workspaces": rows}
