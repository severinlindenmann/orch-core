"""The signed actions of #305 (M3): prepare, look at, and cancel a hand-off. A closed allowlist of typed actions.

**This server never signs and never holds a factor, and it never calls `human.submit`.** The person signs in a
trusted place (`orch sign CODE` in a terminal, later the Touch ID helper), which fetches the request itself and submits
it. The dashboard only asks the host to prepare the request, shows the host's `material`, and follows the outcome.

Every route has a typed model that forbids extras; the host op and its arguments are fixed per action. The browser
never names an op, a path or a raw event.

**One action intent -> at most one request.** `opts.idem` is derived here from the action itself (`intent.py`): the
person, the workspace, the op and arguments exactly as sent to the host, and only the gate or question version.
The browser sends no key. Two tabs, a reopened dialog, a lost answer or a new login all send the same idem with the
same arguments, and the host (contract 0.7, section 0.8.1) answers each with the SAME request, or its terminal state.
This process remembers nothing that the invariant depends on.
"""

import base64
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from . import adapters
from .errors import HostError, fail
from .gateway import Gateway
from .hostlink import _pairs, _valid
from .intent import gate_base, intent_idem, question_base
from .reads import Key, workspace
from .text import clean as no_hidden

RequestId = Annotated[str, StringConstraints(pattern=adapters.handoff.REQUEST_ID.pattern)]
Text = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
Word = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
Label = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,31}$")]
Question = Annotated[str, StringConstraints(pattern=r"^Q[1-9][0-9]{0,5}$")]
Gate = Literal["requirements", "plan", "code"]

HUMAN_OPS = ("human.prepare", "human.get", "human.pending", "human.cancel")  # never human.submit
PER_SESSION = 8  # design section 3: pending sign requests per session
TTL = 600


class Action(BaseModel):
    """Every typed action. `retry` is the explicit "do it again" counter (0 normally): only a click on "Do it again" or
    "Start again" advances it, and it is the only thing besides the action itself that changes its intent."""

    model_config = ConfigDict(extra="forbid", strict=True)
    retry: Annotated[int, Field(ge=0, le=9999)] = 0


class Approve(Action):
    action: Literal["approve"]
    key: Key
    gate: Gate


class RequestChanges(Action):
    action: Literal["request_changes"]
    key: Key
    gate: Literal["requirements", "plan", "verify", "code"]
    message: Text


class Verdict(Action):
    action: Literal["verdict"]
    key: Key
    outcome: Literal["pass", "fail"]
    message: Text | None = None

    @model_validator(mode="after")
    def reason(self):
        if self.outcome == "fail" and not self.message:
            raise ValueError("a failed verdict needs its reason")
        return self


class Answer(Action):
    action: Literal["answer"]
    key: Key
    question: Question
    option: Word | None = None
    message: Text | None = None

    @model_validator(mode="after")
    def something(self):
        if self.option is None and self.message is None:
            raise ValueError("answer with an option, a message, or both")
        return self


class Comment(Action):
    action: Literal["comment"]
    key: Key
    message: Text


class AddLabel(Action):
    action: Literal["label"]
    key: Key
    label: Label


class Close(Action):
    action: Literal["close"]
    key: Key
    resolution: Literal["wont_do", "duplicate", "obsolete", "other"] = "other"
    duplicate_of: Key | None = None
    message: Text | None = None


class Reopen(Action):
    action: Literal["reopen"]
    key: Key
    message: Text | None = None


class NewTicket(Action):
    """A person's `new` carries the key, type, title and owner only (stream A, 11 Oct); the rest is `Details`."""

    action: Literal["new"]
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    type: Literal["feature", "bug", "chore", "spike", "epic"] = "feature"


class Details(Action):
    """The follow-up hand-off after `new`: priority, size and labels as one signed change of the ticket."""

    action: Literal["details"]
    key: Key
    priority: Literal["low", "medium", "high", "urgent"] | None = None
    #: "none" takes the size away (core `set size=none`); the Estimate add-on's "not estimated"
    size: Literal["xs", "s", "m", "l", "xl", "none"] | None = None
    labels: list[Label] | None = Field(default=None, min_length=0, max_length=16)
    parent: Key | None = None
    #: the Estimate box names itself: then the size is all this carries and the add-on must be ON (see `plan`).
    #: Not signed and not sent to the host; the host sees an ordinary `set size=...`.
    via: Literal["estimate"] | None = None

    @model_validator(mode="after")
    def something(self):
        if self.priority is None and self.size is None and self.labels is None and self.parent is None:
            raise ValueError("details change at least one field")
        if self.via == "estimate" and (self.size is None or self.priority or self.labels is not None or self.parent):
            raise ValueError("an estimate changes the size and nothing else")
        return self


SectionId = Literal[
    "summary",
    "context",
    "requirements",
    "out_of_scope",
    "plan",
    "decisions",
    "verification",
    "findings",
    "current_state",
]
MAX_SECTION_BYTES = 65536  # P1 and the host: 64 KiB of UTF-8, not characters


def _section_bytes(text: str) -> str:
    if not text.strip():
        raise ValueError("a section needs some text")
    if len(text.encode()) > MAX_SECTION_BYTES:
        raise ValueError(f"a section is at most {MAX_SECTION_BYTES} bytes of UTF-8")
    return text


SectionText = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_SECTION_BYTES), AfterValidator(_section_bytes)
]


class WriteSection(Action):
    """The follow-up hand-off after `new` for the description: one section, replaced as one signed change."""

    action: Literal["section"]
    key: Key
    section: SectionId
    text: SectionText


PersonId = Annotated[str, StringConstraints(pattern=r"^p_[0-9a-f]{32}$")]
Criterion = Annotated[str, StringConstraints(min_length=1, max_length=200)]
PeopleRole = Literal["owner", "assignees", "reviewers"]


class NewPeople(BaseModel):
    """The people of a NEW ticket (B12D): the owner and the assignees and reviewers, as the form picked them."""

    model_config = ConfigDict(extra="forbid", strict=True)
    owner: PersonId | None = None
    assignees: list[PersonId] = Field(default_factory=list, max_length=16)
    reviewers: list[PersonId] = Field(default_factory=list, max_length=16)


def _new_people(people: "NewPeople | None") -> dict:
    out: dict = {}
    if people is None:
        return out
    if people.owner:
        out["owner"] = people.owner
    for role in ("assignees", "reviewers"):
        ids = sorted(set(getattr(people, role)))
        if ids:
            out[role] = ids
    return out


class QuickAgent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    hours: Annotated[int, Field(ge=1, le=168)]


class PastedImage(BaseModel):
    """R3 part B: a screenshot pasted into a section. Only its description travels here: the bytes go to the signer
    (`click/images`), which checks them against this digest; core `orch.host.sequence` refuses a name that does not
    carry the first 8 characters of the digest."""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: Annotated[str, StringConstraints(pattern=r"^paste-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\.(png|webp)$")]
    sha256: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    bytes: Annotated[int, Field(ge=1, le=5 * 1024 * 1024)]
    alt: Annotated[str, StringConstraints(min_length=1, max_length=120)]


def _images_of(images: list["PastedImage"]) -> list[dict]:
    from orch.canon.text import normalize_text

    return [
        {"name": i.name, "sha256": i.sha256, "bytes": i.bytes, "alt": normalize_text(i.alt, one_line=True)}
        for i in sorted(images, key=lambda x: x.name)
    ]


class NewTicketSequence(Action):
    """B11 (finding 9): a New ticket with its written text on ONE review and ONE confirmation. The signer signs
    `ticket.created`, one `ticket.updated` for the fields and one per written section (host `sequence`, core
    `orch.host.sequence`); this server only relays the intent, the signer builds and checks every event itself."""

    action: Literal["new_sequence"]
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    type: Literal["feature", "bug", "chore", "spike", "epic"] = "feature"
    priority: Literal["low", "medium", "high", "urgent"] | None = None
    size: Literal["xs", "s", "m", "l", "xl"] | None = None
    labels: list[Label] | None = Field(default=None, max_length=16)
    parent: Key | None = None
    sections: dict[SectionId, SectionText] = Field(default_factory=dict, max_length=10)
    #: B12D: acceptance criteria and people in the same review (N3)
    acceptance: list[Criterion] = Field(default_factory=list, max_length=8)
    people: NewPeople | None = None
    #: U4: "Create and start agent" without a usable runner grant: the agent access, signed LAST in the same review
    #: (the start itself is never signed; the page does it afterwards). Absent = the intent is exactly as before.
    agent: QuickAgent | None = None
    #: R3 part B: screenshots pasted into the sections (one signed file each, before the section that shows it)
    images: list[PastedImage] = Field(default_factory=list, max_length=6)

    def intent(self) -> dict:
        """The canonical intent the signer reviews (core `orch.host.sequence`): texts normalised the way the host's
        `section.set` normalises them, labels sorted, a default priority left out. A text core refuses is a 422."""
        from orch.canon.text import TextError, normalize_text
        from orch.host import sequence

        try:
            sets: dict = {}
            if self.priority and self.priority != "medium":
                sets["priority"] = self.priority
            if self.size:
                sets["size"] = self.size
            if self.labels:
                sets["labels"] = sorted(set(self.labels))
            if self.parent and self.type != "epic":
                sets["parent"] = self.parent
            sections = {sid: normalize_text(text).strip("\n") for sid, text in self.sections.items()}
            intent: dict = {
                "v": 1,
                "kind": "new_ticket",
                "title": normalize_text(self.title, one_line=True),
                "ticket_type": self.type,
                "set": sets,
                "sections": {sid: t for sid, t in sections.items() if t},
            }
            criteria = [normalize_text(t, one_line=True) for t in self.acceptance if t.strip()]
            if criteria:
                intent["acceptance"] = criteria
            people = _new_people(self.people)
            if people:
                intent["people"] = people
            if self.images:
                intent["images"] = _images_of(self.images)
            if self.agent is not None:
                intent["agent"] = {"hours": self.agent.hours, "scope": "workable"}
            return sequence.validate_intent(intent)
        except (TextError, sequence.SequenceInvalid) as exc:
            raise fail("invalid.input", f"That ticket can't be signed: {exc}", exit=1) from None


