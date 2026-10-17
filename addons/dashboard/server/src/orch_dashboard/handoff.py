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
import threading
import time
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

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
    size: Literal["xs", "s", "m", "l", "xl"] | None = None
    labels: list[Label] | None = Field(default=None, min_length=0, max_length=16)
    parent: Key | None = None

    @model_validator(mode="after")
    def something(self):
        if self.priority is None and self.size is None and self.labels is None and self.parent is None:
            raise ValueError("details change at least one field")
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
    | NewTicket
    | Details
    | WriteSection
    | SettingsSet
    | PolicySet
    | MemberAdd
    | MemberRole
    | MemberRemove
    | GrantRevoke
    | RunnerGrant,
    Field(discriminator="action"),
]


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
        case Move():
            return "move", {"ref": action.key, "to": action.to} | ({"reason": action.reason} if action.reason else {})
        case NewTicket():
            return "new", {"title": action.title, "type": action.type}
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
        case PolicySet():
            return "policy.set", {"gates": {action.gate: action.policy.canonical()}}
        case MemberAdd():
            # `cert` names the file (`-`); its bytes travel in `files` (host_files). `pk` is a plain argument.
            return "member.add", {"name": action.name, "role": action.role, "pk": action.pk, "cert": "-"}
        case MemberRole():
            return "member.role", {"person": action.person, "role": action.role}
        case MemberRemove():
            return "member.remove", {"person": action.person}
        case GrantRevoke():
            return "grant.revoke", {"grant": action.grant} | ({"reason": action.reason} if action.reason else {})
        case RunnerGrant():
            return "grant", {"hours": action.hours, "scope": action.scope} | (
                {"extend": action.extend} if action.extend else {}
            )
    raise AssertionError("closed allowlist")  # pragma: no cover


DURATIONS = ("grant_hours", "claim_ttl_min", "lease_ttl_min")
WORKSPACE_OPS = ("settings.set", "policy.set", "member.add", "member.role", "member.remove", "grant.revoke", "move")


def host_files(action: Action) -> dict | None:
    """The inner op's files for `human.prepare` (R9): only a member's certificate, inline as `b64`."""
    if isinstance(action, MemberAdd):
        return {"cert": {"b64": base64.b64encode(action.cert.encode()).decode()}}
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
        case SettingsSet():
            _, args = host_call(action)
            return lambda ev: one(ev, "settings.changed", set=args["set"])
        case PolicySet():
            _, args = host_call(action)
            return lambda ev: one(ev, "policy.changed", gates=args["gates"]) and "ref" not in ev[0]
        case Move():
            return lambda ev: one(ev, "ticket.moved", to=action.to) and ev[0].get("reason") == action.reason
        case MemberRole():
            return lambda ev: one(ev, "role.changed", person=action.person, role=action.role)
        case MemberRemove():
            return lambda ev: one(ev, "member.removed", person=action.person)
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

    def idem(self, ws: str, retry: int = 0) -> str:
        return intent_idem(self.person, ws, self.op, self.args, self.base, self.files, retry)


GATE_OF = {Approve: lambda a: a.gate, RequestChanges: lambda a: a.gate, Verdict: lambda a: "verify"}


def plan(gateway: Gateway, ws: str, body: Action) -> Plan:
    """Canonical host content, plus a gate or question version only; other actions have no base."""
    link = gateway.link(ws)
    key = getattr(body, "key", None)
    data = gateway.call(ws, "ticket.get", {"ref": key}) if key is not None else None
    labels = list(data["doc"].get("labels", [])) if isinstance(body, AddLabel) and data else None
    op, args = host_call(body, labels)  # the only place an op name is chosen
    if op in WORKSPACE_OPS and link.welcome.get("ops", {}).get(op) != 1:
        # policy.set and settings.set wait for F1 events on a real host (R5): never offered, never guessed.
        raise fail("op.unsupported", f"This host does not serve {op} yet.", exit=1)
    base = None
    if data is not None:
        gate = GATE_OF.get(type(body))
        if gate:
            base = gate_base(data, gate(body))
        elif isinstance(body, Answer):
            base = question_base(data, body.question)
    return Plan(op, args, host_files(body), base, link.welcome["peer"]["person"])


CLICK_ONLY = (
    "This workspace signs in the dashboard. Use Sign here, or turn convenience off from the terminal with "
    "`orch host convenience off --workspace {ws}`."
)


def send_prepare(plan_: Plan, ws: str, retry: int, call, pending=None):
    """Prepare once with the intent's idem. Retention expiry ends this attempt (`intent.expired`); only the
    person's explicit "Start again" advances `retry` and permits a fresh request.

    `idem.conflict` means the host holds an attempt bound to another path or signer. Report it as
    `intent.open` while a matching op+key is open, otherwise `intent.blocked`. Neither prepares another request."""
    try:
        return call(plan_.idem(ws, retry))
    except HostError as exc:
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

    @app.post("/api/w/{ws}/handoff")
    def prepare(body: PrepareBody, request: Request, ws: str = Depends(workspace)):
        token = request.state.token
        gateway.enabled(ws)
        if gateway.one_path_is_click(ws):
            raise HostError(
                {"code": "sign.click_only", "message": CLICK_ONLY.format(ws=ws), "retryable": False}, exit=3
            )
        with handoffs.lock:
            handoffs.reserve(token)  # checks and takes a slot atomically: concurrent prepares cannot overshoot
        try:
            planned = plan(gateway, ws, body)
            op = planned.op
            # A runner grant signed in the terminal: `orch sign` draws the secret and offers to hand it to the runner,
            # which needs `runner` so the signer sets label runner (the click path sets it itself).
            args = planned.args | ({"runner": True} if isinstance(body, RunnerGrant) else {})

            def call(idem: str):
                return human(
                    ws,
                    "human.prepare",
                    {"op": op, "args": args, "handoff": True},
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
            )
        except BaseException:
            handoffs.release(token)
            raise
        if response["state"] == "submitted":
            handoffs.release(token)
            raise read_done(human, ws, response["id"])
        handoffs.own(planned.person, ws, response["id"], TTL)
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
        return adapters.handoff.state(rows[0])

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
