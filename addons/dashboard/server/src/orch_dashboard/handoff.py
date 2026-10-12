"""The signed actions of #305 (M3): prepare, look at, and cancel a hand-off. A closed allowlist of typed actions.

**This server never signs and never holds a factor, and it never calls `human.submit`.** The person signs in a
trusted place (`orch sign CODE` in a terminal, later the Touch ID helper), which fetches the request itself and submits
it. The dashboard only asks the host to prepare the request, shows the host's `material`, and follows the outcome.

Every route has a typed model that forbids extras; the host op and its arguments are fixed per action. The browser
never names an op, a path or a raw event. `opts.idem` comes from a client-generated key (one per click, reused on
retry); a retry with the same key answers from this process's record instead of preparing a second request.
"""

import base64
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from . import adapters
from .errors import HostError, fail
from .gateway import Gateway
from .hostlink import _pairs, _valid
from .reads import Key, workspace

Idem = Annotated[str, StringConstraints(pattern=r"^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
RequestId = Annotated[str, StringConstraints(pattern=adapters.handoff.REQUEST_ID.pattern)]
Text = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
Word = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
Label = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,31}$")]
Question = Annotated[str, StringConstraints(pattern=r"^Q[1-9][0-9]{0,5}$")]
Gate = Literal["requirements", "plan", "code"]

HUMAN_OPS = ("human.prepare", "human.get", "human.pending", "human.cancel")  # never human.submit
PER_SESSION = 8  # design section 3: pending sign requests per session
TTL = 600
REMEMBERED = 256
LOST_KEPT = 900  # seconds a lost-call marker or an attempt record is kept (a request lives 600 s)
SLACK = 2  # seconds either side of a window (host timestamps have whole-second resolution)
UNKNOWN_CAP = 256


@dataclass
class Lost:
    """A prepare whose answer was lost: the window in which the host may have made a request of ours."""

    start: float
    end: float
    ids: set[str] = field(default_factory=set)  # the attempts that may have made it


@dataclass
class Attempt:
    id: str
    ws: str
    op: str
    key: str | None
    start: float
    end: float | None = None  # None while it runs


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    idem: Idem


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
    labels: list[Label] | None = Field(default=None, min_length=1, max_length=16)
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
OneLine = r"^[^\x00-\x1f\x7f\u2028\u2029]+$"
Token = Literal["owner", "maintainer", "member", "ticket_owner", "assignees", "reviewers", "watchers"]
PolicyGate = Literal["requirements", "plan", "verify", "code"]
TicketType = Literal["feature", "bug", "chore", "spike", "epic"]
MAX_PK = 87  # base64url of a 65-byte public key, no padding
MAX_CERT_BYTES = 16384  # a device certificate is a few hundred bytes; anything near this is not one
PK_BYTES = 65


class SettingsSet(Action):
    """`settings.set {set:{grant_hours?, claim_ttl_min?, lease_ttl_min?}}`; `repos` is refused (the Repos addon)."""

    action: Literal["settings_set"]
    grant_hours: Annotated[int, Field(ge=1, le=24)] | None = None
    claim_ttl_min: Annotated[int, Field(ge=15, le=1440)] | None = None
    lease_ttl_min: Annotated[int, Field(ge=5, le=1440)] | None = None

    @model_validator(mode="after")
    def something(self):
        if self.grant_hours is None and self.claim_ttl_min is None and self.lease_ttl_min is None:
            raise ValueError("change at least one duration")
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
        """Stored in canonical form (F1 5.7): lists sorted and de-duplicated; a non-canonical policy is refused."""
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
    name: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=OneLine)]
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
    reason: Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=OneLine)] | None = None