class EditAcceptance(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    edit: dict[Annotated[str, StringConstraints(pattern=r"^AC[1-9][0-9]*$")], Criterion] = Field(
        default_factory=dict, max_length=8
    )
    add: list[Criterion] = Field(default_factory=list, max_length=8)


TaskId = Annotated[str, StringConstraints(pattern=r"^T[1-9][0-9]*$")]
AcId = Annotated[str, StringConstraints(pattern=r"^AC[1-9][0-9]*$")]
TaskText = Annotated[str, StringConstraints(max_length=500)]
TaskCommand = Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class TaskEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: TaskText = ""
    verify: TaskCommand | None = None
    proves: list[AcId] = Field(default_factory=list, max_length=64)


class TaskAdd(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: TaskText
    verify: TaskCommand = ""
    proves: list[AcId] = Field(max_length=64)


class EditTasks(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    edit: dict[TaskId, TaskEdit] = Field(default_factory=dict, max_length=32)
    add: list[TaskAdd] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def limit_changes(self):
        if len(self.edit) + len(self.add) > 32:
            raise ValueError("At most 32 tasks can be changed or added together.")
        return self


class PeopleChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    add: list[PersonId] = Field(default_factory=list, max_length=16)
    remove: list[PersonId] = Field(default_factory=list, max_length=16)


class EditLinks(BaseModel):
    """F6: the repositories (workspace repo names) a ticket is linked to and a branch for some of them. The whole new
    list: the server compares it with the ticket as read now and builds the old-to-new change from that."""

    model_config = ConfigDict(extra="forbid", strict=True)
    repos: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(max_length=16)
    branches: dict[
        Annotated[str, StringConstraints(min_length=1, max_length=100)],
        Annotated[str, StringConstraints(min_length=1, max_length=200)],
    ] = Field(default_factory=dict, max_length=16)


class EditTicket(Action):
    """B12D (finding N3): an inline edit of an EXISTING ticket on ONE review and ONE confirmation. The page sends what
    it changes and the `head_seq` of the log it showed. This server reads the ticket, refuses an edit of a ticket that
    has changed since (`conflict.stale`), and builds the intent from that read (the text and criteria it replaces, the
    head hash). The signer signs one ordinary event per change and stops if anyone else writes to the ticket meanwhile
    (core `orch.host.sequence`, kind edit_ticket); this server only relays the intent."""

    action: Literal["edit_sequence"]
    key: Key
    head_seq: Annotated[int, Field(ge=1)]
    sections: dict[SectionId, SectionText] = Field(default_factory=dict, max_length=10)
    acceptance: EditAcceptance | None = None
    tasks: EditTasks | None = None
    people: dict[PeopleRole, PeopleChange] | None = None
    links: EditLinks | None = None
    images: list[PastedImage] = Field(default_factory=list, max_length=6)  # R3 part B, as in a new ticket

    def intent(self, data: dict) -> dict:
        """`data` is the host's `ticket.get` of the ticket, read now."""
        from orch import canon
        from orch.canon.text import TextError, normalize_text
        from orch.host import sequence

        doc = data.get("doc") or {}
        head = doc.get("head") or {}
        if head.get("seq") != self.head_seq:
            raise fail(
                "conflict.stale",
                "This ticket changed after you opened it. Reload it and make your edit again.",
                exit=5,
            )
        try:
            current = {**(doc.get("sections") or {}), "current_state": doc.get("current_state", "")}
            intent: dict = {
                "v": 1,
                "kind": "edit_ticket",
                "ticket": {"key": doc.get("key"), "uid": doc.get("uid")},
                "ticket_type": doc.get("type"),
                "head": head.get("head"),
                "sections": {
                    sid: {"was": canon.section_hash(current.get(sid, "")), "text": normalize_text(text).strip("\n")}
                    for sid, text in self.sections.items()
                    if normalize_text(text).strip("\n") != current.get(sid, "")
                },
            }
            if intent["ticket"]["key"] != self.key:
                raise sequence.SequenceInvalid("the host answered for another ticket")
            if self.acceptance is not None and (self.acceptance.edit or self.acceptance.add):
                a = self.acceptance
                was = [{"id": x["id"], "text": x["text"]} for x in doc.get("acceptance") or []]
                known = {x["id"]: x["text"] for x in was}
                if set(a.edit) - set(known):
                    raise sequence.SequenceInvalid("it names a criterion the ticket does not have")
                edits = {k: normalize_text(v, one_line=True) for k, v in a.edit.items()}
                intent["acceptance"] = {
                    "was": was,
                    "edit": {k: v for k, v in edits.items() if v != known[k]},
                    "add": [normalize_text(t, one_line=True) for t in a.add if t.strip()],
                }
                if not intent["acceptance"]["edit"] and not intent["acceptance"]["add"]:
                    del intent["acceptance"]
            if self.tasks is not None:
                rows = doc.get("tasks") or []
                was = [{k: row[k] for k in ("id", "text", "verify", "proves", "assignee") if k in row} for row in rows]
                known = {row["id"]: row for row in was}
                states = {row["id"]: row.get("state") for row in rows}
                edits = {}
                for tid, change in self.tasks.edit.items():
                    if tid not in known:
                        raise sequence.SequenceInvalid("it names a task the ticket does not have")
                    previous = {**known[tid], "verify": known[tid]["verify"]["cmd"] if known[tid]["verify"] else None}
                    fields = {}
                    for name in change.model_fields_set:
                        value = getattr(change, name)
                        if isinstance(value, str):
                            value = normalize_text(value, one_line=True)
                        if name == "proves":  # the canonical form core requires: sorted, unique
                            value = sorted(set(value), key=lambda a: int(a[2:]))
                        if name == "text" and not value.strip():
                            raise sequence.SequenceInvalid("Task text cannot be empty.")
                        if value != previous[name]:
                            fields[name] = value
                    if states[tid] == "done" and ({"verify", "proves"} & fields.keys()):
                        raise sequence.SequenceInvalid(
                            "This task is done: reopen it before changing its check or criteria."
                        )
                    if fields:
                        edits[tid] = fields
                adds = []
                for task in self.tasks.add:
                    if not task.text.strip():
                        continue
                    item = {
                        "text": normalize_text(task.text, one_line=True),
                        "proves": sorted(set(task.proves), key=lambda a: int(a[2:])),
                    }
                    if "verify" in task.model_fields_set:
                        item["verify"] = normalize_text(task.verify, one_line=True)
                    adds.append(item)
                if edits or adds:
                    intent["tasks"] = {"was": was, "edit": edits, "add": adds}
                    # Core resolves proves from this snapshot, even with no criterion changes.
                    intent.setdefault(
                        "acceptance",
                        {
                            "was": [{"id": x["id"], "text": x["text"]} for x in doc.get("acceptance") or []],
                            "edit": {},
                            "add": [],
                        },
                    )
            if self.links is not None:
                now = doc.get("links") or {"repos": [], "branches": {}, "prs": [], "external": []}
                was = {
                    "repos": list(now.get("repos") or []),
                    "branches": dict(now.get("branches") or {}),
                    "prs": list(now.get("prs") or []),
                    "external": list(now.get("external") or []),
                }
                repos = sorted(set(self.links.repos))
                branches = {r: b for r, b in self.links.branches.items() if r in repos}
                if set(repos) != set(was["repos"]) or branches != was["branches"]:
                    intent["links"] = {"was": was, "repos": repos, "branches": branches}
            if self.people:
                intent["people"] = {
                    role: {"add": sorted(set(c.add)), "remove": sorted(set(c.remove))}
                    for role, c in self.people.items()
                    if c.add or c.remove
                }
                if not intent["people"]:
                    del intent["people"]
            if self.images:
                intent["images"] = _images_of(self.images)
            return sequence.validate_intent(intent)
        except (TextError, sequence.SequenceInvalid, KeyError, TypeError) as exc:
            raise fail("invalid.input", f"That edit can't be signed: {exc}", exit=1) from None


class QuickCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    cmd: Annotated[str, StringConstraints(min_length=1, max_length=300)] | None = None


class QuickStart(Action):
    """B11 step (ii): a quick task on ONE review and ONE confirmation. The signer signs the ticket (labelled quick), its
    requirements, the acceptance criteria and tasks (one per check), the plan derived from them, both approvals and,
    only when the person asks for it, the agent access, last. This server only relays the intent."""

    action: Literal["quick_start"]
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    description: str = Field(default="", max_length=65536)
    checks: list[QuickCheck] = Field(default_factory=list, max_length=4)
    agent: QuickAgent | None = None

    def intent(self) -> dict:
        from orch.canon.text import TextError, normalize_text
        from orch.host import sequence

        try:
            title = normalize_text(self.title, one_line=True)
            description = normalize_text(self.description).strip("\n") or title
            checks = [
                {
                    "text": normalize_text(c.text, one_line=True),
                    "cmd": None if c.cmd is None else normalize_text(c.cmd, one_line=True),
                }
                for c in self.checks
            ] or [{"text": title, "cmd": None}]
            return sequence.validate_intent(
                {
                    "v": 1,
                    "kind": "quick_task",
                    "title": title,
                    "description": description,
                    "checks": checks,
                    "agent": None if self.agent is None else {"hours": self.agent.hours, "scope": "workable"},
                }
            )
        except (TextError, sequence.SequenceInvalid) as exc:
            raise fail("invalid.input", f"That task can't be signed: {exc}", exit=1) from None


# ---- Workspace settings, members and grants (M4, #306) -------------------------------------------------------------
# Closed and typed like the ticket actions above. `repos` is not an argument of `settings_set`: the Repos addon owns
# repo editing, and `extra="forbid"` refuses it. `grant` (issuing) is not an action: the signer draws the secret.

Person = Annotated[str, StringConstraints(pattern=r"^p_[0-9a-f]{32}$")]
GrantId = Annotated[str, StringConstraints(pattern=r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
Role = Literal["owner", "maintainer", "member", "viewer"]
OneLine = AfterValidator(no_hidden)  # no control, bidi, zero-width or other hidden characters (by Unicode category)
Token = Literal["owner", "maintainer", "member", "ticket_owner", "assignees", "reviewers", "watchers"]
PolicyGate = Literal["requirements", "plan", "verify", "code"]
TicketType = Literal["feature", "bug", "chore", "spike", "epic"]
MAX_PK = 87  # base64url of a 65-byte public key, no padding
MAX_CERT_BYTES = 16384  # a device certificate is a few hundred bytes; anything near this is not one
PK_BYTES = 65


class SettingsSet(Action):
    """`settings.set {set:{grant_hours?, claim_ttl_min?, lease_ttl_min?}}`; `repos` is refused (the Repos addon)."""

    action: Literal["settings_set"]
    grant_hours: Annotated[int, Field(ge=1, le=168)] | None = None  # core GRANT_MAX_HOURS (cross-checked by a test)
    claim_ttl_min: Annotated[int, Field(ge=15, le=1440)] | None = None
    lease_ttl_min: Annotated[int, Field(ge=5, le=1440)] | None = None
    #: B6 (owner decision F): the workspace's allowed ways to sign; `terminal` is always present, `touchid` is reserved
    sign_methods: Annotated[list[Literal["terminal", "click", "phone"]], Field(min_length=1, max_length=3)] | None = (
        None
    )

    @model_validator(mode="after")
    def something(self):
        if (
            self.grant_hours is None
            and self.claim_ttl_min is None
            and self.lease_ttl_min is None
            and self.sign_methods is None
        ):
            raise ValueError("change at least one setting")
        if self.sign_methods is not None and (
            "terminal" not in self.sign_methods or len(set(self.sign_methods)) != len(self.sign_methods)
        ):
            raise ValueError("sign_methods always contains terminal, once each")
        return self


class WorkspaceIcon(BaseModel):
    """An icon and a colour of core's closed lists (`orch.wsicons`); nothing else is accepted."""

    model_config = ConfigDict(extra="forbid")
    icon: str
    colour: str

    @model_validator(mode="after")
    def closed(self):
        from orch.wsicons import COLOUR_LABELS, ICON_LABELS

        if self.icon not in ICON_LABELS or self.colour not in COLOUR_LABELS:
            raise ValueError("icon: an icon and a colour that orch offers")
        return self


class IconSet(Action):
    """`settings.set {set:{icon}}`: the workspace icon (null: back to the automatic one). Signed with a click; the host
    checks the closed lists again."""

    action: Literal["icon_set"]
    icon: WorkspaceIcon | None


class RepoSet(Action):
    """`settings.set {set:{repos:{name: {path, remote?, default_branch?}}}}`: add a repo or change one (Repos add-on).
    The host checks the path again (absolute, an existing git folder, not the home folder, not the orch store)."""

    action: Literal["repo_set"]
    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
    path: Annotated[str, StringConstraints(min_length=1, max_length=1024), OneLine]
    remote: Annotated[str, StringConstraints(min_length=1, max_length=512), OneLine] | None = None
    default_branch: Annotated[str, StringConstraints(min_length=1, max_length=255), OneLine] | None = None

    @model_validator(mode="after")
    def plain(self):
        if not self.path.startswith("/"):
            raise ValueError("write the full path, starting with /")
        if self.remote is not None and not adapters.settings.REMOTE.fullmatch(self.remote):
            raise ValueError("a remote is an https:// address without a login")
        return self

    def entry(self) -> dict:
        return {"path": self.path} | {
            k: v for k, v in (("remote", self.remote), ("default_branch", self.default_branch)) if v is not None
        }


class RepoRemove(Action):
    """Take a repo off the workspace's list. Nothing on disk is touched."""

    action: Literal["repo_remove"]
    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]


class PolicyBody(BaseModel):
    """F1 5.7 `Policy`, exactly: five keys, tokens from the closed list."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=False)
    approvers: Annotated[list[Token], Field(min_length=1, max_length=8)]
    count: Annotated[int, Field(ge=1, le=9)]
    not_: Annotated[list[Token], Field(max_length=8, alias="not")]
    applies: Literal["all", "off"] | Annotated[list[TicketType], Field(min_length=1, max_length=16)]
    independent: bool

    def canonical(self) -> dict:
        """The policy in the canonical form F1 5.7 stores (lists sorted, repeats removed): what is sent and shown."""
        return {
            "approvers": sorted(set(self.approvers)),
            "count": self.count,
            "not": sorted(set(self.not_)),
            "applies": self.applies if isinstance(self.applies, str) else sorted(set(self.applies)),
            "independent": self.independent,
        }


class PolicySet(Action):
    """`policy.set {gates:{Gate: Policy}}` for the workspace default of one gate (a ticket override is not offered)."""

    action: Literal["policy_set"]
    gate: PolicyGate
    policy: PolicyBody

    @model_validator(mode="after")
    def code_rules(self):
        # For `code`, `not` always includes `assignees` and `independent` is true (D59); the host refuses less.
        if self.gate == "code" and ("assignees" not in self.policy.not_ or not self.policy.independent):
            raise ValueError("the code review is never approved by the assignees and is always independent")
        return self


def _strict_json_object(text: str) -> dict:
    """Strict JSON: an object, no duplicate keys, bounded depth and size, integers only (the host's `loads_strict`)."""
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_float=_no_float, parse_constant=_no_float)
        _valid(value)
    except (ValueError, RecursionError):
        raise ValueError("the certificate is not strict JSON") from None
    if not isinstance(value, dict):
        raise ValueError("the certificate is a JSON object")
    return value


def _no_float(_text):
    raise ValueError("numbers in a certificate are integers")


class MemberAdd(Action):
    """`member.add`: the invitee made their own key and first device certificate; the owner signs the roster change.

    The public key and the certificate are the invitee's own files, uploaded by the person (host contract R9: the
    certificate goes through `human.prepare`'s `files`, the key is an argument). Nothing is read from a path."""

    action: Literal["member_add"]
    name: Annotated[str, StringConstraints(min_length=1, max_length=64), OneLine]
    role: Role
    pk: Annotated[str, StringConstraints(min_length=MAX_PK, max_length=MAX_PK, pattern=r"^[A-Za-z0-9_-]+$")]
    cert: Annotated[str, StringConstraints(min_length=2, max_length=MAX_CERT_BYTES)]

    @model_validator(mode="after")
    def check_files(self):
        if self.name != self.name.strip():
            raise ValueError("the name has spaces at its ends")
        try:
            raw = base64.urlsafe_b64decode(self.pk + "=")
        except ValueError:
            raise ValueError("the public key is not base64url") from None
        if len(raw) != PK_BYTES or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != self.pk:
            raise ValueError("the public key is 65 bytes, base64url")
        if len(self.cert.encode("utf-8", "strict")) > MAX_CERT_BYTES:
            raise ValueError("the certificate is too large")
        _strict_json_object(self.cert)
        return self


class MemberRole(Action):
    action: Literal["member_role"]
    person: Person
    role: Role


class MemberRemove(Action):
    action: Literal["member_remove"]
    person: Person


DeviceId = Annotated[str, StringConstraints(pattern=r"^d_[0-9a-f]{32}$")]


class DeviceRemove(Action):
    """#1198 C15, `device.remove {device}`: one click. Core refuses the device that signs the click and the last working
    device of an owner (`device.lockout`); this server only relays the device id the page showed."""

    action: Literal["device_remove"]
    device: DeviceId


# the dashboard add-on is always on; no other package is offered here
AddonName = Literal[
    "start-agent", "terminals", "worktrees", "publish", "usage", "estimate", "explorer", "wiki", "quick"
]


class AddonGrant(Action):
    """`addon.grant {name}`: turn a shipped add-on on. Signed (the typed word names the add-on); the host reads the
    package itself and the review shows what it may do."""

    action: Literal["addon_grant"]
    name: AddonName


MarketName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,39}$")]
#: core `marketstate._STAGE`: the host's own stage id
StageId = Annotated[str, StringConstraints(pattern=r"^st_[0-9a-f]{32}$")]


class MarketGrant(Action):
    """`addon.grant {name, staged}`: install or update the package the host staged and the person reviewed (A10).
    Exactly these two keys (`extra="forbid"`). **One click** where click-to-sign is on (V3-D38, owner 24 Oct: the
    signer shows the whole review, the marketplace lines "per host, not signed" included), the terminal hand-off where
    it is off. `publish` is terminal only (`click.prepare` refuses it). It has no agent route (the dashboard session
    is the person's). The host re-reads the stage and judges it again when the signed request is submitted."""

    action: Literal["market_grant"]
    name: MarketName
    staged: StageId


class AddonDisable(Action):
    """`addon.disable {name}`: turn it off. Its data stays; refused while its work is running."""

    action: Literal["addon_disable"]
    name: AddonName


#: core `publishOrigin` (schemas/common.json): https, or http on this computer only; no path, lower case
Origin = Annotated[
    str,
    StringConstraints(
        max_length=300,
        pattern=r"^(?:https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[1-9][0-9]{0,4})?"
        r"|http://(?:localhost|127\.0\.0\.1|10\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|172\.(?:1[6-9]|2[0-9]|3[01])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|192\.168\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9]))(?::[1-9][0-9]{0,4})?)$",
    ),
]


class PublishSetup(Action):
    """`settings.set {set:{publish:{server, share}}}`: where shared files are sent and where a link opens. Signed; the
    review says both addresses in words. Only a person changes it (never an agent), and always in the terminal: it
    decides where files go."""

    action: Literal["publish_setup"]
    server: Origin
    share: Origin

    @model_validator(mode="after")
    def different(self):
        if self.server == self.share:
            raise ValueError("the files and the links use two different addresses")
        return self


class PublishAddresses(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    server: Origin
    share: Origin

    @model_validator(mode="after")
    def different(self):
        PublishSetup(action="publish_setup", server=self.server, share=self.share)
        return self


class AddonEnable(Action):
    action: Literal["addon_enable"]
    name: Literal["publish"]
    publish: PublishAddresses

    def settings(self) -> dict:
        return {"publish": self.publish.model_dump()}

    def intent(self, grant: dict) -> dict:
        from orch.host import sequence

        try:
            return sequence.validate_intent(
                {"v": 1, "kind": "addon_enable", "name": self.name, "settings": self.settings(), "grant": grant}
            )
        except sequence.SequenceInvalid as exc:
            raise fail("invalid.input", f"That add-on can't be signed: {exc}", exit=1) from None


class PublishShare(Action):
    """`publish.share {staged_id}` through `human.prepare`: a person's signature over the exact words the host builds
    from the staged copy (ticket, kind, days, server, every file with size and hash). The page then calls
    `POST /shares/send`; the host refuses it without this signature. `key` is the ticket the copy belongs to (the
    prepared review must be about that ticket). Terminal only: the click signer does not serve this op."""

    action: Literal["publish_share"]
    key: Key
    staged_id: Annotated[str, StringConstraints(pattern=r"^ps_[0-9a-f]{32}$")]


class GrantRevoke(Action):
    action: Literal["grant_revoke"]
    grant: GrantId
    reason: Annotated[str, StringConstraints(min_length=1, max_length=200), OneLine] | None = None


class Move(Action):
    """`move`: a workspace owner or maintainer returns unclaimed work to `backlog` or `open` (host contract 0.8, H6b).
    Served only through `human.prepare`; the host refuses a claimed ticket and any other target."""

    action: Literal["move"]
    key: Key
    to: Literal["backlog", "open"]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=200), OneLine] | None = None


AcceptanceText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000), OneLine]


