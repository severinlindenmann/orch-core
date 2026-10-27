"""Reduced workspace settings (M4): `workspace.settings`, `member.list`, `grant.list` as the UI's types.

The server forwards exactly what the host gives the person and never widens it: no field is added, guessed or copied
from another read. A field the host leaves out is `None` (unknown), never zero or a default of ours. What the host
withholds by role (a repo's `path` and `remote` for anyone but an owner or maintainer, other people's grants for anyone
but an owner) simply is not in the answer, and the UI says nothing about it.

Two things are held to the host's word. A repo's `path` and `check` (is the folder there, which branch) are forwarded
only when the host sent them, which it does for an owner or maintainer at the keyboard and nobody else; they are never
asked for another way. A `remote` is shown only as a canonical identity without credentials.
"""

import re

from pydantic import BaseModel, Field, model_serializer

from ..errors import fail
from ..text import hidden
from .shell import safe_name

GATES = ("requirements", "plan", "verify", "code")
DURATIONS = {"grant_hours": (1, 168), "claim_ttl_min": (15, 1440), "lease_ttl_min": (5, 1440)}
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
TICKET_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,18}$")
GRANT_ID = re.compile(r"^f?gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
SESSION = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
#: a grant's label (common.json `label`): `runner` marks the runner grant (H11c-2); anything else is shown as text
GRANT_LABEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
MAX_SESSION_IDS = 100  # the host caps them at 100; the server never forwards more
MAX_REPO_PAGES = 8  # 32 repos a page: 256 repos at most are listed, and the answer says when there are more


class Stamp(BaseModel):
    seq: int
    at: str
    by: str


REPO_STATES = ("present", "missing", "not_git", "unreadable")


class RepoCheck(BaseModel):
    """What the host saw at the repo's folder just now (read-only): the folder and the branch checked out."""

    state: str
    branch: str | None = None


class Repo(BaseModel):
    name: str
    default_branch: str | None
    remote: str | None = None
    #: the declared folder: only when the host sent it (owners and maintainers); otherwise absent, never guessed
    path: str | None = None
    check: RepoCheck | None = None
    #: keys of the tickets that link this repo (an owner or maintainer at the keyboard only, as the host sent them)
    tickets: list[str] | None = None


class Settings(BaseModel):
    """`available: false` is a host without `settings/1`: every other field is then unknown (None)."""

    available: bool
    grant_hours: int | None = None
    claim_ttl_min: int | None = None
    lease_ttl_min: int | None = None
    #: B6: the workspace's allowed ways to sign (`terminal` always). Absent when the host did not say (an older core).
    sign_methods: list[str] | None = None
    #: whether the host reported sign_methods at all (an explicit null = not set, every way allowed)
    sign_methods_known: bool = Field(default=False, exclude=True)
    defaults: list[str] | None = None
    gates: dict[str, dict] | None = None
    repos: list[Repo] | None = None
    repos_more: bool = False
    settings_at: Stamp | None = None
    policy_at: Stamp | None = None

    @model_serializer(mode="wrap")
    def _omit_unknown_sign_methods(self, handler):
        out = handler(self)
        if out.get("sign_methods") is None and not self.sign_methods_known:
            out.pop("sign_methods", None)  # an older core said nothing: absent (the UI shows it read-only)
        return out


def unavailable() -> Settings:
    return Settings(available=False)


# The ways to sign the host can hold: core's own list is the one source (a fresh One click setup signs all four, so a
# narrower list here made every freshly set-up workspace answer 500). `touchid` is reserved by the contract.
try:
    from orch.model.workspace import SIGN_METHODS as _CORE_SIGN_METHODS
except ImportError:  # pragma: no cover - the dashboard always ships with core; this is only a safety net
    _CORE_SIGN_METHODS = ("terminal", "click", "phone", "hub")
SIGN_METHODS_KNOWN = frozenset((*_CORE_SIGN_METHODS, "touchid"))


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
    path = entry.get("path")
    if path is not None and not (isinstance(path, str) and 0 < len(path) <= 1024 and not hidden(path)):
        raise bad()
    check = entry.get("check")
    if check is not None:
        got = check.get("branch") if isinstance(check, dict) else 0
        if (
            not isinstance(check, dict)
            or check.get("state") not in REPO_STATES
            or not (got is None or (isinstance(got, str) and BRANCH.fullmatch(got) and not hidden(got)))
        ):
            raise bad()
        check = RepoCheck(state=check["state"], branch=got)
    linking = entry.get("linked_by")
    if linking is not None:
        if not (
            isinstance(linking, list)
            and len(linking) <= 12
            and all(isinstance(k, str) and TICKET_KEY.fullmatch(k) for k in linking)
        ):
            raise bad()
    return Repo(
        name=name, default_branch=branch, remote=remote(entry.get("remote")), path=path, check=check, tickets=linking
    )


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
        methods = first.get("sign_methods")
        if methods is not None and (
            not isinstance(methods, list)
            or "terminal" not in methods
            or any(m not in SIGN_METHODS_KNOWN for m in methods)
        ):
            raise bad()
        repos: list[Repo] = []
        for page in [first, *more_pages]:
            repos += [repo(n, e) for n, e in sorted(page["settings"]["repos"].items())]
        return Settings(
            **out,
            sign_methods=methods,
            sign_methods_known="sign_methods" in first,
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
    # v1.9 `read_ext/3` (section 0.8.5) and H12d-2. Absent from the host: None (unknown), never a default of ours.
    #: the device's own name (R6 pairing record), untrusted text; None when the host sends none or it can't be shown
    label: str | None = None
    #: the label had invisible or control characters (other than a line break): it is withheld, never repaired
    label_hidden: bool = False
    paired_at: str | None = None
    relay: bool | None = None
    cert_expires_at: str | None = None
    #: the level the device's reader record counts as now; None: no verified record (the gate counts it as 0)
    reader: int | None = None


class RosterMember(BaseModel):
    person: str
    name: str
    role: str
    devices: list[Device]
    name_hidden: bool = False  # see adapters.shell.safe_name
    # `last_seen` is not served by the host (v1.5): there is no such field to fill.


LABEL_MAX = 80  # protocol section 2.5: at most 80 code points after cleaning
READER_MAX = 1000  # a level is a small int; anything outside 0..READER_MAX is not a level


def label(value) -> tuple[str | None, bool]:
    """The device's label, or None. A line break is kept (the protocol keeps LF); any other invisible or control
    character withholds the whole label (`label_hidden`), as a member's name is."""
    if not isinstance(value, str) or not value or len(value) > LABEL_MAX:
        return None, False
    if hidden(value.replace("\n", "")):
        return None, True
    return value, False


def level(value) -> int | None:
    return value if type(value) is int and 0 <= value <= READER_MAX else None


def time_or_none(value) -> str | None:
    return value if isinstance(value, str) and TIME.fullmatch(value) else None


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
                    label=label(d.get("label"))[0],
                    label_hidden=label(d.get("label"))[1],
                    paired_at=time_or_none(d.get("paired_at")),
                    relay=d["relay"] if type(d.get("relay")) is bool else None,
                    cert_expires_at=time_or_none(d.get("cert_expires_at")),
                    reader=level(d.get("reader")),
                )
                for d in m["devices"]
            ]
            name, name_hidden = safe_name(m)
            out.append(
                RosterMember(person=m["person"], name=name, role=m["role"], devices=devices, name_hidden=name_hidden)
            )
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
    # H11c-2: the grant.issued label (`runner` for the runner grant); None when the host does not say or it is not one
    label: str | None = None
    # read_ext/2: absent means unknown (None), not "none". `session_ids` is capped at 100; `sessions` counts all.
    session_ids: list[str] | None = None
    sessions_more: bool | None = None
    # a follow-up grant (`fgr_`) names the grant it was derived from; None for every other grant
    parent: str | None = None


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
                    label=g["label"] if isinstance(g.get("label"), str) and GRANT_LABEL.fullmatch(g["label"]) else None,
                    session_ids=ids[:MAX_SESSION_IDS] if ids is not None else None,
                    sessions_more=bool(g.get("sessions_more")) if ext and "sessions_more" in g else None,
                    parent=g["parent"]
                    if isinstance(g.get("parent"), str) and GRANT_ID.fullmatch(g["parent"])
                    else None,
                )
            )
        return out
    except (KeyError, TypeError, AttributeError, ValueError):
        raise bad() from None


