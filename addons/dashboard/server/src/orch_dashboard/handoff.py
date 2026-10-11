"""The signed actions of #305 (M3): prepare, look at, and cancel a hand-off. A closed allowlist of typed actions.

**This server never signs and never holds a factor, and it never calls `human.submit`.** The person signs in a
trusted place (`orch sign CODE` in a terminal, later the Touch ID helper), which fetches the request itself and submits
it. The dashboard only asks the host to prepare the request, shows the host's `material`, and follows the outcome.

Every route has a typed model that forbids extras; the host op and its arguments are fixed per action. The browser
never names an op, a path or a raw event. `opts.idem` comes from a client-generated key (one per click, reused on
retry); a retry with the same key answers from this process's record instead of preparing a second request.
"""

import hashlib
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from . import adapters
from .errors import HostError, fail
from .gateway import Gateway
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

    @model_validator(mode="after")
    def something(self):
        if self.priority is None and self.size is None and self.labels is None:
            raise ValueError("details change at least one field")
        return self


PrepareBody = Annotated[
    Approve | RequestChanges | Verdict | Answer | Comment | AddLabel | Close | Reopen | NewTicket | Details,
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
            return "set", {"ref": action.key, "pairs": pairs}
    raise AssertionError("closed allowlist")  # pragma: no cover


class Handoffs:
    """What this process remembers, **per session token**: the requests it prepared (kept to their expiry so a late poll
    still works), the ones still open (the quota), and the idem records of its own clicks."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.owned: dict[str, dict[str, tuple[str, float]]] = {}  # token -> request id -> (workspace, deadline)
        self.active: dict[str, set[str]] = {}
        self.by_idem: OrderedDict[tuple[str, str, str], tuple[str, dict | None]] = OrderedDict()  # None: in flight
        self.unknown: dict[tuple[str, str, str], float] = {}  # (token, ws, binding) -> when the lost call started

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

    def human(ws: str, op: str, args: dict, idem: str | None = None, dry_run: bool | None = None):
        if op not in HUMAN_OPS:
            raise AssertionError("the dashboard calls prepare, get, pending and cancel only")  # never human.submit
        link = gateway.enabled(ws)
        return link.call(op, args, write=idem is not None, idem=idem, dry_run=dry_run)

    app.state.human = human

    def cancel_orphans(ws: str, op: str, key: str | None, since: float):
        """A `human.prepare` whose answer was lost may have left a request open. Cancel the person's open requests that
        match the action and were created after the lost call began (the host keeps no idem record for prepare)."""
        for row in human(ws, "human.pending", {})["requests"]:
            if row["state"] not in ("prepared", "bound") or row["op"] != op or (key and row.get("key") != key):
                continue
            created = _epoch(row["created_at"])
            if created is not None and created >= since - 2:
                human(ws, "human.cancel", {"request_id": row["id"]})

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
            lost = handoffs.unknown.pop((token, ws, binding), None)
        started = time.time()
        try:
            labels = (
                list(gateway.call(ws, "ticket.get", {"ref": body.key})["doc"].get("labels", []))
                if isinstance(body, AddLabel)
                else None
            )
            op, args = host_call(body, labels)
            if lost is not None:
                cancel_orphans(ws, op, getattr(body, "key", None), lost)
            prepared = human(ws, "human.prepare", {"op": op, "args": args, "handoff": True}, idem=body.idem)
            response = adapters.handoff.request(prepared, ws, expect_op=op, expect_key=getattr(body, "key", None))
        except BaseException as exc:
            with handoffs.lock:
                handoffs.by_idem.pop(slot, None)
                if isinstance(exc, HostError) and exc.error["code"] in ("outcome.unknown", "cancelled"):
                    handoffs.unknown[(token, ws, binding)] = started  # the next prepare cancels any orphan first
            raise
        with handoffs.lock:
            handoffs.by_idem[slot] = (binding, response)
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