class AcceptCriterion(Action):
    action: Literal["accept_criterion"]
    key: Key
    ac: AcId
    note: AcceptanceText | None = None


class WithdrawAcceptance(Action):
    action: Literal["withdraw_acceptance"]
    key: Key
    ac: AcId
    reason: AcceptanceText


class MarkTaskDone(Action):
    action: Literal["mark_task_done"]
    key: Key
    task: TaskId


class RunnerGrant(Action):
    """M7b, the runner grant (D-P2-27): a click request, or a terminal hand-off with runner set by this
    server, never by the browser. The click path maps to the
    signer's `prepare {op:"grant", args:{hours, scope}, deposit:true}`; the signer itself sets `label:"runner"` and
    `verbs:"agent"` and hands the secret to the runner, so neither the label nor the verbs are the browser's to send."""

    action: Literal["runner_grant"]
    hours: Annotated[int, Field(ge=1, le=168)] = 8  # core GRANT_MAX_HOURS; the host holds the workspace cap
    scope: Literal["all", "workable"] = "workable"
    #: B8 Extend: the runner grant this one replaces (one request, one signature, typed word); the signer revokes it
    extend: Annotated[str, StringConstraints(pattern=r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")] | None = None


class AgentAccess(Action):
    """#1198 C7, agent access for a tool outside the dashboard: a click request whose secret comes back sealed to the
    page's own key and is shown once there. Click only (never a terminal hand-off); the signer fixes the verbs
    (`agent`) and refuses a runner label, so neither is the browser's to send. A runner grant is `RunnerGrant`."""

    action: Literal["agent_access"]
    hours: Annotated[int, Field(ge=1, le=168)] = 8  # core GRANT_MAX_HOURS
    scope: Literal["all", "workable"] = "workable"
    label: Annotated[str, StringConstraints(min_length=1, max_length=40, strip_whitespace=True)] | None = None
    #: the page's one-use P-256 public key (65-byte point, base64url): sent to the signer at prepare, so the review the
    #: person approves is of this key; the dashboard server only relays it (it is public) and never holds a private key
    reveal_key: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{87}$")]

    @field_validator("label")
    @classmethod
    def _plain_label(cls, v):
        if v is not None and (v.lower() == "runner" or any(ord(c) < 32 or ord(c) == 127 for c in v)):
            raise ValueError("label is plain text and not runner")
        return v


MAX_HUB_CARD_BYTES = 16 * 1024


class HubJoin(Action):
    """#1198 C10, join a hub (selective only: a full join needs the key in passphrase custody, so it is the terminal's).
    Click only: the backup key is typed once in the page, sealed to the signer, and makes the owner proof; never a
    terminal hand-off. The card is the hub's own file, as text (strict JSON); the host verifies it, not this server."""

    action: Literal["hub_join"]
    hub_card: Annotated[str, StringConstraints(min_length=2, max_length=MAX_HUB_CARD_BYTES)]
    hub_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8,64}$")]
    mode: Literal["selective", "full"] = "selective"

    @model_validator(mode="after")
    def check_card(self):
        _strict_json_object(self.hub_card)
        return self


class HubModeFull(Action):
    """Run-9 feedback 6: switch the joined hub from selective to full. One click; the card is the host's pinned one."""

    action: Literal["hub_mode_full"]
    hub_card: Annotated[str, StringConstraints(min_length=2, max_length=MAX_HUB_CARD_BYTES)]
    hub_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8,64}$")]

    @model_validator(mode="after")
    def check_card(self):
        _strict_json_object(self.hub_card)
        return self


