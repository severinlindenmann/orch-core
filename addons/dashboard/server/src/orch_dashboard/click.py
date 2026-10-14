"""The click-to-sign routes (H5c, orch-v2-host section 0.8.6 item 7): prepare, view, sign, cancel and the status card.

A closed set of typed routes, like the hand-off. The browser names a typed action (the same bodies as the hand-off);
the op and its arguments are chosen here, in `host_call`, never by the browser. The View comes from the signer and is
passed on unchanged: **this server never builds a preview for a click request, never logs a View, a word or a
credential, and never signs.** It also never calls the signer from an addon route: only these four handlers do.

**One action intent -> at most one request -> at most one signature.** The idem handed to the signer's `prepare` is
derived from canonical content plus only the gate/question version (`intent.py`), not the click or session;
the signer forwards it to `human.prepare`
and the host answers every tab, retry and re-login with the SAME request (or its terminal state). The records below are
a convenience (which ids this dashboard prepared, what a View is checked against, a settling display), never the guard.

A request obtained through an intent belongs to the PERSON in that workspace: any session of that person may view, sign,
poll and cancel it (the signer separately refuses anything it did not prepare itself, and signs a request once).
"""

import re
import threading
from collections import OrderedDict
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

from . import adapters
from .click_signer import lost_sign
from .errors import HostError, fail
from .gateway import Gateway
from .handoff import PrepareBody, RequestId, done_error, expected, plan, read_done, send_prepare
from .reads import workspace

# The card text, verbatim (owner decision, 12 Oct 2026). A View that carries anything else is not shown.
RISK = "Convenience signing: agents running as you, or anything that takes over this dashboard, could sign."
ViewDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Word = Annotated[str, StringConstraints(min_length=1, max_length=128)]
TTL = 660  # a request lives 10 minutes at most (the signer caps it); the session forgets it a minute later
REMEMBERED = 256
#: refusals after which a request can no longer be signed: its slot is free again
ENDED = {"expired", "not_prepared_here", "custody_changed", "human.request_expired", "human.request_state"}
#: the signer says `the request is <state>` when an intent's idem returns a request that already ended
TERMINAL = re.compile(r"^the request is (submitted|cancelled|expired)$")


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: RequestId


class Sign(Step):
    view_digest: ViewDigest
    word: Word | None = None


