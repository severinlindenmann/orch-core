"""The click-to-sign routes (H5c, orch-v2-host section 0.8.6 item 7): prepare, view, sign, cancel and the status card.

A closed set of typed routes, like the hand-off. The browser names a typed action (the same bodies as the hand-off);
the op and its arguments are chosen here, in `host_call`, never by the browser. The View comes from the signer and is
passed on unchanged: **this server never builds a preview for a click request, never logs a View, a word or a
credential, and never signs.** It also never calls the signer from an addon route: only these four handlers do.

A request is the browser session's: view, sign and cancel of an id this session did not prepare look like a request
that never existed (the signer separately refuses anything it did not prepare itself).
"""

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

from . import adapters
from .click_signer import lost_sign
from .errors import HostError, fail
from .gateway import Gateway
from .handoff import (
    WORKSPACE_OPS,
    AddLabel,
    PrepareBody,
    RequestId,
    expected,
    host_call,
    host_files,
    host_idem,
)
from .reads import workspace

# The card text, verbatim (owner decision, 12 Oct 2026). A View that carries anything else is not shown.
RISK = "Convenience signing: agents running as you, or anything that takes over this dashboard, could sign."
ViewDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Word = Annotated[str, StringConstraints(min_length=1, max_length=128)]
TTL = 660  # a request lives 10 minutes at most (the signer caps it); the session forgets it a minute later
REMEMBERED = 64
RECENT = 300  # seconds a signed action is remembered, so a lost browser answer can read "Signed"
#: refusals after which a request can no longer be signed: its slot is free again
ENDED = {"expired", "not_prepared_here", "custody_changed", "human.request_expired", "human.request_state"}


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: RequestId


class Sign(Step):
    view_digest: ViewDigest
    word: Word | None = None


