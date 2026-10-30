"""Hub and dashboard as one app (H1, 27 Oct; mockups/hub-one-app/TECH.md "Decisions").

The two sites are different origins, so a switch carries a one-use 30 s ticket in the URL fragment. The HUB is the only
authority for a ticket (it stores it and marks it used); this process never verifies a signature and holds no hub
credential: it asks its own host, and the host asks the hub over the authenticated link (`hub.switch`).

* `POST /auth/switch/challenge` pre-login, loopback only (E4): `{ticket_hash, workspace_id}` -> `{nonce, name}`. The
                                nonce is single use, 60 s, bound to the ticket hash and this origin; `name` is the
                                workspace's name for the "Open <name>?" confirm (null when unknown). No ticket is used.
* `POST /auth/switch`           pre-login: `{t}` is a ticket the hub console made for this workspace. The host redeems
                                at the hub (single use, audience = this workspace's link, user = the workspace owner's
                                hub alias). `open`: a session of kind `switch` (not `local`) and the path to land on;
                                `signout`: nothing is opened, the page ends its own session.
                                `{t, key_id, sig}` (E4): the browser proves its enrolled key over
                                `trusted.message` BEFORE the redeem. The server checks the key (enrolled, not revoked,
                                not idle, this origin, loopback) and that the key's enrolled person, this dashboard's
                                local person in the workspace and the ticket's user (the workspace owner) are the same
                                person. Any failure: 401 `switch.unverified` and the ticket is NOT used (the page asks
                                "Open <name>?"). Success: redeem and a full `local` session in one step.
                                D93 (remote access, R2): the host may refuse the redeem (`switch.refused`: the owner
                                said the computer runs at a customer's place, or the hub is joined selectively):
                                403 with the plain line "Open this workspace on its own computer."; the ticket
                                stays unused.
* `POST /api/w/{ws}/hub/switch` `{return_to, kind}`: a ticket to open the hub console (`open`) or to sign out of it
                                (`signout`); answers the URL to navigate to (`<hub origin>/#t=<ticket>`).
* `GET  /api/w/{ws}/hub/rail`   the company rail: the hub and the owner's workspaces (name, role, online, badge),
                                fetched
                                server side. No hub joined: `{"hub": null, "workspaces": []}`.

The ticket lives in the fragment only (never a query, never a log); the browser page strips it at once.
"""

from __future__ import annotations

import re
import time
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from . import adapters, reads
from .errors import HostError, fail, response
from .gateway import Gateway
from .policy import LOOPBACK, REMOTE
from .text import hidden

TICKET = re.compile(r"^([0-9a-f]{32})\.[0-9a-f]{32}$")
KEY_ID = r"^[0-9a-f]{32}$"
SIG = r"^[A-Za-z0-9_-]{86}$"
RETURN_TO = re.compile(r"^/(?!/)[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,255}$")  # a path only, like the hub's
RAIL_TTL = 15.0  # seconds a rail answer is reused (per workspace): a page load must not hit the hub every time
SWITCH_REFUSED = "Open this workspace on its own computer."
_NO_HUB = {"hub.not_joined", "hub.state_mismatch", "hub.unreachable"}


def safe_return_to(value) -> str:
    """A path on this site, else Home: one leading slash, no `//`, no backslash, no `..`, no control character."""
    if not isinstance(value, str) or RETURN_TO.fullmatch(value) is None or ".." in value or "\\" in value:
        return "/"
    return value


class SwitchBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    t: Annotated[str, StringConstraints(pattern=TICKET.pattern)]
    key_id: Annotated[str, StringConstraints(pattern=KEY_ID)] | None = None
    sig: Annotated[str, StringConstraints(pattern=SIG)] | None = None


class ChallengeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticket_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    workspace_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class TicketBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    return_to: Annotated[str, StringConstraints(max_length=256)] = "/"
    kind: Literal["open", "signout"] = "open"


def browser_origin(request: Request) -> str:
    """The origin the browser uses to reach this dashboard (from the one Host this listener answered, which the
    security layer already checked). The hub keeps it so its own pages can link back."""
    policies = request.app.state.policies
    policy = policies.get(request.scope.get("orch_listener", LOOPBACK)) or policies[LOOPBACK]
    return policy.origin_for(request.headers["host"])


def rail_view(raw) -> dict:
    """The hub's `rail_get` answer reduced to the closed fields the page shows; anything else is dropped."""
    raw = raw if isinstance(raw, dict) else {}
    hub = raw.get("hub") if isinstance(raw.get("hub"), dict) else {}
    rows = []
    for r in (raw.get("workspaces") if isinstance(raw.get("workspaces"), list) else [])[:50]:
        if not isinstance(r, dict) or not isinstance(r.get("workspace_id"), str):
            continue
        origin = r.get("origin")
        rows.append(
            {
                "workspace_id": r["workspace_id"][:64],
                "name": r["name"][:80] if isinstance(r.get("name"), str) else None,
                "role": r["role"] if r.get("role") in ("read", "edit", "admin", "owner") else None,
                "online": r.get("online") is True,
                "badge": r["badge"] if isinstance(r.get("badge"), int) and 0 <= r["badge"] < 1000 else 0,
                "origin": origin
                if isinstance(origin, str) and re.fullmatch(r"https?://[A-Za-z0-9.\[\]:-]{1,200}", origin)
                else None,
                "current": r.get("current") is True,
            }
        )
    origin = hub.get("origin")
    return {
        "hub": {
            "name": hub["name"][:80] if isinstance(hub.get("name"), str) else "Hub",
            "origin": origin
            if isinstance(origin, str) and re.fullmatch(r"https?://[A-Za-z0-9.\[\]:-]{1,200}", origin)
            else None,
        },
        "workspaces": rows,
    }