class HubLeave(Action):
    """#1198 C10, leave the hub this workspace is joined to. Click only, with the backup key sealed (see HubJoin)."""

    action: Literal["hub_leave"]


PrepareBody = Annotated[
    Approve
    | RequestChanges
    | Verdict
    | Answer
    | Comment
    | AddLabel
    | Close
    | Reopen
    | Move
    | AcceptCriterion
    | WithdrawAcceptance
    | MarkTaskDone
    | NewTicket
    | NewTicketSequence
    | EditTicket
    | QuickStart
    | Details
    | WriteSection
    | SettingsSet
    | IconSet
    | RepoSet
    | RepoRemove
    | PolicySet
    | MemberAdd
    | MemberRole
    | MemberRemove
    | DeviceRemove
    | AddonGrant
    | MarketGrant
    | AddonDisable
    | PublishSetup
    | AddonEnable
    | PublishShare
    | GrantRevoke
    | RunnerGrant
    | AgentAccess
    | HubJoin
    | HubModeFull
    | HubLeave,
    Field(discriminator="action"),
]


def _grant_landed(gateway: Gateway, ws: str, intent: dict, person, since) -> bool:
    """The last step of a quick task with agent access: a live runner grant of this person with exactly the reviewed
    length, issued after this request began. Unknown or unreadable counts as not landed (fail closed: the dialog keeps
    waiting and never says "done" for access that was not given)."""
    from orch.host import sequence

    if not isinstance(person, str) or not isinstance(since, str):
        return False
    try:
        grants = gateway.call(ws, "grant.list", {"active": True}).get("grants") or []
        hours = intent["agent"]["hours"]
        for g in grants:
            issued, expires = g.get("issued_at"), g.get("expires_at")
            if (
                g.get("person") == person
                and g.get("label") == "runner"
                and not g.get("revoked_at")
                and isinstance(issued, str)
                and isinstance(expires, str)
                and issued >= since
                and sequence._epoch(expires) - sequence._epoch(issued) == hours * 3600
            ):
                return True
    except (HostError, KeyError, TypeError, ValueError, AttributeError):
        return False
    return False


def sequence_progress(gateway: Gateway, ws: str, key, intent: dict, person=None, since=None) -> dict | None:
    """B11: how many steps of a sequence are in the ticket's log now, read from the host (`ticket.get`): the created
    ticket, then the fields, then each section whose latest hash is the intent's text. A display aid for the terminal
    path (the signer drives the run in the person's terminal); it decides nothing."""
    from orch import canon
    from orch.host import sequence

    total = len(sequence.steps(intent))
    if not isinstance(key, str) or intent["kind"] == "edit_ticket":  # an edit shows no step count (follow-up)
        return None
    try:
        data = gateway.call(ws, "ticket.get", {"ref": key})
    except HostError:
        return None
    doc, sections, gates = data.get("doc") or {}, data.get("sections") or {}, data.get("gates") or {}
    done = 1
    ac = tasks = 0
    for step in sequence.steps(intent)[1:]:
        kind = step["kind"]
        if kind == "grant":  # a workspace event, not in the ticket: read from the grants, and counted like any step
            if not _grant_landed(gateway, ws, intent, person, since):
                break
            done += 1
            continue
        if kind == "set":
            labels = ["quick"] if intent["kind"] == "quick_task" else None
            ok = (
                all(doc.get(k) == v for k, v in intent.get("set", {}).items())
                if labels is None
                else doc.get("labels") == labels
            )
        elif kind == "section":
            sid = step["args"]["section"]
            history = sections.get(sid) or []
            ok = bool(history) and history[-1].get("hash") == canon.section_hash(step["args"]["message"])
        elif kind == "ac":
            ac += 1
            ok = len(doc.get("acceptance") or []) >= ac
        elif kind == "people":
            role, add = step["args"]["role"], step["args"]["add"]
            have = [doc.get("owner")] if role == "owner" else (doc.get("people") or {}).get(role) or []
            ok = set(add) <= set(have)
        elif kind == "task":
            tasks += 1
            ok = len(doc.get("tasks") or []) >= tasks
        else:  # approve
            ok = (gates.get(step["args"]["gate"]) or {}).get("state") == "approved"
        if not ok:
            break
        done += 1
    return {"done": done, "of": total}


