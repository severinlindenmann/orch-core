"""Reduced workspace settings (M4): `workspace.settings`, `member.list`, `grant.list` as the UI's types.

The server forwards exactly what the host gives the person and never widens it: no field is added, guessed or copied
from another read. A field the host leaves out is `None` (unknown), never zero or a default of ours. What the host
withholds by role (a repo's `path` and `remote` for anyone but an owner or maintainer, other people's grants for anyone
but an owner) simply is not in the answer, and the UI says nothing about it.

Two things are held back on purpose. A repo's `path` is never forwarded (the Repos addon owns repo editing, and a
local path belongs to its owner's machine). A `remote` is shown only as a canonical identity without credentials.
"""

import re

from pydantic import BaseModel

from ..errors import fail
from ..text import hidden

GATES = ("requirements", "plan", "verify", "code")
DURATIONS = {"grant_hours": (1, 24), "claim_ttl_min": (15, 1440), "lease_ttl_min": (5, 1440)}
APPROVER_TOKENS = ("owner", "maintainer", "member", "ticket_owner", "assignees", "reviewers", "watchers")
TICKET_TYPES = ("feature", "bug", "chore", "spike", "epic")
PERSON = re.compile(r"^p_[A-Za-z0-9]{1,64}$")
REPO = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# No control, bidi, zero-width or byte-order characters in anything shown from the host (they could reorder a line).
BRANCH = re.compile(r"^.{1,255}$")
WORD = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
# F1 5.7: a canonical identity is `https://host[:port]/path` or `local:<name>`, and carries no userinfo.
REMOTE = re.compile(r"^(?:https://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?/[^\s@?#]{0,1000}|local:[A-Za-z0-9._-]{1,64})$")
USERINFO = re.compile(r"^(https://)[^/@\s]*@")
GRANT_ID = re.compile(r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
SESSION = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
MAX_SESSION_IDS = 100  # the host caps them at 100; the server never forwards more
MAX_REPO_PAGES = 8  # 32 repos a page: 256 repos at most are listed, and the answer says when there are more


class Stamp(BaseModel):
    seq: int
    at: str
    by: str


class Repo(BaseModel):
    name: str
    default_branch: str | None
    remote: str | None = None


class Settings(BaseModel):
    """`available: false` is a host without `settings/1`: every other field is then unknown (None)."""

    available: bool
    grant_hours: int | None = None
    claim_ttl_min: int | None = None
    lease_ttl_min: int | None = None
    defaults: list[str] | None = None
    gates: dict[str, dict] | None = None
    repos: list[Repo] | None = None
    repos_more: bool = False
    settings_at: Stamp | None = None
    policy_at: Stamp | None = None


def unavailable() -> Settings:
    return Settings(available=False)


def bad():
    return fail("internal", "The host's settings do not match its contract.", exit=1)


def stamp(value) -> Stamp | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or type(value.get("seq")) is not int
        or not TIME.fullmatch(str(value.get("at")))
        or not PERSON.fullmatch(str(value.get("by")))
    ):
        raise bad()
    return Stamp(seq=value["seq"], at=value["at"], by=value["by"])