def register(app: FastAPI, gateway: Gateway, auth=None, clock=time.monotonic):
    cache: dict[str, tuple[float, dict]] = {}

    def link_for(ws: str):
        link = gateway.enabled(ws)
        if not link.supports("hub.switch", "hub/1"):
            raise fail("click.contract", "Update orch-core: it cannot switch to a hub yet.", exit=9)
        return link

    def workspace_name(ws: str) -> str | None:
        """The name for "Open <name>?", or None (not served here, host down, or a name that cannot be shown safely)."""
        try:
            name = gateway.enabled(ws).call("workspace.show").get("name")
        except (HostError, AttributeError):
            return None
        return name[:80] if isinstance(name, str) and name and not hidden(name) else None

    def same_person(link, ws: str, kid_ok) -> bool:
        """E4's identity rule, BEFORE the redeem: the key's enrolled person (checked by `kid_ok`, given this
        dashboard's local person), this dashboard's local person in ``ws``, and the ticket's user, who can only ever be
        the workspace owner (`hub.switch` redeems a ticket for the owner's hub alias only): so the local person must
        be the owner here. Any host failure is a no."""
        try:
            person = link.welcome["peer"]["person"]
            if not any(m.person == person and m.role == "owner" for m in adapters.members(link.call("member.list"))):
                return False
        except (HostError, KeyError, TypeError, AttributeError, ValueError):  # ValueError: a roster that does not parse
            return False
        return kid_ok(person)

    @app.post("/auth/switch/challenge")
    def challenge(body: ChallengeBody, request: Request):
        a = auth or app.state.auth
        listener = request.scope.get("orch_listener", LOOPBACK)
        nonce = a.switch_challenge(body.ticket_hash, browser_origin(request), listener)
        if nonce is None:
            return response("rate.limited", 429)
        return JSONResponse({"nonce": nonce, "name": workspace_name(body.workspace_id)})

    @app.post("/auth/switch")
    def redeem(body: SwitchBody, request: Request):
        ws = TICKET.fullmatch(body.t).group(1)
        a = auth or app.state.auth
        listener = request.scope.get("orch_listener", LOOPBACK)
        client = getattr(request.state, "client", None) if listener == REMOTE else None
        if not a.switch_attempt(client):
            return response("rate.limited", 429)
        try:
            link = link_for(ws)
        except HostError:
            a.switch_attempt(client)
            return response("session.required", 401)
        keyed = body.key_id is not None or body.sig is not None
        if keyed:
            # The proof comes first: a failed or partial one never reaches the hub, so the ticket stays good for the
            # "Open <name>?" confirm.
            origin = browser_origin(request)
            proved = same_person(
                link,
                ws,
                lambda person: a.switch_proof(body.t, ws, body.key_id, body.sig, origin, listener, person),
            )
            if not proved:
                return response("switch.unverified", 401)
        try:
            got = link.call("hub.switch", {"kind": "redeem", "ticket": body.t})
        except HostError as exc:
            if exc.error["code"] == "switch.refused":  # D93 (R2): the host refused before the hub: the ticket is unused
                return JSONResponse(
                    {"ok": False, "error": {"code": "switch.refused", "message": SWITCH_REFUSED, "retryable": False}},
                    status_code=403,
                )
            a.switch_attempt(client)  # a refused ticket costs its source a second charge
            return response("session.required", 401)  # one answer for a bad, used, expired or foreign ticket
        if got.get("kind") == "signout":
            return JSONResponse({"signed_out": True})
        minted = a.open_keyed(body.key_id, ws) if keyed else None
        if minted is None:  # no proof, or the key was revoked between the proof and the redeem: read-mostly
            minted = a.open_switch(listener, ws)
        return JSONResponse({**minted, "return_to": safe_return_to(got.get("return_to")), "workspace_id": ws})

    @app.post("/api/w/{ws}/hub/switch")
    def ticket(body: TicketBody, request: Request, ws: str = Depends(reads.workspace)):
        got = link_for(ws).call(
            "hub.switch",
            {
                "kind": "ticket",
                "origin": browser_origin(request),
                "return_to": safe_return_to(body.return_to),
                "ticket_kind": body.kind,
            },
        )
        return {"url": got["url"]}

    @app.get("/api/w/{ws}/hub/rail")
    def rail(request: Request, ws: str = Depends(reads.workspace)):
        hit = cache.get(ws)
        if hit is not None and clock() - hit[0] < RAIL_TTL:
            return hit[1]
        try:
            raw = link_for(ws).call("hub.switch", {"kind": "rail", "origin": browser_origin(request)})
        except HostError as exc:
            if exc.error["code"] in _NO_HUB or exc.error["code"] == "hub.refused":
                return {"hub": None, "workspaces": []}
            raise
        out = rail_view(raw)
        cache[ws] = (clock(), out)
        return out