def host_call(action: Action, current_labels: list[str] | None = None) -> tuple[str, dict]:
    """(host op, args) for a typed action. The only place an op name is chosen, and it is a literal."""
    match action:
        case Approve():
            return "approve", {"ref": action.key, "gate": action.gate}
        case RequestChanges():
            return "request_changes", {"ref": action.key, "gate": action.gate, "message": action.message}
        case Verdict():
            args = {"ref": action.key, "outcome": action.outcome}
            return "verdict", args | ({"message": action.message} if action.message else {})
        case Answer():
            args = {"ref": action.key, "question": action.question}
            args |= {"option": action.option} if action.option else {}
            return "answer", args | ({"message": action.message} if action.message else {})
        case Comment():
            return "log", {"ref": action.key, "message": action.message}
        case AddLabel():
            labels = sorted({*(current_labels or []), action.label})
            return "set", {"ref": action.key, "pairs": ["labels=" + ",".join(labels)]}
        case Close():
            args = {"ref": action.key, "resolution": action.resolution}
            args |= {"duplicate_of": action.duplicate_of} if action.duplicate_of else {}
            return "close", args | ({"message": action.message} if action.message else {})
        case Reopen():
            return "reopen", {"ref": action.key} | ({"message": action.message} if action.message else {})
        case AcceptCriterion():
            return "ac.accept", {"ref": action.key, "ac": action.ac} | ({"note": action.note} if action.note else {})
        case WithdrawAcceptance():
            return "ac.unaccept", {"ref": action.key, "ac": action.ac, "reason": action.reason}
        case MarkTaskDone():
            return "task.mark_done", {"ref": action.key, "task": action.task}
        case Move():
            return "move", {"ref": action.key, "to": action.to} | ({"reason": action.reason} if action.reason else {})
        case NewTicket():
            return "new", {"title": action.title, "type": action.type}
        case NewTicketSequence():
            intent = action.intent()
            return "new", {"title": intent["title"], "type": intent["ticket_type"]}
        case QuickStart():
            intent = action.intent()
            return "new", {"title": intent["title"], "type": "chore"}
        case EditTicket():  # its op comes from the intent, which needs the ticket as read now: see plan()
            raise AssertionError("plan() builds an edit's first step")
        case Details():
            pairs = [f"{k}={v}" for k, v in (("priority", action.priority), ("size", action.size)) if v]
            if action.labels is not None:
                pairs.append("labels=" + ",".join(sorted(set(action.labels))))
            if action.parent:
                pairs.append(f"parent={action.parent}")
            return "set", {"ref": action.key, "pairs": pairs}
        case WriteSection():
            return "section.set", {"ref": action.key, "section": action.section, "message": action.text}
        case SettingsSet():
            return "settings.set", {"set": action.model_dump(include={*DURATIONS, "sign_methods"}, exclude_none=True)}
        case IconSet():
            return "settings.set", {"set": {"icon": action.icon.model_dump() if action.icon else None}}
        case AddonEnable():
            return "settings.set", {"set": action.settings()}
        case PublishSetup():
            return "settings.set", {"set": {"publish": {"server": action.server, "share": action.share}}}
        case PublishShare():
            return "publish.share", {"staged_id": action.staged_id}
        case RepoSet():
            return "settings.set", {"set": {"repos": {action.name: action.entry()}}}
        case RepoRemove():
            return "settings.set", {"set": {"repos": {action.name: None}}}
        case PolicySet():
            return "policy.set", {"gates": {action.gate: action.policy.canonical()}}
        case MemberAdd():
            # `cert` names the file (`-`); its bytes travel in `files` (host_files). `pk` is a plain argument.
            return "member.add", {"name": action.name, "role": action.role, "pk": action.pk, "cert": "-"}
        case MemberRole():
            return "member.role", {"person": action.person, "role": action.role}
        case MemberRemove():
            return "member.remove", {"person": action.person}
        case DeviceRemove():
            return "device.remove", {"device": action.device}
        case AddonGrant():
            return "addon.grant", {"name": action.name}
        case MarketGrant():
            return "addon.grant", {"name": action.name, "staged": action.staged}
        case AddonDisable():
            return "addon.disable", {"name": action.name}
        case GrantRevoke():
            return "grant.revoke", {"grant": action.grant} | ({"reason": action.reason} if action.reason else {})
        case RunnerGrant():
            return "grant", {"hours": action.hours, "scope": action.scope} | (
                {"extend": action.extend} if action.extend else {}
            )
        case AgentAccess():
            return "grant", {"hours": action.hours, "scope": action.scope} | (
                {"label": action.label} if action.label else {}
            )
        case HubJoin():
            return "hub.join", {
                "hub_card": _strict_json_object(action.hub_card),
                "hub_id": action.hub_id,
                "mode": action.mode,
            }
        case HubModeFull():
            return "hub.mode", {"hub_card": _strict_json_object(action.hub_card), "mode": "full"}
        case HubLeave():
            return "hub.leave", {}
    raise AssertionError("closed allowlist")  # pragma: no cover


#: core `orch.publishclient.approval.MARK` (cross-checked by a test): the first line of every share approval
PUBLISH_MARK = "Publish approval: share files outside this computer"
DURATIONS = ("grant_hours", "claim_ttl_min", "lease_ttl_min")
WORKSPACE_OPS = (
    "settings.set",
    "policy.set",
    "member.add",
    "member.role",
    "member.remove",
    "device.remove",
    "grant.revoke",
    "move",
    "addon.grant",
    "addon.disable",
)


def host_files(action: Action) -> dict | None:
    """The inner op's files for `human.prepare` (R9): only a member's certificate, inline as `b64`."""
    if isinstance(action, MemberAdd):
        return {"cert": {"b64": base64.b64encode(action.cert.encode()).decode()}}
    return None


def _acceptance_event_ok(ev: dict, person: str | None, payload: set[str]) -> bool:
    """A prepared manual-acceptance event: only the envelope and the keys of its op, from a person (the caller when
    known). The signer checks the same again; this refuses a host that prepared more or other than what was asked."""
    actor = ev.get("actor")
    return (
        set(ev) <= set(adapters.handoff.ENVELOPE) | payload
        and isinstance(actor, dict)
        and actor.get("kind") == "person"
        and (person is None or actor.get("id") == person)
    )


def expected_material(action: Action):
    """What the host's prepared MATERIAL must say, for the actions whose words are not in the events: the statement
    file of a manual acceptance (its note or reason is only there). `None` for every other action."""
    from orch.host import acceptance

    match action:
        case AcceptCriterion() | WithdrawAcceptance():
            withdraw = isinstance(action, WithdrawAcceptance)

            def check(material: dict, events: list) -> bool:
                statement = material.get("statement")
                parsed = acceptance.parse(statement) if isinstance(statement, str) else None
                return (
                    parsed is not None
                    and len(events) == 1
                    and parsed["kind"] == ("withdraw" if withdraw else "accept")
                    and parsed["ac"] == action.ac
                    and parsed["reason" if withdraw else "note"] == (action.reason if withdraw else action.note)
                    and acceptance.matches(events[0], statement)
                )

            return check
    return None