# ---- reader.list (H12d-2, cap `reader/1`) ---------------------------------------------------------------------------
DEVICE = re.compile(r"^d_[0-9a-f]{32}$")


class Reader(BaseModel):
    device: str
    person: str
    #: what the device declared (0 without a record that verifies)
    reader: int
    #: the level the gate counts now
    level: int


class Readers(BaseModel):
    available: bool
    readers: list[Reader]
    #: 1 while click-to-sign is on or the workspace has click-signed history: every reader must be at this level
    required_reader: int | None


def readers_unavailable() -> Readers:
    return Readers(available=False, readers=[], required_reader=None)


def readers(data) -> Readers:
    """`reader.list` through an allow-list: device, person, reader, level and `required_reader` only. `dk_hash`,
    `digest` and `decl` stay on the server (the page does not need them)."""
    try:
        rows = []
        for r in data["readers"]:
            if not DEVICE.fullmatch(r["device"]) or not PERSON.fullmatch(r["person"]):
                raise ValueError("id")
            declared, counted = level(r.get("reader")), level(r.get("level"))
            if declared is None or counted is None:
                raise ValueError("level")
            rows.append(Reader(device=r["device"], person=r["person"], reader=declared, level=counted))
        required = level(data.get("required_reader"))
        if required is None:
            raise ValueError("required_reader")
        return Readers(available=True, readers=rows, required_reader=required)
    except (KeyError, TypeError, AttributeError, ValueError):
        raise bad() from None
