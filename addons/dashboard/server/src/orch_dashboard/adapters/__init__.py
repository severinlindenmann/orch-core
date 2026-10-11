"""M0 projections of the existing UI types. Unavailable fields are null, never guessed."""

from typing import Literal

from pydantic import BaseModel


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


class TicketSummary(BaseModel):
    key: str
    uid: str
    title: str
    type: str
    priority: str
    size: str | None
    status: str
    labels: list[str]
    parent: str | None
    owner: str | None
    assignees: list[str]
    claim: dict | None
    updated_at: str | None
    gates: dict[str, str]
    needs: list[str]
    head_seq: int
    epic: str | None = None


class Need(BaseModel):
    kind: str
    ticket: str | None
    uid: str | None
    title: str | None = None
    text: str
    ref: str | None
    since: str | None = None
    who: list[str]


class Today(BaseModel):
    workspace: str
    needs_you: list[Need]
    sub_cursor: str | None


def members(data):
    return [
        Member(person=m["person"], name=m["name"], role=m["role"], devices=len(m["devices"])) for m in data["members"]
    ]


def me(identity, roster, ws):
    member = next(m for m in members(roster) if m.person == identity["person"])
    return Me(**member.model_dump(exclude={"devices"}), tier=identity["tier"], workspace=ws)


def ticket(row):
    return TicketSummary(**{**row, "status": row["status"].replace("_", "-")})


def today(data, ws):
    kinds = {
        "approve": "approval",
        "question": "question",
        "no_eligible": "no_eligible",
        "acknowledge": "acknowledge",
        "addon": "addon",
    }
    return Today(
        workspace=ws,
        sub_cursor=data.get("sub_cursor"),
        needs_you=[
            Need(
                kind=("verdict" if n["kind"] == "approve" and n.get("ref") == "verify" else kinds[n["kind"]]),
                ticket=n.get("key"),
                uid=n.get("uid"),
                text=n["detail"],
                ref=n.get("ref"),
                who=n["who"],
            )
            for n in data["items"]
        ],
    )
