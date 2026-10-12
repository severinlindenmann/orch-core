"""The click-to-sign routes (H5c, orch-v2-host section 0.8.6 item 7): prepare, view, sign, cancel and the status card.

A closed set of typed routes, like the hand-off. The browser names a typed action (the same bodies as the hand-off);
the op and its arguments are chosen here, in `host_call`, never by the browser. The View comes from the signer and is
passed on unchanged: **this server never builds a preview for a click request, never logs a View, a word or a
credential, and never signs.** It also never calls the signer from an addon route: only these four handlers do.

A request is the browser session's: view, sign and cancel of an id this session did not prepare look like a request
that never existed (the signer separately refuses anything it did not prepare itself).
"""

import threading
import time
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

from . import adapters
from .errors import fail
from .gateway import Gateway
from .handoff import (
    WORKSPACE_OPS,
    AddLabel,
    PrepareBody,
    RequestId,
    host_call,
    host_files,
    host_idem,
)
from .reads import workspace

ViewDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Word = Annotated[str, StringConstraints(min_length=1, max_length=128)]
TTL = 660  # a request lives 10 minutes at most (the signer caps it); the session forgets it a minute later
REMEMBERED = 64


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

    def add(self, token: str, ws: str, request_id: str) -> None:
        with self.lock:
            mine = self.owned.setdefault(token, {})
            now = self.clock()
            for rid in [rid for rid, (_, deadline) in mine.items() if deadline <= now]:
                del mine[rid]
            mine[request_id] = (ws, now + TTL)
            while len(mine) > REMEMBERED:
                del mine[next(iter(mine))]

    def has(self, token: str, ws: str, request_id: str) -> bool:
        with self.lock:
            hit = self.owned.get(token, {}).get(request_id)
            return bool(hit and hit[0] == ws and hit[1] > self.clock())

    def forget(self, token: str, request_id: str) -> None:
        with self.lock:
            self.owned.get(token, {}).pop(request_id, None)

    def open(self, token: str) -> list[tuple[str, str]]:
        with self.lock:
            return [(rid, ws) for rid, (ws, _) in self.owned.get(token, {}).items()]


def register(app: FastAPI, gateway: Gateway):
    clicks = app.state.clicks = Clicks()
    signer = gateway.click

    def mine(request: Request, ws: str, request_id: str) -> None:
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
        clicks.add(token, ws, got["request_id"])
        return {k: got[k] for k in ("request_id", "expires_at", "op", "confirm") if k in got}

    @app.post("/api/w/{ws}/click/view")
    def view(body: Step, request: Request, ws: str = Depends(workspace)):
        mine(request, ws, body.request_id)
        # The signer's View and digest, unchanged: nothing is added, hidden or re-rendered here.
        return signer.call("view", {"workspace_id": ws, "request_id": body.request_id})

    @app.post("/api/w/{ws}/click/sign")
    def sign(body: Sign, request: Request, ws: str = Depends(workspace)):
        mine(request, ws, body.request_id)
        args = {"workspace_id": ws, "request_id": body.request_id, "view_digest": body.view_digest}
        if body.word is not None:
            args["word"] = body.word  # exactly as typed
        got = signer.call("sign", args)
        clicks.forget(request.state.token, body.request_id)
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
        got = signer.call("cancel", {"workspace_id": ws, "request_id": body.request_id})
        clicks.forget(request.state.token, body.request_id)
        return {"id": body.request_id, "state": got.get("state")}

    def cancel_session(token: str):
        """Logout: the click requests this session left open are cancelled (best effort, each on its own)."""
        for rid, ws in clicks.open(token):
            try:
                signer.call("cancel", {"workspace_id": ws, "request_id": rid})
            except Exception:  # noqa: BLE001 - one failed cancel must not stop the rest, nor the logout
                pass
            clicks.forget(token, rid)

    app.state.cancel_clicks = cancel_session