class Clicks:
    """The click requests each browser session prepared (id -> workspace, deadline); nothing about their content."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.Lock()
        self.owned: dict[str, dict[str, tuple[str, float]]] = {}
        self.ops: dict[
            tuple[str, str], tuple[str, object, int]
        ] = {}  # -> (op chosen at prepare, typed body, signer epoch)
        self.by_idem: OrderedDict[tuple[str, str, str], str] = OrderedDict()  # (token, ws, idem) -> binding

    def prune(self, token: str) -> dict[str, tuple[str, float]]:  # under the lock
        mine = self.owned.setdefault(token, {})
        now = self.clock()
        for rid in [rid for rid, (_, deadline) in mine.items() if deadline <= now]:
            del mine[rid]
            self.ops.pop((token, rid), None)
        return mine

    def add(self, token: str, ws: str, request_id: str, op: str = "", body: object = None, epoch: int = 0) -> None:
        with self.lock:
            mine = self.prune(token)
            mine[request_id] = (ws, self.clock() + TTL)
            self.ops[(token, request_id)] = (op, body, epoch)
            while len(mine) > REMEMBERED:
                self.ops.pop((token, next(iter(mine))), None)
                del mine[next(iter(mine))]

    def has(self, token: str, ws: str, request_id: str) -> bool:
        with self.lock:
            hit = self.owned.get(token, {}).get(request_id)
            return bool(hit and hit[0] == ws and hit[1] > self.clock())

    def op_of(self, token: str, request_id: str) -> str:
        with self.lock:
            return self.ops.get((token, request_id), ("", None, 0))[0]

    def body_of(self, token: str, request_id: str):
        with self.lock:
            return self.ops.get((token, request_id), ("", None, 0))[1]

    def stale(self, epoch: int) -> list[tuple[str, str, str]]:
        """(token, workspace, request id) of every open click request a signer of an earlier life prepared."""
        with self.lock:
            return [
                (tok, self.owned[tok][rid][0], rid)
                for (tok, rid), (_, _, e) in self.ops.items()
                if e != epoch and rid in self.owned.get(tok, {})
            ]

    def forget_all(self, request_id: str) -> None:
        """The request ended: every session lets go of it."""
        with self.lock:
            for tok, mine in self.owned.items():
                mine.pop(request_id, None)
                self.ops.pop((tok, request_id), None)

    def forget(self, token: str, request_id: str) -> None:
        with self.lock:
            self.owned.get(token, {}).pop(request_id, None)
            self.ops.pop((token, request_id), None)

    def open(self, token: str) -> list[tuple[str, str]]:
        with self.lock:
            return [(rid, ws) for rid, (ws, _) in self.owned.get(token, {}).items()]


def verify(prepared: dict, ws: str, **expect) -> dict:
    """The hand-off's checks on the host's own record (`adapters.handoff.request`): op, ticket key, exact events."""
    return adapters.handoff.request(prepared, ws, **expect)


def refusal_check(message: str):
    from .errors import HostError

    return HostError(
        {"code": "human.click_refused", "message": message, "retryable": False, "reason": "check_failed"}, 3
    )


def register(app: FastAPI, gateway: Gateway):
    clicks = app.state.clicks = Clicks()
    handoffs = app.state.handoffs  # the quota of open requests is one per session, shared with the hand-off
    signer = gateway.click
    signer.unsettled = handoffs.any_unsettled  # a hung signer is not replaced while a signature may be mid-submit
    STATES = ("prepared", "bound", "submitted", "cancelled", "expired")

    def binding_of(body) -> str:
        return hashlib.sha256(adapters.handoff.cj(body.model_dump(exclude={"idem"}, exclude_none=True))).hexdigest()

    def human(ws: str, op: str, args: dict):
        return app.state.human(ws, op, args)

    def sweep() -> None:
        """After a signer restart its approvals are gone: cancel the orphaned click requests at the host through OUR
        link and give their quota slots back, so they cannot hold the session's limit for 11 minutes."""
        for token, ws, rid in clicks.stale(signer.restarts):
            if handoffs.is_unsettled(rid):
                continue  # its signature may be at the host: it settles by itself, nothing is cancelled or forgotten
            try:
                human(ws, "human.cancel", {"request_id": rid})
            except Exception:  # noqa: BLE001 - already settled, or the host is away: the host expires it anyway
                pass
            clicks.forget(token, rid)
            handoffs.settle(token, rid)

    def mine(request: Request, ws: str, request_id: str) -> None:
        gateway.enabled(ws)  # the workspace is pinned and the dashboard addon is enabled in it, like the hand-off
        sweep()
        if not clicks.has(request.state.token, ws, request_id):
            raise fail("not_found", exit=2)  # another session's request and one that never existed look the same

    @app.get("/api/w/{ws}/click/status")
    def status(ws: str = Depends(workspace)):
        gateway.enabled(ws)
        return adapters.click_card(signer.status().get(ws)) or {
            "custody": "none",
            "enabled": False,
            "available": False,
            "problem": "no_signer",
        }

    @app.post("/api/w/{ws}/click/prepare")
    def prepare(body: PrepareBody, request: Request, ws: str = Depends(workspace)):
        token = request.state.token
        gateway.enabled(ws)  # pinned workspace, dashboard addon enabled in it (the hand-off's `human()` does this)
        # The same click retried (one key) must be the same action: another body under a used key is a conflict.
        binding = binding_of(body)
        handoffs.refuse_if_unsettled(ws, binding)  # an unknown outcome is never answered with a second request
        slot = (token, ws, body.idem)
        with clicks.lock:
            known = clicks.by_idem.get(slot)
        if known is not None and known != binding:
            raise fail("idem.conflict", "This key was used for another action.", exit=5)
        reserved = known is None
        if reserved:
            with handoffs.lock:
                handoffs.reserve(token)  # check and take a slot in one step (shared with the hand-off)
        try:
            return _prepare(body, token, ws, slot, binding, reserved)
        except BaseException:
            if reserved:
                handoffs.release(token)
            raise

    def _prepare(body, token, ws, slot, binding, reserved):
        labels = (
            list(gateway.call(ws, "ticket.get", {"ref": body.key})["doc"].get("labels", []))
            if isinstance(body, AddLabel)
            else None
        )
        op, args = host_call(body, labels)  # the only place an op name is chosen
        if op in WORKSPACE_OPS and gateway.link(ws).welcome.get("ops", {}).get(op) != 1:
            raise fail("op.unsupported", f"This host does not serve {op} yet.", exit=1)
        sent = {
            "workspace_id": ws,
            "op": op,
            "args": args,
            # One key per click, reused on retry; bound to this session and workspace like the hand-off's.
            "idem": host_idem(token, ws, body.idem, "click-idem"),
        }
        files = host_files(body)
        if files:
            sent["files"] = files
        got = signer.call("prepare", sent)
        sweep()  # this call may have started a new signer: the old one's orphans are cancelled now
        clicks.add(token, ws, got["request_id"], op, body, signer.restarts)
        if reserved:
            handoffs.commit(token, got["request_id"], until=handoffs.clock() + TTL)
        with clicks.lock:
            clicks.by_idem[slot] = binding
            while len(clicks.by_idem) > 256:
                clicks.by_idem.popitem(last=False)
        return {k: got[k] for k in ("request_id", "expires_at", "op", "confirm") if k in got}

    @app.post("/api/w/{ws}/click/view")
    def view(body: Step, request: Request, ws: str = Depends(workspace)):
        mine(request, ws, body.request_id)
        token, rid = request.state.token, body.request_id
        # The hand-off's gate on the host's own record: it must be the action that was asked for (the ticket, the exact
        # events and values), read through OUR link. A host that prepared anything else is refused before it is shown.
        wanted = clicks.body_of(token, rid)
        if wanted is not None:  # another request of this action is in flight or unsettled: follow that one
            handoffs.refuse_if_unsettled(ws, binding_of(wanted), exclude=rid)
        try:
            prepared = human(ws, "human.get", {"request_id": rid})
            verify(
                prepared,
                ws,
                expect_op=clicks.op_of(token, rid),
                expect_key=getattr(wanted, "key", None),
                expect_events=expected(wanted) if wanted is not None else None,
            )
        except HostError as exc:
            if exc.error.get("code") != "internal":
                raise
            ended(ws, token, rid)
            raise refusal_check(
                "The host prepared something other than what was asked for. Nothing was shown."
            ) from None
        got = _signer_call("view", ws, rid)
        # The signer's View and digest, unchanged: nothing is added, hidden or re-rendered here.
        shown = got.get("view") if isinstance(got, dict) else None
        if not isinstance(shown, dict) or shown.get("op") != clicks.op_of(token, rid) or shown.get("risk") != RISK:
            raise refusal_check("The review is not what was asked for, or lacks the risk line. Cancel it.")
        return got

    def ended(ws: str, token: str, rid: str) -> None:
        """Nothing more can be signed on this request: cancel it at the host (best effort), free its slot."""
        if handoffs.is_unsettled(rid):
            return  # never cancel a signature that may be in flight, unsettled or just signed
        for call in (
            lambda: signer.call("cancel", {"workspace_id": ws, "request_id": rid}),
            lambda: human(ws, "human.cancel", {"request_id": rid}),
        ):
            try:
                call()
                break
            except Exception:  # noqa: BLE001
                continue
        clicks.forget(token, rid)
        handoffs.settle(token, rid)

    def _signer_call(op: str, ws: str, rid: str, **extra):
        try:
            return signer.call(op, {"workspace_id": ws, "request_id": rid, **extra})
        except HostError:
            sweep()  # a restart may have happened inside this call
            raise

    @app.post("/api/w/{ws}/click/state")
    def state(body: Step, request: Request, ws: str = Depends(workspace)):
        """Where a request stands at the host (read through our own link): the answer to an unknown outcome. An
        unsettled request is answered for any session of this person, and never as `not_found`."""
        rid = body.request_id
        # A tracked request (in flight, unsettled, or just signed) may be read by any session of this person: read-only,
        # it grants nothing else (no view, no sign, no cancel).
        unsettled = handoffs.is_unsettled(rid) == ws
        if unsettled:
            gateway.enabled(ws)
        else:
            mine(request, ws, rid)
        try:
            rows = human(ws, "human.pending", {"request_id": rid})["requests"]
        except HostError:
            if unsettled:
                return {"id": rid, "state": "unknown"}
            raise
        row = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) and rows[0].get("id") == rid else None
        if row is None:
            if unsettled:
                return {"id": rid, "state": "unknown"}
            raise fail("not_found", exit=2)
        got = row.get("state")
        if got not in STATES:
            return {"id": rid, "state": "unknown"}  # the host sent something this dashboard does not know: say so
        if got not in ("prepared", "bound"):
            clicks.forget_all(rid)
            handoffs.settle_all(rid)  # every session's slot, not only this caller's
            handoffs.settled(rid)
        return {"id": rid, "state": got}

    @app.post("/api/w/{ws}/click/sign")
    def sign(body: Sign, request: Request, ws: str = Depends(workspace)):
        mine(request, ws, body.request_id)
        token, rid = request.state.token, body.request_id
        wanted = clicks.body_of(token, rid)
        mark = binding_of(wanted) if wanted is not None else None
        if mark is not None:
            # Another request of this very action in flight or unsettled (another tab): never a second signature.
            handoffs.refuse_if_unsettled(ws, mark, exclude=rid)
            # In flight counts as unsettled from here on, BEFORE the channel is used: a prepare or a hung-signer
            # replacement cannot slip in between, and the mark is already there if the answer is lost.
            handoffs.mark_unsettled(ws, mark, rid, handoffs.clock() + TTL + 60)
        args = {"view_digest": body.view_digest}
        if body.word is not None:
            args["word"] = body.word  # exactly as typed
        try:
            got = signer.call("sign", {"workspace_id": ws, "request_id": rid, **args}, lost=lost_sign())
        except HostError as exc:
            if exc.error.get("reason") != "uncertain" and mark is not None:
                handoffs.settled(rid)  # a definite refusal: nothing is in flight; an uncertain one keeps its mark
            sweep()
            if (exc.error.get("reason") or exc.error["code"]) in ENDED:
                clicks.forget(token, rid)
                handoffs.settle(token, rid)
            raise  # an uncertain outcome keeps its slot and its request: it settles at the host
        clicks.forget(token, rid)
        handoffs.settle(token, rid)
        handoffs.settled(rid)
        if mark is not None:
            # Just signed: a lost browser answer, or a second click on the same action, re-attaches and reads "Signed".
            handoffs.signed(ws, mark, rid, handoffs.clock() + RECENT)
        result = got.get("result") if isinstance(got.get("result"), dict) else {}
        out = {"state": "submitted", "duplicate": bool(got.get("duplicate"))}
        if isinstance(result.get("key"), str):
            out["key"] = result["key"]
        if isinstance(result.get("seq"), int):
            out["seq"] = result["seq"]
        return out

    @app.post("/api/w/{ws}/click/cancel")
    def cancel(body: Step, request: Request, ws: str = Depends(workspace)):
        mine(request, ws, body.request_id)
        token = request.state.token
        if handoffs.is_unsettled(body.request_id):
            raise refusal_check("A signature of this is settling; it cannot be cancelled now.")
        try:
            got = _signer_call("cancel", ws, body.request_id)
        except HostError as exc:
            if (exc.error.get("reason") or exc.error["code"]) in ENDED | {"not_found"}:
                ended(ws, token, body.request_id)  # the signer forgot it: cancel it at the host ourselves
            raise
        clicks.forget(token, body.request_id)
        handoffs.settle(token, body.request_id)
        return {"id": body.request_id, "state": got.get("state")}

    def cancel_session(token: str):
        """Logout: the click requests this session left open are cancelled (best effort, each on its own)."""
        for rid, ws in clicks.open(token):
            if handoffs.is_unsettled(rid):
                continue  # logout never cancels a signature whose outcome is unknown
            try:
                signer.call("cancel", {"workspace_id": ws, "request_id": rid})
            except Exception:  # noqa: BLE001 - one failed cancel must not stop the rest, nor the logout
                pass
            clicks.forget(token, rid)
            handoffs.settle(token, rid)

    app.state.cancel_clicks = cancel_session