def policy(value) -> dict:
    """One gate policy in canonical form, or an error: the form edits exactly what the host stores."""
    if (
        not isinstance(value, dict)
        or set(value) != {"approvers", "count", "not", "applies", "independent"}
        or not isinstance(value["approvers"], list)
        or not 1 <= len(value["approvers"]) <= 8
        or not all(isinstance(a, str) and a in APPROVER_TOKENS for a in value["approvers"])
        or type(value["count"]) is not int
        or not 1 <= value["count"] <= 9
        or not isinstance(value["not"], list)
        or len(value["not"]) > 8
        or not all(isinstance(a, str) and a in APPROVER_TOKENS for a in value["not"])
        or type(value["independent"]) is not bool
    ):
        raise bad()
    applies = value["applies"]
    if not (
        applies in ("all", "off")
        or (
            isinstance(applies, list)
            and 1 <= len(applies) <= 16
            and all(isinstance(t, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", t) for t in applies)
        )
    ):
        raise bad()
    return {
        "approvers": value["approvers"],
        "count": value["count"],
        "not": value["not"],
        "applies": applies,
        "independent": value["independent"],
    }


def remote(value) -> str | None:
    """A declared remote as a canonical identity without credentials, else nothing (never repaired)."""
    if not isinstance(value, str):
        return None
    value = USERINFO.sub(r"\1", value)
    return value if REMOTE.fullmatch(value) and not hidden(value) else None


def repo(name, entry) -> Repo:
    if not isinstance(name, str) or not REPO.fullmatch(name) or not isinstance(entry, dict):
        raise bad()
    branch = entry.get("default_branch")
    if branch is not None and not (isinstance(branch, str) and BRANCH.fullmatch(branch) and not hidden(branch)):
        raise bad()
    return Repo(name=name, default_branch=branch, remote=remote(entry.get("remote")))  # `path` is never forwarded


def settings(first: dict, more_pages: list[dict], more: bool) -> Settings:
    """`first` is the first page of `workspace.settings`; later pages only add repo entries (the rest repeats)."""
    try:
        scalars = first["settings"]
        out: dict = {"available": True}
        for name, (low, high) in DURATIONS.items():
            value = scalars[name]
            if type(value) is not int or not low <= value <= high:
                raise bad()
            out[name] = value
        gates = first["gates"]
        if not isinstance(gates, dict) or set(gates) - set(GATES):
            raise bad()
        defaults = first["defaults"]
        if not isinstance(defaults, list) or any(d not in DURATIONS for d in defaults):
            raise bad()
        repos: list[Repo] = []
        for page in [first, *more_pages]:
            repos += [repo(n, e) for n, e in sorted(page["settings"]["repos"].items())]
        return Settings(
            **out,
            defaults=sorted(set(defaults)),
            gates={g: policy(p) for g, p in gates.items()},
            repos=repos,
            repos_more=more,
            settings_at=stamp(first.get("settings_at")),
            policy_at=stamp(first.get("policy_at")),
        )
    except (KeyError, TypeError, AttributeError, ValueError):
        raise bad() from None


class Device(BaseModel):
    id: str
    auth: str | None
    scopes: list[str]
    valid: bool


class RosterMember(BaseModel):
    person: str
    name: str
    role: str
    devices: list[Device]
    # `last_seen` is not served by the host (v1.5): there is no such field to fill.


def word(value) -> str:
    if not isinstance(value, str) or not WORD.fullmatch(value):
        raise bad()
    return value


def roster(data) -> list[RosterMember]:
    try:
        out = []
        for m in data["members"]:
            devices = [
                Device(
                    id=word(d["id"]),
                    auth=word(d["auth"]) if isinstance(d.get("auth"), str) else None,
                    scopes=[word(s) for s in d.get("scopes") or []],
                    valid=d.get("valid") is True,
                )
                for d in m["devices"]
            ]
            if not isinstance(m["name"], str) or hidden(m["name"]):
                raise fail("member.name_invalid", "A member's name has invisible characters.", exit=3)
            out.append(RosterMember(person=m["person"], name=m["name"], role=m["role"], devices=devices))
        return out
    except (KeyError, TypeError, AttributeError, ValueError):
        raise bad() from None


class Revoked(BaseModel):
    at: str
    by: str | None = None
    seq: int | None = None


class Grant(BaseModel):
    id: str
    person: str
    scope: str
    verbs: str | list[str]
    issued_at: str
    expires_at: str
    revoked: Revoked | None
    sessions: int
    # read_ext/2: absent means unknown (None), not "none". `session_ids` is capped at 100; `sessions` counts all.
    session_ids: list[str] | None = None
    sessions_more: bool | None = None


def revoked(at, detail) -> Revoked | None:
    if not at:
        return None
    return Revoked(at=at, by=detail["by"] if detail else None, seq=detail.get("seq") if detail else None)


def grants(data, ext: bool) -> list[Grant]:
    try:
        out = []
        for g in data["grants"]:
            at = g.get("revoked_at")
            detail = g.get("revoked") if ext else None
            if detail is not None and (not isinstance(detail, dict) or not isinstance(detail.get("by"), str)):
                raise bad()
            ids = g.get("session_ids") if ext else None
            if ids is not None and not (
                isinstance(ids, list) and all(isinstance(i, str) and SESSION.fullmatch(i) for i in ids)
            ):
                raise bad()
            if not GRANT_ID.fullmatch(g["id"]) or type(g["sessions"]) is not int:
                raise bad()
            out.append(
                Grant(
                    id=g["id"],
                    person=g["person"],
                    scope=word(g["scope"]),
                    verbs=g["verbs"],
                    issued_at=g["issued_at"],
                    expires_at=g["expires_at"],
                    revoked=revoked(at, detail),
                    sessions=g["sessions"],
                    session_ids=ids[:MAX_SESSION_IDS] if ids is not None else None,
                    sessions_more=bool(g.get("sessions_more")) if ext and "sessions_more" in g else None,
                )
            )
        return out
    except (KeyError, TypeError, AttributeError, ValueError):
        raise bad() from None
