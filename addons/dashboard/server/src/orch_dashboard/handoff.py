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
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query, Request
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
    action: Literal["new"]
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    type: Literal["feature", "bug", "chore", "spike", "epic"] = "feature"
    priority: Literal["low", "medium", "high", "urgent"] | None = None
    size: Literal["xs", "s", "m", "l", "xl"] | None = None
    labels: list[Label] | None = Field(default=None, min_length=1, max_length=16)
    parent: Key | None = None
    message: Text | None = None


PrepareBody = Annotated[
    Approve | RequestChanges | Verdict | Answer | Comment | AddLabel | Close | Reopen | NewTicket,
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
            args = {"title": action.title, "type": action.type}
            args |= {"priority": action.priority} if action.priority else {}
            args |= {"size": action.size} if action.size else {}
            args |= {"label": action.labels} if action.labels else {}
            args |= {"parent": action.parent} if action.parent else {}
            return "new", args | ({"message": action.message} if action.message else {})
    raise AssertionError("closed allowlist")  # pragma: no cover


class Handoffs:
    """What this process remembers: idem records, the open request per action, and each session's open requests."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.by_idem: OrderedDict[tuple[str, str], tuple[str, dict]] = OrderedDict()
        self.by_action: dict[tuple[str, str, str], dict] = {}
        self.open: dict[str, dict[str, tuple[str, float]]] = {}  # token -> request id -> (workspace, deadline)

    def binding(self, action: Action) -> str:
        body = action.model_dump(exclude={"idem"}, exclude_none=True)
        return hashlib.sha256(adapters.handoff.cj(body)).hexdigest()

    def live(self, token: str) -> dict[str, tuple[str, float]]:
        now = self.clock()
        mine = self.open.setdefault(token, {})
        for rid in [rid for rid, (_, deadline) in mine.items() if deadline <= now]:
            del mine[rid]
        return mine

    def remember(self, token: str, ws: str, idem: str, binding: str, response: dict):
        with self.lock:
            self.by_idem[(ws, idem)] = (binding, response)
            while len(self.by_idem) > REMEMBERED:
                self.by_idem.popitem(last=False)
            self.by_action[(token, ws, binding)] = response
            self.live(token)[response["id"]] = (ws, self.clock() + TTL)

    def forget(self, token: str, request_id: str):
        with self.lock:
            self.live(token).pop(request_id, None)
            for key in [k for k, v in self.by_action.items() if v["id"] == request_id]:
                del self.by_action[key]


def register(app: FastAPI, gateway: Gateway):
    handoffs = app.state.handoffs = Handoffs()

    def human(ws: str, op: str, args: dict, idem: str | None = None, dry_run: bool | None = None):
        if op not in HUMAN_OPS:
            raise AssertionError("the dashboard calls prepare, get, pending and cancel only")  # never human.submit
        link = gateway.enabled(ws)
        return link.call(op, args, write=idem is not None, idem=idem, dry_run=dry_run)

    app.state.human = human

    @app.post("/api/w/{ws}/handoff")
    def prepare(body: PrepareBody, request: Request, ws: str = Depends(workspace)):
        token, binding = request.state.token, handoffs.binding(body)
        known = handoffs.by_idem.get((ws, body.idem))
        if known:
            if known[0] != binding:
                raise fail("idem.conflict", "This key was used for another action.", exit=5)
            return known[1]
        # One open request per (workspace, ticket, action): reopening the panel reuses it, never a second prepare.
        reuse = handoffs.by_action.get((token, ws, binding))
        if reuse:
            try:
                row = human(ws, "human.pending", {"request_id": reuse["id"]})["requests"][0]
                if row["state"] in ("prepared", "bound"):
                    handoffs.remember(token, ws, body.idem, binding, reuse)
                    return reuse
            except HostError:
                pass
            handoffs.forget(token, reuse["id"])
        if len(handoffs.live(token)) >= PER_SESSION:
            raise fail("quota.host", "Too many sign requests are open. Cancel one first.", exit=9)
        labels = None
        if isinstance(body, AddLabel):
            labels = human_labels(ws, body.key)
        op, args = host_call(body, labels)
        prepared = human(ws, "human.prepare", {"op": op, "args": args, "handoff": True}, idem=body.idem)
        response = adapters.handoff.request(prepared, ws)
        handoffs.remember(token, ws, body.idem, binding, response)
        return response

    def human_labels(ws: str, key: str) -> list[str]:
        return list(gateway.call(ws, "ticket.get", {"ref": key})["doc"].get("labels", []))

    @app.get("/api/w/{ws}/handoff/{request_id}")
    def state(
        request_id: RequestId,
        request: Request,
        full: Annotated[bool, Query()] = False,
        ws: str = Depends(workspace),
    ):
        rows = human(ws, "human.pending", {"request_id": request_id})["requests"]
        if len(rows) != 1 or rows[0]["id"] != request_id:
            raise fail("not_found", exit=2)
        out = adapters.handoff.state(rows[0])
        if rows[0]["state"] not in ("prepared", "bound"):
            handoffs.forget(request.state.token, request_id)  # it no longer counts against the session's open requests
        if full and rows[0]["state"] in ("prepared", "bound"):
            out["request"] = adapters.handoff.request(human(ws, "human.get", {"request_id": request_id}), ws)
        return out

    @app.delete("/api/w/{ws}/handoff/{request_id}")
    def cancel(request_id: RequestId, request: Request, ws: str = Depends(workspace)):
        done = human(ws, "human.cancel", {"request_id": request_id})
        handoffs.forget(request.state.token, request_id)
        return {"id": request_id, "state": done["state"]}

    def cancel_session(token: str):
        """Logout and session expiry: every request this session still holds is cancelled (best effort)."""
        for rid, (ws, _) in list(handoffs.live(token).items()):
            try:
                human(ws, "human.cancel", {"request_id": rid})
            except HostError:
                pass
            handoffs.forget(token, rid)

    app.state.cancel_session = cancel_session
