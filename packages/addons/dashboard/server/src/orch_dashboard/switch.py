"""Hub and dashboard as one app (H1, 27 Oct; mockups/hub-one-app/TECH.md "Decisions").

The two sites are different origins, so a switch carries a one-use 30 s ticket in the URL fragment. The HUB is the only
authority for a ticket (it stores it and marks it used); this process never verifies a signature and holds no hub
credential: it asks its own host, and the host asks the hub over the authenticated link (`hub.switch`).

* `POST /auth/switch`           pre-login: `{t}` is a ticket the hub console made for this workspace. The host redeems
                                at the hub (single use, audience = this workspace's link, user = the workspace owner's
                                hub alias). `open`: a session of kind `switch` (not `local`) and the path to land on;
                                `signout`: nothing is opened, the page ends its own session.
* `POST /api/w/{ws}/hub/switch` `{return_to, kind}`: a ticket to open the hub console (`open`) or to sign out of it
                                (`signout`); answers the URL to navigate to (`<hub origin>/#t=<ticket>`).
* `GET  /api/w/{ws}/hub/rail`   the company rail: the hub and the owner's workspaces (name, role, online, badge),
                                fetched
                                server side. No hub joined: `{"hub": null, "workspaces": []}`.

The ticket lives in the fragment only (never a query, never a log); the browser page strips it at once.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
import urllib.parse
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from . import reads
from .errors import HostError, fail, response
from .gateway import Gateway
from .policy import LOOPBACK, REMOTE

TICKET = re.compile(r"^([0-9a-f]{32})\.[0-9a-f]{32}$")
RETURN_TO = re.compile(r"^/(?!/)[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,255}$")  # a path only, like the hub's
RAIL_TTL = 15.0  # seconds a rail answer is reused (per workspace): a page load must not hit the hub every time
_NO_HUB = {"hub.not_joined", "hub.state_mismatch", "hub.unreachable"}


def safe_return_to(value) -> str:
    """A path on this site, else Home: one leading slash, no `//`, no backslash, no `..`, no control character."""
    if not isinstance(value, str) or RETURN_TO.fullmatch(value) is None or ".." in value or "\\" in value:
        return "/"
    return value


class SwitchBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    t: Annotated[str, StringConstraints(pattern=TICKET.pattern)]


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


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _is_local_address(host: str) -> bool:
    """True when ``host`` is an IP address assigned to THIS computer (a bind to it works)."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.bind((str(ip), 0))
        return True
    except OSError:
        return False


def reachable_origin(origin, browser, is_local=_is_local_address, probe=_tcp_open):
    """FB10: the hub names its own address (often a private one such as a NetBird IP). A computer cannot always dial
    its own private address, so when the browser reached THIS dashboard over loopback and the hub runs on this same
    computer, the hub is opened on 127.0.0.1 instead (same port), but only when something answers there. Anything
    else (a remote hub, a browser on another address, nothing listening on loopback) keeps the hub's own origin."""
    if not isinstance(origin, str) or not isinstance(browser, str):
        return origin
    try:
        o, b = urllib.parse.urlsplit(origin), urllib.parse.urlsplit(browser)
        port = o.port
    except ValueError:
        return origin
    if (b.hostname or "") not in ("127.0.0.1", "localhost", "::1") or not o.hostname or port is None:
        return origin
    if o.hostname in ("127.0.0.1", "localhost", "::1") or not is_local(o.hostname):
        return origin
    return f"{o.scheme}://127.0.0.1:{port}" if probe("127.0.0.1", port) else origin


def reachable_url(url, browser, **kw):
    """The ticket address (`<hub origin>/#t=...`) on an origin this browser can open (see `reachable_origin`)."""
    if not isinstance(url, str):
        return url
    base, sep, rest = url.partition("/#")
    return reachable_origin(base, browser, **kw) + sep + rest


def rail_view(raw, browser=None) -> dict:
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
            "origin": reachable_origin(origin, browser)
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

    @app.post("/auth/switch")
    def redeem(body: SwitchBody, request: Request):
        ws = TICKET.fullmatch(body.t).group(1)
        a = auth or app.state.auth
        client = getattr(request.state, "client", None) if request.scope.get("orch_listener") == REMOTE else None
        if not a.switch_attempt(client):
            return response("rate.limited", 429)
        try:
            got = link_for(ws).call("hub.switch", {"kind": "redeem", "ticket": body.t})
        except HostError:
            a.switch_attempt(client)  # a refused ticket costs its source a second charge
            return response("session.required", 401)  # one answer for a bad, used, expired or foreign ticket
        if got.get("kind") == "signout":
            return JSONResponse({"signed_out": True})
        listener = request.scope.get("orch_listener", LOOPBACK)
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
        return {"url": reachable_url(got["url"], browser_origin(request))}

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
        out = rail_view(raw, browser_origin(request))
        cache[ws] = (clock(), out)
        return out