class Clicks:
    """What each open click request was prepared for (workspace, op, typed body, signer epoch): the View is checked
    against it, and an orphan of an earlier signer is found by its epoch. Not a guard of anything."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ops: OrderedDict[str, tuple[str, str, object, int]] = OrderedDict()

    def add(self, ws: str, request_id: str, op: str, body: object, epoch: int) -> None:
        with self.lock:
            self.ops[request_id] = (ws, op, body, epoch)
            self.ops.move_to_end(request_id)
            while len(self.ops) > REMEMBERED:
                self.ops.popitem(last=False)

    def get(self, request_id: str) -> tuple[str, str, object, int]:
        with self.lock:
            return self.ops.get(request_id, ("", "", None, 0))

    def stale(self, epoch: int) -> list[tuple[str, str]]:
        """(workspace, request id) of every click request a signer of an earlier life prepared."""
        with self.lock:
            return [(ws, rid) for rid, (ws, _, _, e) in self.ops.items() if e != epoch]

    def forget(self, request_id: str) -> None:
        with self.lock:
            self.ops.pop(request_id, None)


def verify(prepared: dict, ws: str, **expect) -> dict:
    """The hand-off's checks on the host's own record (`adapters.handoff.request`): op, ticket key, exact events."""
    return adapters.handoff.request(prepared, ws, **expect)


def refusal_check(message: str):
    return HostError(
        {"code": "human.click_refused", "message": message, "retryable": False, "reason": "check_failed"}, 3
    )


def register(app: FastAPI, gateway: Gateway):
    clicks = app.state.clicks = Clicks()
    handoffs = app.state.handoffs  # the quota of open requests is one per session, shared with the hand-off
    signer = gateway.click
    signer.unsettled = handoffs.any_unsettled  # a hung signer is not replaced while a signature may be mid-submit
    STATES = ("prepared", "bound", "submitted", "cancelled", "expired")

    def human(ws: str, op: str, args: dict):
        return app.state.human(ws, op, args)

    def sweep() -> None:
        """After a signer restart its approvals are gone: cancel the orphaned click requests at the host through OUR
        link and give their quota slots back, so they cannot hold a session's limit for 11 minutes."""
        for ws, rid in clicks.stale(signer.restarts):
            if handoffs.is_unsettled(rid):
                continue  # its signature may be at the host: it settles by itself, nothing is cancelled or forgotten
            try:
                human(ws, "human.cancel", {"request_id": rid})
            except Exception:  # noqa: BLE001 - already settled, or the host is away: the host expires it anyway
                pass
            clicks.forget(rid)
            handoffs.settle_all(rid)
            handoffs.disown(rid)

    def mine(ws: str, request_id: str) -> None:
        """A request of this PERSON in this workspace (any of their sessions); another's, or none, is `not_found`."""
        gateway.enabled(ws)  # the workspace is pinned and the dashboard addon is enabled in it, like the hand-off
        sweep()
        if not handoffs.owns(app.state.person_of(ws), ws, request_id):
            raise fail("not_found", exit=2)

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
        with handoffs.lock:
            handoffs.reserve(token)  # check and take a slot in one step (shared with the hand-off)
        try:
            return _prepare(body, token, ws)
        except BaseException:
            handoffs.release(token)
            raise

    def _prepare(body, token, ws):
        planned = plan(gateway, ws, body)  # the only place an op name is chosen
        # A terminal hand-off of this very action, prepared before the flag was switched on, is still open: nothing
        # is prepared beside it (the host refuses to flip the flag with one pending; this is defence in depth).
        key = getattr(body, "key", None)
        for row in human(ws, "human.pending", {}).get("requests", []):
            if (
                isinstance(row, dict)
                and row.get("code")
                and row.get("state") in ("prepared", "bound")
                and row.get("op") == planned.op
                and row.get("key") == key
            ):
                raise HostError(
                    {
                        "code": "intent.open",
                        "message": "An attempt from the terminal is still open; it expires at "
                        + f"{row.get('expires_at')}.",
                        "retryable": False,
                    },
                    exit=5,
                )
        files = planned.files

        def call(idem: str):
            sent = {"workspace_id": ws, "op": planned.op, "args": planned.args, "idem": idem}
            if files:
                sent["files"] = files
            return signer.call("prepare", sent)

        try:
            got = send_prepare(planned, ws, body.retry, call, lambda: human(ws, "human.pending", {}))
        except HostError as exc:
            sweep()
            said = str(exc.error.get("message", "")) if exc.error["code"] == "human.request_state" else ""
            ended_as = TERMINAL.fullmatch(said)
            if ended_as is None:
                raise
            # The host returned this intent's request, and it has ended: say how, and offer an explicit new attempt.
            state = ended_as.group(1)
            if state == "submitted":
                # The signer drops the terminal request id. Replay the exact binding through our link to obtain
                # the host record; opts.idem guarantees this cannot create another request.
                device = signer.status().get(ws, {}).get("device")
                if not device:
                    raise fail("host.unavailable", "Cannot read the earlier signing record.") from None
                try:
                    prepared = app.state.human(
                        ws,
                        "human.prepare",
                        {"op": planned.op, "args": planned.args, "signer": {"device": device, "auth": "convenience"}},
                        idem=planned.idem(ws, body.retry),
                        files=files,
                    )
                except HostError as exc:
                    # The same mapping as send_prepare: never a raw idem code to the browser (Opus re-check N3).
                    code = exc.error.get("code")
                    if code == "idem.expired":
                        raise fail(
                            "intent.expired",
                            "This attempt is older than the host keeps records (24 h). Start again only if you intend "
                            "another attempt; an earlier one may have been signed.",
                            exit=5,
                        ) from None
                    if code == "idem.conflict":
                        raise fail(
                            "intent.blocked",
                            "The earlier attempt was signed, but its record can't be read here. Start again only if "
                            "you intend another attempt; an earlier one may have been signed.",
                            exit=5,
                        ) from None
                    raise
                if prepared.get("request", {}).get("state") != "submitted":
                    raise fail("internal", "The host did not return the submitted intent.")
                raise read_done(human, ws, prepared["request"]["id"]) from None

            raise HostError(
                {
                    "code": "intent.ended",
                    "message": f"The earlier attempt is {state}.",
                    "retryable": False,
                    "state": state,
                },
                exit=5,
            ) from None
        rid = got["request_id"]
        clicks.forget(rid)  # adopted by the new signer; it is no longer an orphan
        sweep()
        clicks.add(ws, rid, planned.op, body, signer.restarts)
        handoffs.own(planned.person, ws, rid, TTL)
        handoffs.commit(token, ws, rid, until=handoffs.clock() + TTL, kind="click")
        return {k: got[k] for k in ("request_id", "expires_at", "op", "confirm") if k in got}

    @app.post("/api/w/{ws}/click/view")
    def view(body: Step, ws: str = Depends(workspace)):
        mine(ws, body.request_id)
        rid = body.request_id
        _, op, wanted, _ = clicks.get(rid)
        # The hand-off's gate on the host's own record: it must be the action that was asked for (the ticket, the exact
        # events and values), read through OUR link. A host that prepared anything else is refused before it is shown.
        try:
            prepared = human(ws, "human.get", {"request_id": rid})
            verify(
                prepared,
                ws,
                expect_op=op,
                expect_key=getattr(wanted, "key", None),
                expect_events=expected(wanted) if wanted is not None else None,
            )
        except HostError as exc:
            if exc.error.get("code") != "internal":
                raise
            ended(ws, rid)
            raise refusal_check(
                "The host prepared something other than what was asked for. Nothing was shown."
            ) from None
        got = _signer_call("view", ws, rid)
        # The signer's View and digest, unchanged: nothing is added, hidden or re-rendered here.
        shown = got.get("view") if isinstance(got, dict) else None
        if not isinstance(shown, dict) or shown.get("op") != op or shown.get("risk") != RISK:
            raise refusal_check("The review is not what was asked for, or lacks the risk line. Cancel it.")
        return got

    def ended(ws: str, rid: str) -> None:
        """Nothing more can be signed on this request: cancel it at the host (best effort), free its slots."""
        if handoffs.is_unsettled(rid):
            return  # never cancel a signature that may be in flight or unsettled
        for call in (
            lambda: signer.call("cancel", {"workspace_id": ws, "request_id": rid}),
            lambda: human(ws, "human.cancel", {"request_id": rid}),
        ):
            try:
                call()
                break
            except Exception:  # noqa: BLE001
                continue
        clicks.forget(rid)
        handoffs.settle_all(rid)
        handoffs.disown(rid)  # it can never be signed: it looks like one that never existed

    def _signer_call(op: str, ws: str, rid: str, **extra):
        try:
            return signer.call(op, {"workspace_id": ws, "request_id": rid, **extra})
        except HostError:
            sweep()  # a restart may have happened inside this call
            raise

    @app.post("/api/w/{ws}/click/state")
    def state(body: Step, ws: str = Depends(workspace)):
        """Where a request stands at the host (read through our own link): the answer to an unknown outcome."""
        rid = body.request_id
        mine(ws, rid)
        try:
            rows = human(ws, "human.pending", {"request_id": rid})["requests"]
        except HostError:
            if handoffs.is_unsettled(rid):
                return {"id": rid, "state": "unknown"}
            raise
        row = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) and rows[0].get("id") == rid else None
        if row is None:
            if handoffs.is_unsettled(rid):
                return {"id": rid, "state": "unknown"}
            raise fail("not_found", exit=2)
        got = row.get("state")
        if got not in STATES:
            return {"id": rid, "state": "unknown"}  # the host sent something this dashboard does not know: say so
        if got not in ("prepared", "bound"):
            handoffs.settle_all(rid)  # every session's slot
            handoffs.settled(rid)
            clicks.forget(rid)
        if got == "submitted" and row.get("seq") is None:
            raise done_error({}, row)
        return {"id": rid, "state": got}

    @app.post("/api/w/{ws}/click/sign")
    def sign(body: Sign, ws: str = Depends(workspace)):
        mine(ws, body.request_id)
        rid = body.request_id
        # In flight counts as unsettled from here on, BEFORE the channel is used: nothing here cancels the request or
        # replaces the signer under it, and the mark is already there if the answer is lost. (A second signature of
        # the request is refused by the signer and the host, which are the ones that know.)
        prior = handoffs.is_unsettled(rid)
        handoffs.mark_unsettled(ws, rid, handoffs.clock() + TTL + 60)
        args = {"view_digest": body.view_digest}
        if body.word is not None:
            args["word"] = body.word  # exactly as typed
        try:
            got = signer.call("sign", {"workspace_id": ws, "request_id": rid, **args}, lost=lost_sign())
        except HostError as exc:
            if exc.error.get("reason") == "uncertain":
                # A concurrent definite failure may have cleared the in-flight mark.
                handoffs.mark_unsettled(ws, rid, handoffs.clock() + TTL + 60)
            elif prior is None:
                # A definite retry failure must not clear an earlier unknown outcome.
                # Clear new marks before follow-up host reads, which may themselves fail.
                handoffs.settled(rid)
            if exc.error.get("code") == "human.request_state":
                rows = human(ws, "human.pending", {"request_id": rid}).get("requests", [])
                if len(rows) == 1 and rows[0].get("state") == "submitted":
                    prepared = human(ws, "human.get", {"request_id": rid})
                    handoffs.settled(rid)
                    handoffs.settle_all(rid)
                    clicks.forget(rid)
                    raise done_error(prepared, rows[0]) from None
            sweep()
            if exc.error.get("reason") == "disabled":
                ended(ws, rid)  # the flag was switched off: this request can never be signed, it reads as ended
            elif (exc.error.get("reason") or exc.error["code"]) in ENDED:
                clicks.forget(rid)
                handoffs.settle_all(rid)
                handoffs.disown(rid)
            raise  # an uncertain outcome keeps its slot and its request: it settles at the host
        clicks.forget(rid)
        handoffs.settle_all(rid)
        handoffs.settled(rid)
        result = got.get("result") if isinstance(got.get("result"), dict) else {}
        out = {"state": "submitted", "duplicate": bool(got.get("duplicate"))}
        if isinstance(result.get("key"), str):
            out["key"] = result["key"]
        if isinstance(result.get("seq"), int):
            out["seq"] = result["seq"]
        return out

    @app.post("/api/w/{ws}/click/cancel")
    def cancel(body: Step, ws: str = Depends(workspace)):
        mine(ws, body.request_id)
        rid = body.request_id
        if handoffs.is_unsettled(rid):
            raise refusal_check("A signature of this is settling; it cannot be cancelled now.")
        try:
            got = _signer_call("cancel", ws, rid)
        except HostError as exc:
            if (exc.error.get("reason") or exc.error["code"]) in ENDED | {"not_found"}:
                ended(ws, rid)  # the signer forgot it: cancel it at the host ourselves
            raise
        clicks.forget(rid)
        handoffs.settle_all(rid)
        handoffs.disown(rid)
        return {"id": rid, "state": got.get("state")}

    def cancel_session(token: str):
        """Logout: the click requests this session left open are cancelled (best effort, each on its own), except one
        whose signature is unknown and one another session of the person holds too."""
        for rid, ws in handoffs.held(token, "click"):
            if handoffs.is_unsettled(rid):
                continue  # logout never cancels a signature whose outcome is unknown
            if handoffs.shared(token, rid):
                handoffs.settle(token, rid)
                continue
            try:
                signer.call("cancel", {"workspace_id": ws, "request_id": rid})
            except Exception:  # noqa: BLE001 - one failed cancel must not stop the rest, nor the logout
                pass
            clicks.forget(rid)
            handoffs.settle(token, rid)

    app.state.cancel_clicks = cancel_session