def expected(action: Action, person: str | None = None):
    """What the host's prepared events must be, for the actions that are not about a ticket. A host that prepares
    anything else (another type, another person, other values) is refused before the person sees it. `person` is the
    caller (the runner grant must be the caller's own)."""

    def one(events: list, type_: str, **fields) -> bool:
        if len(events) != 1 or events[0].get("type") != type_:
            return False
        return all(adapters.handoff.cj(events[0].get(k)) == adapters.handoff.cj(v) for k, v in fields.items())

    match action:
        case SettingsSet() | IconSet() | RepoSet() | RepoRemove() | PublishSetup() | AddonEnable():
            _, args = host_call(action)
            return lambda ev: one(ev, "settings.changed", set=args["set"])
        case PublishShare():
            # one `log.added` note on the ticket whose words are the host's approval text for exactly this staged copy
            return lambda ev: (
                len(ev) == 1
                and ev[0].get("type") == "log.added"
                and isinstance(ev[0].get("text"), str)
                and ev[0]["text"].startswith(PUBLISH_MARK + "\n")
                and f"\nStaged copy: {action.staged_id}\n" in ev[0]["text"] + "\n"
            )
        case PolicySet():
            _, args = host_call(action)
            return lambda ev: one(ev, "policy.changed", gates=args["gates"]) and "ref" not in ev[0]
        case NewTicketSequence() | QuickStart():
            intent = action.intent()
            kind = intent.get("ticket_type", "chore")
            return lambda ev: (
                one(ev, "ticket.created", title=intent["title"], ticket_type=kind)
                and (person is None or ev[0].get("owner") == person)
            )
        case EditTicket():
            # the first event is one ordinary edit (the signer checks it against the intent, head included)
            return lambda ev: len(ev) == 1 and ev[0].get("type") in ("ticket.updated", "people.changed")
        case AcceptCriterion():
            return lambda ev: (
                one(ev, "artifact.added", kind="feedback", ac=action.ac)
                and _acceptance_event_ok(ev[0], person, {"name", "kind", "sha256", "bytes", "ac", "label"})
                and re.fullmatch(rf"accepted {action.ac} [0-9a-f]{{16}}", str(ev[0].get("label"))) is not None
            )
        case WithdrawAcceptance():
            return lambda ev: (
                one(ev, "artifact.replaced", kind="feedback", label=f"withdrawn {action.ac}")
                and _acceptance_event_ok(ev[0], person, {"name", "kind", "sha256", "bytes", "label", "replaces"})
            )
        case MarkTaskDone():
            return lambda ev: one(ev, "task.done", task=action.task) and _acceptance_event_ok(ev[0], person, {"task"})
        case Move():
            return lambda ev: one(ev, "ticket.moved", to=action.to) and ev[0].get("reason") == action.reason
        case MemberRole():
            return lambda ev: one(ev, "role.changed", person=action.person, role=action.role)
        case MemberRemove():
            return lambda ev: one(ev, "member.removed", person=action.person)
        case DeviceRemove():
            return lambda ev: one(ev, "device.removed", device=action.device) and set(ev[0]) <= (
                set(adapters.handoff.ENVELOPE) | {"device"}
            )
        case AddonGrant():
            return lambda ev: one(ev, "addon.granted", name=action.name)
        case MarketGrant():
            return lambda ev: one(ev, "addon.granted", name=action.name)
        case AddonDisable():
            return lambda ev: one(ev, "addon.disabled", name=action.name)
        case GrantRevoke():
            return lambda ev: one(ev, "grant.revoked", grant=action.grant) and ev[0].get("reason") == action.reason
        case RunnerGrant():
            # Exactly one `grant.issued`: label runner, verbs agent, the hours and scope asked for (the host applies
            # the workspace setting), and the caller as its person (the actor). Anything else is not shown, so it is
            # never signed (M7b).
            if action.extend:  # B8: the new grant, then the revoke of exactly the one named (nothing else, no reason)
                return lambda ev: (
                    len(ev) == 2
                    and ev[0].get("type") == "grant.issued"
                    and ev[0].get("label") == "runner"
                    and ev[0].get("verbs") == "agent"
                    and ev[0].get("hours") == action.hours
                    and ev[0].get("scope") == action.scope
                    and set(ev[0]) <= GRANT_ISSUED_KEYS
                    and ev[1].get("type") == "grant.revoked"
                    and ev[1].get("grant") == action.extend
                    and ev[1].get("reason") == "extended"
                    and person is not None
                    and all(
                        isinstance(e.get("actor"), dict)
                        and e["actor"].get("kind") == "person"
                        and e["actor"].get("id") == person
                        for e in ev
                    )
                )
            return lambda ev: (
                one(ev, "grant.issued", label="runner", verbs="agent", hours=action.hours, scope=action.scope)
                # Review I2: exactly the grant.issued payload (plus the envelope), nothing a future field could add.
                and set(ev[0]) <= GRANT_ISSUED_KEYS
                and type(ev[0].get("hours")) is int
                and 1 <= ev[0]["hours"] <= 168
                and person is not None
                and isinstance(ev[0].get("actor"), dict)
                and ev[0]["actor"].get("kind") == "person"
                and ev[0]["actor"].get("id") == person
            )
        case AgentAccess():
            # Exactly one `grant.issued` of verbs agent, the hours, scope and label asked for (no label when none was),
            # nothing a future field could add, and the caller as its person. A runner label never passes.
            return lambda ev: (
                one(ev, "grant.issued", verbs="agent", hours=action.hours, scope=action.scope, label=action.label)
                and set(ev[0]) <= GRANT_ISSUED_KEYS
                and ev[0].get("label") != "runner"
                and person is not None
                and isinstance(ev[0].get("actor"), dict)
                and ev[0]["actor"].get("kind") == "person"
                and ev[0]["actor"].get("id") == person
            )
        case HubJoin():
            # exactly one `hub.joined`: a selective binding to the hub the person was told, the caller as its person
            return lambda ev: (
                one(ev, "hub.joined", mode=action.mode, hub_id=action.hub_id)
                and person is not None
                and isinstance(ev[0].get("actor"), dict)
                and ev[0]["actor"].get("kind") == "person"
                and ev[0]["actor"].get("id") == person
            )
        case HubModeFull():
            return lambda ev: (
                one(ev, "hub.joined", mode="full", hub_id=action.hub_id)
                and person is not None
                and isinstance(ev[0].get("actor"), dict)
                and ev[0]["actor"].get("kind") == "person"
                and ev[0]["actor"].get("id") == person
            )
        case HubLeave():
            return lambda ev: (
                one(ev, "hub.left")
                and person is not None
                and isinstance(ev[0].get("actor"), dict)
                and ev[0]["actor"].get("kind") == "person"
                and ev[0]["actor"].get("id") == person
            )
        case MemberAdd():
            cert = _strict_json_object(action.cert)
            return lambda ev: (
                one(ev, "member.added", name=action.name, role=action.role, pk_pub=action.pk, device_cert=cert)
                and isinstance(ev[0].get("person"), str)
                and ev[0]["person"].startswith("p_")
            )
    return None


#: A prepared `grant.issued`: the envelope (adapters.handoff.ENVELOPE) and the event schema's payload fields.
GRANT_ISSUED_KEYS = frozenset(
    {"v", "id", "type", "actor", "auth", "hash_v", "roster_v", "based_on", "sig"}
    | {"grant", "scope", "verbs", "issued_at", "hours", "expires_at", "secret_hash", "label"}
)


@dataclass(frozen=True)
class Plan:
    """What one action means to the host: the op and arguments (the only thing hashed), the files, and the intent."""

    op: str
    args: dict
    files: dict | None
    base: object
    person: str
    #: B11: the `sequence` meta of `human.prepare` (one review, N signatures), or None
    sequence: dict | None = None

    def idem(self, ws: str, retry: int = 0) -> str:
        return intent_idem(self.person, ws, self.op, self.args, self.base, self.files, retry)


GATE_OF = {Approve: lambda a: a.gate, RequestChanges: lambda a: a.gate, Verdict: lambda a: "verify"}


def plan(gateway: Gateway, ws: str, body: Action) -> Plan:
    """Canonical host content, plus a gate or question version only; other actions have no base."""
    link = gateway.link(ws)
    key = getattr(body, "key", None)
    data = gateway.call(ws, "ticket.get", {"ref": key}) if key is not None else None
    labels = list(data["doc"].get("labels", [])) if isinstance(body, AddLabel) and data else None
    if isinstance(body, EditTicket):
        from orch.host import sequence

        edit_intent = body.intent(data)
        op, args = sequence.step_args(edit_intent, 0, None)  # the first step of the edit, naming the ticket
    else:
        edit_intent = None
        op, args = host_call(body, labels)  # the only place an op name is chosen
    if isinstance(body, Details) and body.via == "estimate" and not gateway.addon_on(link, "estimate"):
        raise fail("addon.disabled", "Estimate is switched off here. Switch it on in Settings, Add-ons.", exit=3)
    addon_base_ = None
    if isinstance(body, AddonDisable | AddonGrant):
        from .addon_switches import addon_base, check_off

        if isinstance(body, AddonDisable):
            check_off(gateway, ws, body.name)  # not while its work is running (the page greys the switch too)
        # the latest switch of this add-on is part of the intent: on, off, on is three requests
        addon_base_ = addon_base(link, body.name, isinstance(body, AddonGrant))
    if op in WORKSPACE_OPS and link.welcome.get("ops", {}).get(op) != 1:
        # policy.set and settings.set wait for F1 events on a real host (R5): never offered, never guessed.
        raise fail("op.unsupported", f"This host does not serve {op} yet.", exit=1)
    if op == "publish.share" and link.welcome.get("ops", {}).get("publish.send") != 1:
        raise fail(
            "op.unsupported", "This host does not serve Publish.", exit=1
        )  # the signed half is useless without it
    base = None
    if data is not None:
        gate = GATE_OF.get(type(body))
        if gate:
            base = gate_base(data, gate(body))
        elif isinstance(body, Answer):
            base = question_base(data, body.question)
    if addon_base_ is not None:
        base = addon_base_
    if isinstance(body, MarketGrant):
        # one intent per stage: a new stage (a new review) is a new request, the same stage is one
        base = {"addon": body.name, "staged": body.staged}
    person = link.welcome["peer"]["person"]
    meta = None
    if isinstance(body, NewTicketSequence | QuickStart | EditTicket):
        from orch.host import sequence

        intent = edit_intent if edit_intent is not None else body.intent()
        digest = sequence.digest_of(ws, person, intent)
        meta = {"step": 0, "of": len(sequence.steps(intent)), "digest": digest, "intent": intent}
        base = {"sequence": digest}  # the whole intent is part of the intent's idem
    if isinstance(body, AddonEnable):
        from orch.host import sequence

        from .addon_switches import addon_base

        if link.welcome.get("ops", {}).get("addon.preview") != 1:
            raise fail("op.unsupported", "This host does not serve addon.preview yet.", exit=1)
        switch_base = addon_base(link, body.name, True)
        intent = body.intent(gateway.call(ws, "addon.preview", {"name": body.name})["grant"])
        digest = sequence.digest_of(ws, person, intent)
        meta = {"step": 0, "of": 2, "digest": digest, "intent": intent}
        base = {"sequence": digest, "addon": switch_base}
    if isinstance(body, AgentAccess):
        base = {
            "reveal_key": body.reveal_key
        }  # a new page key is a new request: one key, one grant (the idem keeps it)
    return Plan(op, args, host_files(body), base, person, meta)


