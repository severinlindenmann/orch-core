"""Shell projections: workspaces, members and identity. Unavailable fields are null, never guessed."""

from typing import Literal

from pydantic import BaseModel

from ..errors import fail
from ..text import hidden


class Member(BaseModel):
    person: str
    name: str
    role: Literal["owner", "maintainer", "member", "viewer"]
    devices: int


class Workspace(BaseModel):
    id: str
    prefix: str
    name: str
    members: list[Member] = []
    needs_you: int | None = None
    host_state: str = "ok"
    features: list[str] = []


class Me(BaseModel):
    person: str
    name: str
    role: str
    tier: str
    workspace: str


def members(data):
    for m in data["members"]:
        if not isinstance(m.get("name"), str) or hidden(m["name"]):
            # Not repaired, not shown: a name with invisible characters could pass for another person's.
            raise fail("member.name_invalid", "A member's name has invisible characters.", exit=3)
    return [
        Member(person=m["person"], name=m["name"], role=m["role"], devices=len(m["devices"])) for m in data["members"]
    ]


def me(identity, roster, ws):
    member = next(m for m in members(roster) if m.person == identity["person"])
    return Me(**member.model_dump(exclude={"devices"}), tier=identity["tier"], workspace=ws)
