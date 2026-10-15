"""Shell projections: workspaces, members and identity. Unavailable fields are null, never guessed."""

import re
from typing import Literal

from pydantic import BaseModel

from ..text import hidden
from .addons import runner

DEVICE = re.compile(r"d_[0-9a-f]{32}")


class Member(BaseModel):
    person: str
    name: str
    role: Literal["owner", "maintainer", "member", "viewer"]
    devices: int
    # The host's name had invisible characters: it is not shown (nor repaired); the person id stands in for it.
    name_hidden: bool = False


class Workspace(BaseModel):
    id: str
    prefix: str
    name: str
    members: list[Member] = []
    needs_you: int | None = None
    host_state: str = "ok"
    features: list[str] = []
    # What the signer says about this workspace (custody, enabled, available, problem); null: no signer answered.
    click: dict | None = None
    runner: dict | None = None


def click_card(row):
    """The signer's per-workspace status as the card needs it. Only these fields; nothing else is passed on."""
    if not isinstance(row, dict):
        return None
    out = {
        "custody": row.get("custody") if row.get("custody") in ("convenience", "passphrase", "none") else "none",
        "enabled": row.get("enabled") is True,
        "available": row.get("available") is True,
    }
    if isinstance(row.get("problem"), str):
        out["problem"] = row["problem"][:200]
    if isinstance(row.get("device"), str) and DEVICE.fullmatch(row["device"]):
        out["device"] = row["device"]  # the key the switch would move (shown before the person types the passphrase)
    return out


class Me(BaseModel):
    person: str
    name: str
    role: str
    tier: str
    workspace: str
    runner: dict | None = None


def safe_name(m) -> tuple[str, bool]:
    """A member's name, or their person id when the host's name can't be shown safely (invisible characters could
    make it pass for another person's). The name is never repaired; one such member doesn't hide the workspace."""
    if isinstance(m.get("name"), str) and not hidden(m["name"]):
        return m["name"], False
    return m["person"], True


def members(data):
    out = []
    for m in data["members"]:
        name, name_hidden = safe_name(m)
        out.append(
            Member(person=m["person"], name=name, role=m["role"], devices=len(m["devices"]), name_hidden=name_hidden)
        )
    return out


def me(identity, roster, ws, *, runner_supported=False):
    member = next(m for m in members(roster) if m.person == identity["person"])
    return Me(
        **member.model_dump(exclude={"devices"}),
        tier=identity["tier"],
        workspace=ws,
        runner=runner(identity, runner_supported),
    )