#: How many earlier ENDED (cancelled, expired) attempts of one action a single press steps past. The cap bounds the
#: work per press: walking only replays idem records the host already holds (it creates nothing), so a press costs at
#: most NEXT_RETRY_HOPS + 1 host calls and makes exactly one new request, at the first retry whose attempt has not
#: ended. Nobody can burn retries to deny service: the walk makes no request, and only a cancel ends one (the
#: session's open-request cap limits those). Past the cap the answer is `intent.exhausted`; the page then carries on
#: from the retry after the last one stepped past (`retry + NEXT_RETRY_HOPS + 1`), so nothing is ever stuck.
NEXT_RETRY_HOPS = 16
ENDED_REQUEST = re.compile(r"^the request is (cancelled|expired)$")


def request_ended(got) -> bool:
    """The answer is a request of this very intent that ended unsigned."""
    request = got.get("request") if isinstance(got, dict) else None
    return isinstance(request, dict) and request.get("state") in ("cancelled", "expired")


def send_prepare(plan_: Plan, ws: str, retry: int, call, pending=None):
    """Prepare with the intent's idem at the first retry (from `retry` up) whose attempt has not ended.

    The idem is the intent plus `retry`. An attempt that ENDED unsigned (cancelled, expired) can never be signed, so
    pressing again steps to the next retry: a fresh request, never the dead one. An OPEN attempt is returned as it is
    (a double click, a second tab, an agent: one request per person and action); a SIGNED one is returned too and
    stays "Already done" (never stepped past). The terminal path sees an ended request in the host's answer, the click
    path as the signer's `the request is <ended>`; both are stepped past.

    Retention expiry of a record (`idem.expired`, 24 h) is not stepped past: an earlier one may have been signed, so
    only the person's explicit "Start again" advances `retry` there.

    `idem.conflict` means the host holds an attempt bound to another path or signer. Report it as
    `intent.open` while a matching op+key is open, otherwise `intent.blocked`. Neither prepares another request.

    A HostError leaves with `retry_used`: the retry whose attempt it is about."""
    used = retry
    try:
        for hop in range(NEXT_RETRY_HOPS + 1):
            used = retry + hop
            try:
                got = call(plan_.idem(ws, used))
            except HostError as exc:
                said = str(exc.error.get("message", "")) if exc.error.get("code") == "human.request_state" else ""
                if ENDED_REQUEST.fullmatch(said):
                    continue
                raise
            if request_ended(got):
                continue
            return got
        raise HostError(
            {
                "code": "intent.exhausted",
                "message": "This was started and then cancelled or left to expire many times in a row. "
                "Press it once more to carry on.",
                "retryable": False,
            },
            exit=5,
        )
    except HostError as exc:
        exc.retry_used = used
        code = exc.error.get("code")
        if code == "idem.conflict":
            rows = pending().get("requests", []) if pending else None
            open_ = next(
                (
                    r
                    for r in rows or []
                    if r.get("op") == plan_.op
                    and r.get("key") == plan_.args.get("ref")
                    and r.get("state") in ("prepared", "bound")
                ),
                None,
            )
            if open_ or rows is None:
                raise fail(
                    "intent.open",
                    "An earlier attempt is still open; wait until it ends or expires at "
                    + str((open_ or {}).get("expires_at", "the host"))
                    + ".",
                    exit=5,
                ) from None
            raise fail(
                "intent.blocked",
                "The earlier binding is retained for 24 h, but no request is open. "
                "Start again only if you intend another attempt; an earlier one may have been signed.",
                exit=5,
            ) from None
        if code == "idem.expired":
            raise fail(
                "intent.expired",
                "This attempt is older than the host keeps records (24 h). Start again only if you intend another "
                "attempt; an earlier one may have been signed.",
                exit=5,
            ) from None
        raise


def done_error(prepared: dict, row: dict | None = None) -> HostError:
    """Prior signed content comes only from the host record. Older hosts do not expose signing time."""
    info = prepared.get("request") or {}
    row = row or {}
    op = prepared.get("op") or row.get("op")
    summary = json.dumps((prepared.get("material") or {}).get("review", []), ensure_ascii=False, sort_keys=True)
    # §0.8.1: submitted includes failed appends; human.pending.seq is committed-only.
    if row.get("seq") is None:
        code = (row.get("error") or {}).get("code")
        detail = f" ({code})" if code else ""
        return HostError(
            {
                "code": "intent.failed",
                "message": f"The earlier attempt was submitted but did not land{detail}. "
                "It was not signed into the log. Start again to make a new request.",
                "retryable": False,
                "reason": code,
                "state": "submitted",
            },
            exit=5,
        )
    signed_at = info.get("submitted_at") or row.get("submitted_at")
    return HostError(
        {
            "code": "intent.done",
            "message": "This is already done.",
            "retryable": False,
            "state": "submitted",
            "signed_at": signed_at,
            "op": op,
            "summary": summary,
        },
        exit=5,
    )


def read_done(human, ws: str, rid: str) -> HostError:
    """Use the host's full request and terminal summary, including signing time when that host exposes it."""
    prepared = human(ws, "human.get", {"request_id": rid})
    rows = human(ws, "human.pending", {"request_id": rid}).get("requests", [])
    row = next((r for r in rows if r.get("id") == rid), {})
    if not row:
        raise fail("host.unavailable", "Cannot read the earlier submission outcome.")
    if prepared.get("request", {}).get("state") != "submitted":
        raise fail("internal", "The host did not return the submitted request.")
    return done_error(prepared, row)


#: how many sequence intents (a display aid) are kept per person
INTENTS_PER_PERSON = 64


class Handoffs:
    """What this process remembers. **None of it is what holds "one intent, one request"**: the host's idem record does
    (`intent.py`). This is UI convenience and a quota:

    - `owned`: the request ids this dashboard prepared for a (person, workspace), until they expire. Any session of that
      person may view, poll and cancel them (they are the person's, not the tab's); an id nobody here prepared looks
      like one that never existed. The host's principal check and the signer's own record are the real gates.
    - `active` / `until` / `pending`: the per-session quota of open requests. A request two tabs share is one entry in
      each session's set, so it is never counted twice by a session.
    - `unsettled`: a signature whose outcome is unknown (or still in flight). Only so that nothing here cancels or
      replaces the signer under it; a second signature is refused by the host/signer, not by this record.
    """

    def __init__(self, clock=time.monotonic):
        #: B11: the intent of a sequence's first request, by request id (a display aid: the progress of a run that the
        #: signer drives in the person's terminal). Not a guard of anything. Kept per person, so
        #: another person's burst of prepares cannot evict mine.
        self.intents: dict[str, dict[str, dict]] = {}
        self.clock = clock
        self.lock = threading.Lock()
        self.owned: dict[tuple[str, str], dict[str, float]] = {}  # (person, workspace) -> request id -> deadline
        self.active: dict[str, dict[str, tuple[str, str]]] = {}  # token -> open request id -> (workspace, kind)
        self.until: dict[tuple[str, str], float] = {}  # click requests in `active`: their own deadline
        self.pending: dict[str, int] = {}  # slots reserved by a prepare that has not answered yet
        self.unsettled: dict[str, tuple[str, float]] = {}  # request id -> (workspace, until)
        self.signing: dict[str, int] = {}  # request id -> sign calls in flight
        self.unknown: set[str] = set()  # request ids with a sign outcome nobody knows yet

    # -- ownership: per person and workspace -----------------------------------------------------------
    def note_intent(self, person: str, request_id: str, intent: dict) -> None:
        with self.lock:
            mine = self.intents.setdefault(person, {})
            mine.pop(request_id, None)
            mine[request_id] = intent
            while len(mine) > INTENTS_PER_PERSON:
                mine.pop(next(iter(mine)))

    def intent_of(self, person: str, request_id: str) -> dict | None:
        with self.lock:
            return self.intents.get(person, {}).get(request_id)

    def own(self, person: str, ws: str, request_id: str, ttl: float) -> None:
        with self.lock:
            mine = self.owned.setdefault((person, ws), {})
            mine[request_id] = self.clock() + ttl
            for rid in [r for r, d in mine.items() if d <= self.clock()]:
                del mine[rid]

    def owns(self, person: str, ws: str, request_id: str) -> bool:
        with self.lock:
            return self.owned.get((person, ws), {}).get(request_id, 0) > self.clock()

    def disown(self, request_id: str) -> None:
        with self.lock:
            for mine in self.owned.values():
                mine.pop(request_id, None)

    # -- the quota: per session ------------------------------------------------------------------------
    def prune(self, token: str) -> dict[str, tuple[str, str]]:  # under the lock
        now = self.clock()
        open_ = self.active.setdefault(token, {})
        for rid in [r for r in open_ if self.until.get((token, r), float("inf")) <= now]:
            open_.pop(rid, None)
            self.until.pop((token, rid), None)
        return open_

    def reserve(self, token: str):  # under the lock: check AND take a slot in one step
        if len(self.prune(token)) + self.pending.get(token, 0) >= PER_SESSION:
            raise fail("quota.host", "Too many sign requests are open. Cancel one first.", exit=9)
        self.pending[token] = self.pending.get(token, 0) + 1

    def release(self, token: str):
        with self.lock:
            self.pending[token] = max(0, self.pending.get(token, 0) - 1)

    def commit(self, token: str, ws: str, request_id: str, until: float | None = None, kind: str = "handoff"):
        """The reserved slot becomes the open request `request_id` (one this session holds already adds no slot)."""
        with self.lock:
            self.pending[token] = max(0, self.pending.get(token, 0) - 1)
            self.active.setdefault(token, {})[request_id] = (ws, kind)
            if until is not None:
                self.until[(token, request_id)] = until

    def settle(self, token: str, request_id: str):
        with self.lock:
            self.active.get(token, {}).pop(request_id, None)
            self.until.pop((token, request_id), None)

    def settle_all(self, request_id: str) -> None:
        """The request ended: every session's quota slot for it is freed, not only the caller's."""
        with self.lock:
            for open_ in self.active.values():
                open_.pop(request_id, None)
            for key in [k for k in self.until if k[1] == request_id]:
                del self.until[key]

    def held(self, token: str, kind: str) -> list[tuple[str, str]]:
        """(request id, workspace) of the requests of one kind this session holds open."""
        with self.lock:
            return [(rid, ws) for rid, (ws, k) in self.prune(token).items() if k == kind]

    def shared(self, token: str, request_id: str) -> bool:
        """Does another session of the person hold this request open too? (Then this one's logout leaves it be.)"""
        with self.lock:
            return any(rid == request_id for tok, open_ in self.active.items() if tok != token for rid in open_)

    # -- a signature whose outcome is unknown, or in flight ------------------------------------------
    def is_unsettled(self, request_id: str) -> str | None:
        """The workspace of a request whose signature may be at the host, else None."""
        with self.lock:
            hit = self.unsettled.get(request_id)
            if hit and hit[1] > self.clock():
                return hit[0]
            self.unsettled.pop(request_id, None)
            self.unknown.discard(request_id)
            return None

    def any_unsettled(self) -> bool:
        with self.lock:
            now = self.clock()
            return any(until > now for _, until in self.unsettled.values())

    def settled(self, request_id: str) -> None:
        """The host's answer is known (submitted, or the request ended): clear the mark and any unknown outcome."""
        with self.lock:
            self.unsettled.pop(request_id, None)
            self.unknown.discard(request_id)

    # -- sign calls in flight: one place decides when a failed sign may clear the mark ---------------------
    def begin_sign(self, ws: str, request_id: str, until: float) -> None:
        """Marks the request unsettled before the signer is used."""
        with self.lock:
            hit = self.unsettled.get(request_id)
            if not (hit and hit[1] > self.clock()):
                self.unknown.discard(request_id)  # an expired unknown outcome expired with its mark
            self.unsettled[request_id] = (ws, until)
            self.signing[request_id] = self.signing.get(request_id, 0) + 1

    def end_sign(self, ws: str, request_id: str, until: float, outcome: str) -> None:
        """Ends one sign call: `ok`, `uncertain` or `definite`. Every mark comes from a sign in flight or an unknown
        outcome, so a definite failure clears it only when no other sign of the request is in flight and no outcome
        of it is unknown, whatever order concurrent calls finish in (#444 R-3, F-2). An unknown outcome stays marked
        until the host's answer is known (`settled`) or the mark expires."""
        with self.lock:
            left = self.signing.get(request_id, 1) - 1
            if left > 0:
                self.signing[request_id] = left
            else:
                self.signing.pop(request_id, None)
            if outcome == "uncertain":
                self.unknown.add(request_id)
                self.unsettled[request_id] = (ws, until)
            elif outcome == "definite" and not left and request_id not in self.unknown:
                self.unsettled.pop(request_id, None)