PrepareBody = Annotated[
    Approve
    | RequestChanges
    | Verdict
    | Answer
    | Comment
    | AddLabel
    | Close
    | Reopen
    | NewTicket
    | Details
    | WriteSection
    | SettingsSet
    | PolicySet
    | MemberAdd
    | MemberRole
    | MemberRemove
    | GrantRevoke,
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
        case NewTicket():
            return "new", {"title": action.title, "type": action.type}
        case Details():
            pairs = [f"{k}={v}" for k, v in (("priority", action.priority), ("size", action.size)) if v]
            if action.labels:
                pairs.append("labels=" + ",".join(sorted(set(action.labels))))
            if action.parent:
                pairs.append(f"parent={action.parent}")
            return "set", {"ref": action.key, "pairs": pairs}
        case WriteSection():
            return "section.set", {"ref": action.key, "section": action.section, "message": action.text}
        case SettingsSet():
            return "settings.set", {"set": action.model_dump(include=set(DURATIONS), exclude_none=True)}
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
    raise AssertionError("closed allowlist")  # pragma: no cover


DURATIONS = ("grant_hours", "claim_ttl_min", "lease_ttl_min")
WORKSPACE_OPS = ("settings.set", "policy.set", "member.add", "member.role", "member.remove", "grant.revoke")


def host_files(action: Action) -> dict | None:
    """The inner op's files for `human.prepare` (R9): only a member's certificate, inline as `b64`."""
    if isinstance(action, MemberAdd):
        return {"cert": {"b64": base64.b64encode(action.cert.encode()).decode()}}
    return None


def expected(action: Action):
    """What the host's prepared events must be, for the actions that are not about a ticket. A host that prepares
    anything else (another type, another person, other values) is refused before the person sees it."""

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
        case MemberRole():
            return lambda ev: one(ev, "role.changed", person=action.person, role=action.role)
        case MemberRemove():
            return lambda ev: one(ev, "member.removed", person=action.person)
        case GrantRevoke():
            return lambda ev: one(ev, "grant.revoked", grant=action.grant) and ev[0].get("reason") == action.reason
        case MemberAdd():
            cert = _strict_json_object(action.cert)
            return lambda ev: (
                one(ev, "member.added", name=action.name, role=action.role, pk_pub=action.pk, device_cert=cert)
                and isinstance(ev[0].get("person"), str)
                and ev[0]["person"].startswith("p_")
            )
    return None


class Handoffs:
    """What this process remembers, **per session token**: the requests it prepared (kept to their expiry so a late poll
    still works), the ones still open (the quota), and the idem records of its own clicks."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.owned: dict[str, dict[str, tuple[str, float]]] = {}  # token -> request id -> (workspace, deadline)
        self.active: dict[str, set[str]] = {}
        self.by_idem: OrderedDict[tuple[str, str, str], tuple[str, dict | None]] = OrderedDict()  # None: in flight
        self.unknown: dict[tuple[str, str, str], Lost] = {}  # (token, ws, binding) -> the lost call's window
        self.attempts: list[Attempt] = []  # every prepare of this process, any session: who else could own an orphan
        self.ever: OrderedDict[str, float] = OrderedDict()  # request ids this process has ever been handed
        self.serial = 0

    def begin(self, ws: str, op: str, key: str | None) -> Attempt:  # under the lock
        self.serial += 1
        mine = Attempt(f"p{self.serial}", ws, op, key, time.time())
        self.attempts.append(mine)
        self.forget()
        return mine

    def forget(self):  # under the lock: bounded, entries expire
        now = time.time()
        # Attempts still in flight are never dropped (crowded() must see them); finished ones are bounded.
        done = [a for a in self.attempts if a.end is not None and a.end > now - LOST_KEPT][-512:]
        self.attempts[:] = [a for a in self.attempts if a.end is None or a in done]
        for slot in [k for k, v in self.unknown.items() if v.end < now - LOST_KEPT]:
            del self.unknown[slot]
        while len(self.unknown) > UNKNOWN_CAP:
            del self.unknown[next(iter(self.unknown))]
        while len(self.ever) > 1024:
            self.ever.popitem(last=False)

    def all_known(self) -> set[str]:
        with self.lock:
            return {rid for mine in self.owned.values() for rid in mine} | set(self.ever)

    def crowded(self, lost: Lost, op: str, ws: str, key: str | None, me: str) -> bool:
        """Was another prepare for the same op and ticket running in the lost call's window (any session or tab)?"""
        with self.lock:
            return any(
                a.id != me
                and a.id not in lost.ids
                and (a.ws, a.op, a.key) == (ws, op, key)
                # The host stamps created_at in whole seconds (rounded down), so a request it lists as created by
                # lost.end + SLACK may come from an attempt that started up to a second later.
                and a.start < lost.end + SLACK + 1
                and (a.end is None or a.end >= lost.start - SLACK)
                for a in self.attempts
            )

    def binding(self, action: Action) -> str:
        body = action.model_dump(exclude={"idem"}, exclude_none=True)
        return hashlib.sha256(adapters.handoff.cj(body)).hexdigest()

    def prune(self, token: str):  # under the lock
        now = self.clock()
        mine = self.owned.setdefault(token, {})
        for rid in [rid for rid, (_, deadline) in mine.items() if deadline <= now]:
            del mine[rid]
            self.active.get(token, set()).discard(rid)
        return mine

    def known_ids(self, token: str) -> set[str]:
        with self.lock:
            return set(self.prune(token))

    def owner_of(self, token: str, request_id: str) -> str | None:
        """The workspace a request was prepared in, if this session prepared it."""
        with self.lock:
            hit = self.prune(token).get(request_id)
            return hit[0] if hit else None

    def settle(self, token: str, request_id: str):
        with self.lock:
            self.active.get(token, set()).discard(request_id)


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

    def ours(origin) -> bool:
        """The host states who made the request (kernel uid and pid, and the self-declared client): only ours counts."""
        client = origin.get("client") if isinstance(origin, dict) else None
        return (
            isinstance(origin, dict)
            and origin.get("pid") == os.getpid()
            and origin.get("uid") == os.getuid()
            and isinstance(client, str)
            and (client == "orch-dashboard" or client.startswith("orch-dashboard/"))
        )

    def recover(ws: str, op: str, key: str | None, lost: Lost, me: str) -> list[str]:
        """`human.prepare` ignores `opts.idem` (host 0.8.1): every call makes a request. After a lost answer this
        only CANCELS orphans that are provably ours; it never adopts one. Provably ours: the same op and ticket,
        created in the lost call's own window, unknown to every session of this process, and (the host states the
        origin in `human.get`) made by this process as `orch-dashboard`. If another prepare for the same op and
        ticket ran in the window, or the origin is missing or foreign, nothing is cancelled. A `HostError`
        propagates and the caller keeps the marker. Returns the `expires_at` of the matching requests left open."""
        rows = human(ws, "human.pending", {})["requests"]
        known = handoffs.all_known()
        crowded = handoffs.crowded(lost, op, ws, key, me)
        left: list[str] = []
        for row in rows:
            if not (
                row["state"] == "prepared"
                and row["id"] not in known
                and row["op"] == op
                and (key is None or row.get("key") == key)
                and lost.start - SLACK <= (_epoch(row["created_at"]) or 0) <= lost.end + SLACK
            ):
                continue
            try:
                if crowded or not ours(human(ws, "human.get", {"request_id": row["id"]}).get("origin")):
                    left.append(row["expires_at"])
                    continue
                human(ws, "human.cancel", {"request_id": row["id"]})
            except HostError as exc:
                if exc.error["code"] not in ("not_found", "human.request_expired"):
                    raise
        return left

    @app.post("/api/w/{ws}/handoff")
    def prepare(body: PrepareBody, request: Request, ws: str = Depends(workspace)):
        token, binding = request.state.token, handoffs.binding(body)
        slot = (token, ws, body.idem)
        with handoffs.lock:
            known = handoffs.by_idem.get(slot)
            if known:
                if known[0] != binding:
                    raise fail("idem.conflict", "This key was used for another action.", exit=5)
                if known[1] is None:
                    raise fail("lock.busy", "This request is still being prepared.", exit=9)
                return known[1]
            handoffs.prune(token)
            if len(handoffs.active.setdefault(token, set())) >= PER_SESSION:
                raise fail("quota.host", "Too many sign requests are open. Cancel one first.", exit=9)
            handoffs.by_idem[slot] = (binding, None)  # reserved: a concurrent twin is refused, not prepared again
            while len(handoffs.by_idem) > REMEMBERED:
                handoffs.by_idem.popitem(last=False)
            lost = handoffs.unknown.get((token, ws, binding))  # kept until a recovery has completed
            op = host_call(body, [])[0]
            me = handoffs.begin(ws, op, getattr(body, "key", None))
        called = answered = False
        try:
            labels = (
                list(gateway.call(ws, "ticket.get", {"ref": body.key})["doc"].get("labels", []))
                if isinstance(body, AddLabel)
                else None
            )
            op, args = host_call(body, labels)
            if op in WORKSPACE_OPS and gateway.link(ws).welcome.get("ops", {}).get(op) != 1:
                # policy.set and settings.set wait for F1 events on a real host (R5): never offered, never guessed.
                raise fail("op.unsupported", f"This host does not serve {op} yet.", exit=1)
            left = recover(ws, op, me.key, lost, me.id) if lost is not None else None
            called = True
            prepared = human(
                ws, "human.prepare", {"op": op, "args": args, "handoff": True}, idem=body.idem, files=host_files(body)
            )
            answered = True  # a request may exist from here on, whatever happens to the answer
            response = adapters.handoff.request(
                prepared, ws, expect_op=op, expect_key=me.key, expect_events=expected(body)
            )
            if lost is not None:
                response["earlier_open"] = {"expires_at": max(left) if left else None}
        except BaseException as exc:
            with handoffs.lock:
                handoffs.by_idem.pop(slot, None)
                # The marker stays until a retry has completed. A second lost answer widens the window (earliest start),
                # it never replaces it; any other failure leaves the marker exactly as it was.
                if called and (
                    answered or (isinstance(exc, HostError) and exc.error["code"] in ("outcome.unknown", "cancelled"))
                ):
                    now = time.time()
                    handoffs.unknown[(token, ws, binding)] = (
                        Lost(min(lost.start, me.start), now, lost.ids | {me.id})
                        if lost
                        else Lost(me.start, now, {me.id})
                    )
            raise
        finally:
            with handoffs.lock:
                me.end = time.time()
                if not called and me in handoffs.attempts:
                    handoffs.attempts.remove(me)  # it never reached the host's prepare: it made no request
        with handoffs.lock:
            handoffs.unknown.pop((token, ws, binding), None)
            handoffs.by_idem[slot] = (binding, response)
            handoffs.ever[response["id"]] = time.time()
            handoffs.prune(token)[response["id"]] = (ws, handoffs.clock() + TTL)
            handoffs.active.setdefault(token, set()).add(response["id"])
        return response

    def owned_ws(request: Request, request_id: str, ws: str) -> str:
        if handoffs.owner_of(request.state.token, request_id) != ws:
            raise fail("not_found", exit=2)  # another session's request, or one that never existed, looks the same
        return ws

    @app.get("/api/w/{ws}/handoff/{request_id}")
    def state(request_id: RequestId, request: Request, ws: str = Depends(workspace)):
        owned_ws(request, request_id, ws)
        rows = human(ws, "human.pending", {"request_id": request_id})["requests"]
        if len(rows) != 1 or rows[0]["id"] != request_id:
            raise fail("not_found", exit=2)
        if rows[0]["state"] not in ("prepared", "bound"):
            handoffs.settle(request.state.token, request_id)  # it no longer counts against the session's open requests
        return adapters.handoff.state(rows[0])

    @app.delete("/api/w/{ws}/handoff/{request_id}")
    def cancel(request_id: RequestId, request: Request, ws: str = Depends(workspace)):
        owned_ws(request, request_id, ws)
        done = human(ws, "human.cancel", {"request_id": request_id})
        handoffs.settle(request.state.token, request_id)
        return {"id": request_id, "state": done["state"]}

    def cancel_session(token: str):
        """Logout: every request this session still holds open is cancelled, each on its own (best effort)."""
        with handoffs.lock:
            mine = [
                (rid, ws) for rid, (ws, _) in handoffs.prune(token).items() if rid in handoffs.active.get(token, set())
            ]
        for rid, ws in mine:
            try:
                human(ws, "human.cancel", {"request_id": rid})
            except Exception:  # noqa: BLE001 - one failed cancel must not stop the rest, nor the logout
                pass
            handoffs.settle(token, rid)

    app.state.cancel_session = cancel_session


def _epoch(value: str) -> float | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except (TypeError, ValueError):
        return None