def register(app: FastAPI, gateway: Gateway):
    handoffs = app.state.handoffs = Handoffs()

    def human(
        ws: str, op: str, args: dict, idem: str | None = None, dry_run: bool | None = None, files: dict | None = None
    ):
        if op not in HUMAN_OPS:
            raise AssertionError("the dashboard calls prepare, get, pending and cancel only")  # never human.submit
        link = gateway.enabled(ws)
        return link.call(op, args, write=idem is not None, idem=idem, dry_run=dry_run, files=files)

    app.state.human = human

    def person_of(ws: str) -> str:
        return gateway.link(ws).welcome["peer"]["person"]

    app.state.person_of = person_of

    checks_lock = threading.Lock()
    checks: dict[str, list[float]] = {}  # per bearer token: times of its recent folder checks

    @app.post("/api/w/{ws}/repos/check")
    def check_repo(body: RepoSet, request: Request, ws: str = Depends(workspace)):
        """The host's own look at a repository to add, asked from the form before it closes, so a refusal (home folder,
        not a git folder, inside orch's data, missing, not an owner) stays next to the field. A dry run of the same
        prepare the hand-off makes: nothing is stored, no request exists, nothing is signed. The same check runs again
        when the change is really prepared."""
        gateway.enabled(ws)
        # At most 20 folder checks a minute per session: this route says what exists on this computer.
        now = time.monotonic()
        with checks_lock:  # read, check, append and clean up as one step: concurrent requests cannot overshoot
            recent = [t for t in checks.get(request.state.token, []) if t > now - 60]
            if len(recent) >= 20:
                checks[request.state.token] = recent
                raise fail("rate.limited")
            checks[request.state.token] = [*recent, now]
            for k in [k for k, v in checks.items() if not v or v[-1] <= now - 60]:
                del checks[k]  # tokens with no recent check are dropped: the dict stays small
        # The host decides first (it checks the role, then the folder); the local look is not run before it.
        op, args = host_call(body)
        human(ws, "human.prepare", {"op": op, "args": args, "handoff": True}, dry_run=True)
        return {"ok": True}

    @app.post("/api/w/{ws}/handoff")
    def prepare(body: PrepareBody, request: Request, ws: str = Depends(workspace)):
        token = request.state.token
        gateway.enabled(ws)
        with handoffs.lock:
            handoffs.reserve(token)  # checks and takes a slot atomically: concurrent prepares cannot overshoot
        try:
            if isinstance(
                body, RepoSet
            ):  # the host's path check, before any request exists (plain refusal in the dialog)
                gateway.check_repo_path(ws, body.path)
            if isinstance(
                body, AgentAccess
            ):  # the secret is shown only in the page, sealed: a click request, never here
                raise fail(
                    "invalid.input",
                    "Agent access with a shown secret is signed with one click in the dashboard.",
                    exit=1,
                )
            if isinstance(
                body, HubJoin | HubModeFull | HubLeave
            ):  # the backup key is typed in the page and sealed: a click only
                raise fail(
                    "invalid.input",
                    "A hub step is signed with one click in the dashboard.",
                    exit=1,
                )
            planned = plan(gateway, ws, body)
            if planned.sequence and planned.sequence["intent"].get("images"):
                raise fail(
                    "invalid.input",
                    "Pictures can only be signed with one click in the dashboard. Switch on click to sign "
                    "(Settings) or take the pictures out of the text.",
                    exit=1,
                )
            op = planned.op
            # A runner grant signed in the terminal: `orch sign` draws the secret and offers to hand it to the runner,
            # which needs `runner` so the signer sets label runner (the click path sets it itself).
            args = planned.args | ({"runner": True} if isinstance(body, RunnerGrant) else {})

            def call(idem: str):
                return human(
                    ws,
                    "human.prepare",
                    {
                        "op": op,
                        "args": args,
                        "handoff": True,
                        **({"sequence": planned.sequence} if planned.sequence else {}),
                    },
                    idem=idem,
                    files=planned.files,
                )

            prepared = send_prepare(planned, ws, body.retry, call, lambda: human(ws, "human.pending", {}))
            response = adapters.handoff.request(
                prepared,
                ws,
                expect_op=op,
                expect_key=getattr(body, "key", None),
                expect_events=expected(body, planned.person),
                expect_material=expected_material(body),
            )
        except BaseException:
            handoffs.release(token)
            raise
        if response["state"] == "submitted":
            handoffs.release(token)
            raise read_done(human, ws, response["id"])
        handoffs.own(planned.person, ws, response["id"], TTL)
        if planned.sequence:
            handoffs.note_intent(planned.person, response["id"], planned.sequence["intent"])
        if response["state"] in ("prepared", "bound"):
            handoffs.commit(token, ws, response["id"], until=handoffs.clock() + TTL)
        else:
            handoffs.release(token)
        return response

    def owned_ws(ws: str, request_id: str) -> str:
        if not handoffs.owns(app.state.person_of(ws), ws, request_id):
            raise fail("not_found", exit=2)  # a request nobody here prepared looks like one that never existed
        return ws

    @app.get("/api/w/{ws}/handoff/{request_id}")
    def state(request_id: RequestId, request: Request, ws: str = Depends(workspace)):
        owned_ws(ws, request_id)
        rows = human(ws, "human.pending", {"request_id": request_id})["requests"]
        if len(rows) != 1 or rows[0]["id"] != request_id:
            raise fail("not_found", exit=2)
        if rows[0]["state"] not in ("prepared", "bound"):
            handoffs.settle_all(request_id)  # it no longer counts against any session's open requests
        out = adapters.handoff.state(rows[0])
        intent = handoffs.intent_of(app.state.person_of(ws), request_id)
        if intent is not None and rows[0]["state"] == "submitted" and rows[0].get("seq") is not None:
            out["progress"] = sequence_progress(
                gateway, ws, rows[0].get("key"), intent, person=app.state.person_of(ws), since=rows[0].get("created_at")
            )
            if out["progress"] is not None and intent.get("agent") is not None:
                out["progress"]["last"] = "grant"  # the terminal signs the agent access last (the dialog says so)
        return out

    @app.delete("/api/w/{ws}/handoff/{request_id}")
    def cancel(request_id: RequestId, request: Request, ws: str = Depends(workspace)):
        owned_ws(ws, request_id)
        done = human(ws, "human.cancel", {"request_id": request_id})
        handoffs.settle_all(request_id)
        return {"id": request_id, "state": done["state"]}

    def cancel_session(token: str):
        """Logout: every request this session still holds open is cancelled, each on its own (best effort), except one
        another session of the person holds too."""
        for rid, ws in handoffs.held(token, "handoff"):
            if handoffs.shared(token, rid):
                handoffs.settle(token, rid)
                continue
            try:
                human(ws, "human.cancel", {"request_id": rid})
            except Exception:  # noqa: BLE001 - one failed cancel must not stop the rest, nor the logout
                pass
            handoffs.settle(token, rid)

    app.state.cancel_session = cancel_session
