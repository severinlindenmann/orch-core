"""Hub signing from Mission Control (V3 C11, D78/D81): in a workspace joined to a hub in full mode this server signs
nothing and holds no hub credential. It can only (1) ask the host for the workspace's signing mode and (2) ask the
host's read `hub.handoff` for what the "Approve on your phone or in the hub" panel shows: a summary, the 12-hex short
hash and one link the HOST built from its pinned hub address.

FB15 (the one prepare here): `POST /hub/approve/prepare {action: addon_grant | market_grant}` asks the host to prepare
an add-on grant as an UNBOUND request for the hub (`human.prepare {handoff: true}`, no signer; core marks it
`hub_handoff`), for the owner of a workspace joined in full mode, from a full session. `GET /hub/approve/<id>` polls a
request that route prepared. No other action and no other hand-off is prepared here.

**This module imports no hub code, opens no connection to a hub, and builds no link.** The link is checked against
the host's closed grammar (`URL`) and dropped if it does not match; agent-written text (`summary.title`) is one plain
field the page renders as text, never as a link.
"""

import re
from dataclasses import replace
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import adapters
from .errors import fail
from .gateway import Gateway
from .reads import Key, workspace
from .signing import TTL, AddonGrant, Handoffs, MarketGrant, expected, plan, read_done, send_prepare

#: modes in which no local key signs ("unknown" fails closed as full)
HUB_ONLY = ("full", "unknown")
MODES = ("standalone", "selective", "full", "unknown")
SHORT = re.compile(r"^[0-9a-f]{4} [0-9a-f]{4} [0-9a-f]{4}$")
#: the host's grammar: https://<host>[:port]/#/w/<workspace>/t/<ticket ULID>[/g/<gate>]
URL = re.compile(
    r"^https://(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9a-fA-F:.]+\])(?::[0-9]{1,5})?/#/w/[0-9a-f]{32}/t/"
    r"[0-7][0-9A-HJKMNP-TV-Z]{25}(?:/g/(?:requirements|plan|code|verify))?$"
)
#: FB15: the add-on approval link, ``/#/w/<workspace>/addon/<b64u terms>`` (the host's `build_addon_url`)
ADDON_URL = re.compile(
    r"^https://(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9a-fA-F:.]+\])(?::[0-9]{1,5})?/#/w/[0-9a-f]{32}/addon/[A-Za-z0-9_-]{1,8192}$"
)
#: the settings-change link, ``/#/w/<workspace>/settings/<b64u set>`` (the host's `build_settings_url`)
SETTINGS_URL = re.compile(
    r"^https://(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9a-fA-F:.]+\])(?::[0-9]{1,5})?/#/w/[0-9a-f]{32}/settings/[A-Za-z0-9_-]{1,512}$"
)
#: the only settings the hub's channel may set (hub protocol 8.8), with their ranges
SETTINGS_KEYS = {"grant_hours": (1, 168), "claim_ttl_min": (15, 1440), "lease_ttl_min": (5, 1440)}
#: the same link on the hub's web console (the origin the person signs in to), set by `with_console_origin`
CONSOLE_ORIGIN = re.compile(r"^https?://(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9a-fA-F:.]+\])(?::[0-9]{1,5})?$")
RequestId = Annotated[str, StringConstraints(pattern=r"^hr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
Qid = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]


class HubSign(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["gate", "verdict", "question", "status", "addon", "settings"]
    ticket: Key | None = None
    request: RequestId | None = None  # kind addon: the prepared hand-off request the link is for
    gate: Literal["requirements", "plan", "code", "verify"] | None = None
    question: Qid | None = None
    set: dict[str, Annotated[int, Field(strict=True)]] | None = None  # kind settings: the change the link is for


def mode_of_link(link) -> str | None:
    """The host's signing mode from a pinned host link, or None when this person cannot read it (`hub.status` is for
    owners) or the host predates it. Advisory only: the host refuses a local key in full mode whatever this says."""
    try:
        if not link.supports("hub.status", "hub/1"):
            return None
        mode = link.call("hub.status").get("signing")
    except Exception:  # noqa: BLE001 - advisory read: any doubt is "not known", and the host still decides
        return None
    return mode if mode in MODES else None


def mode_of(gateway: Gateway, ws: str) -> str | None:
    try:
        return mode_of_link(gateway.enabled(ws))
    except Exception:  # noqa: BLE001 - as above
        return None


def addon_panel(got: dict) -> dict:
    """FB15: of the host's add-on answer only the closed fields, each checked (the link by the host's grammar)."""
    s, short, url = got.get("summary"), got.get("short_hash"), got.get("url")
    caps = s.get("capabilities") if isinstance(s, dict) else None
    if (
        got.get("mode") not in HUB_ONLY
        or not isinstance(s, dict)
        or not (isinstance(short, str) and SHORT.fullmatch(short))
        or not (isinstance(url, str) and ADDON_URL.fullmatch(url))
        or not isinstance(s.get("name"), str)
        or not isinstance(s.get("version"), str)
        or not isinstance(caps, list)
        or not all(isinstance(c, str) for c in caps)
    ):
        raise fail("internal", exit=1)
    return {
        "mode": got["mode"],
        "kind": "addon",
        "summary": {"kind": "addon", "name": s["name"][:60], "version": s["version"][:40], "capabilities": caps[:12]},
        "short_hash": short,
        "url": url,
    }


def settings_panel(got: dict) -> dict:
    """The host's settings-change answer: the closed summary (only the three channel keys, in range) and the link."""
    s, url = got.get("summary"), got.get("url")
    change = s.get("set") if isinstance(s, dict) else None
    if (
        got.get("mode") not in HUB_ONLY
        or not isinstance(change, dict)
        or not change
        or not all(
            k in SETTINGS_KEYS and type(v) is int and SETTINGS_KEYS[k][0] <= v <= SETTINGS_KEYS[k][1]
            for k, v in change.items()
        )
        or not (isinstance(url, str) and SETTINGS_URL.fullmatch(url))
    ):
        raise fail("internal", exit=1)
    return {
        "mode": got["mode"],
        "kind": "settings",
        "summary": {"kind": "settings", "set": dict(change)},
        "short_hash": None,
        "url": url,
    }


def with_console_origin(info: dict, link, request) -> dict:
    """FB15 round 3: the host builds the add-on link from its hub LINK address (the host-to-hub port), which a browser
    cannot open. The page the person signs in to is the hub's web console: its origin is what the hub's `rail` answer
    names (the same one the hub switch uses). Only the origin is replaced; the path and fragment stay the host's. No
    console origin known: the host's link is kept."""
    from .switch import browser_origin, rail_view  # lazy: switch imports reads, which imports this module's siblings

    try:
        raw = link.call("hub.switch", {"kind": "rail", "origin": browser_origin(request)})
        hub = rail_view(raw)["hub"]
        origin = hub["origin"]
        other = raw["hub"].get("network_origin") if isinstance(raw.get("hub"), dict) else None
    except Exception:  # noqa: BLE001 - advisory: the host's own link still stands
        return info
    if not isinstance(origin, str) or not CONSOLE_ORIGIN.fullmatch(origin):
        return info
    rest = info["url"].split("/#/", 1)[1]
    out = {**info, "url": f"{origin}/#/{rest}"}
    if isinstance(other, str) and other != origin and CONSOLE_ORIGIN.fullmatch(other):
        out["other_url"] = f"{other}/#/{rest}"  # the same page for another device
    return out


def panel(got: dict) -> dict:
    """Of the host's answer only the closed fields, each checked; anything off is the dashboard's own `internal`."""
    if got.get("kind") == "addon":
        return addon_panel(got)
    if got.get("kind") == "settings":
        return settings_panel(got)
    s = got.get("summary")
    short, url = got.get("short_hash"), got.get("url")
    if (
        got.get("mode") not in HUB_ONLY
        or not isinstance(s, dict)
        or not (short is None or (isinstance(short, str) and SHORT.fullmatch(short)))
        or not (isinstance(url, str) and URL.fullmatch(url))
        or not isinstance(s.get("title"), str)
    ):
        raise fail("internal", exit=1)
    keep = ("kind", "ticket", "gate", "gate_gen", "question", "status")
    return {
        "mode": got["mode"],
        "summary": {**{k: s.get(k) for k in keep}, "title": s["title"][:200]},
        "short_hash": short,
        "url": url,
    }


#: FB15: the only actions the hub approval route prepares (an add-on turned on, shipped or from a marketplace)
HubApproval = Annotated[AddonGrant | MarketGrant, Field(discriminator="action")]
#: the request states in which the hub may still answer it
OPEN = ("prepared", "bound")


def register(app: FastAPI, gateway: Gateway):
    #: the requests the hub approval route prepared, per (person, workspace), and its own per-session quota: the poll
    #: answers only for these (a click request id, or one prepared elsewhere, looks like one that never existed)
    approvals = Handoffs()

    def approval_owner(ws: str, request: Request):
        """A full session (never a switch or read-mostly one), the SESSION's person holds the role owner, and the host
        says the workspace is joined to a hub in full mode. The host checks the role and the mode again."""
        from .cosmetic_routes import full_session
        from .hub import status_view

        token = request.state.token
        if app.state.auth.login_kind(token) == "switch" or not full_session(app.state.auth, token):
            raise fail("session.readonly", "Open a full session to ask for an approval in the hub.", exit=3)
        link = gateway.enabled(ws)
        if not all(link.supports(op, "hub/1") for op in ("hub.status", "hub.handoff")):
            raise fail("click.contract", "Update orch-core: it cannot hand an approval to the hub yet.", exit=9)
        person = app.state.person_of(ws)
        role = next((m.get("role") for m in link.call("member.list")["members"] if m.get("person") == person), None)
        if role != "owner":
            raise fail("role.denied", "Only the workspace owner asks for an approval in the hub.", exit=3)
        hub = status_view(link.call("hub.status"))
        if not (hub["joined"] and hub["mode"] == "full"):
            raise fail("hub.not_joined", "This workspace is not joined to a hub in full mode.", exit=3)
        return link, person, token

    @app.post("/api/w/{ws}/hub/approve/prepare")
    def hub_approve_prepare(body: HubApproval, request: Request, ws: str = Depends(workspace)):
        """FB15: turn an add-on on through the hub. The host prepares the grant as an unbound request (`human.prepare
        {handoff: true}`, no signer: core marks it for the hub), and its `hub.handoff` read names it and builds the one
        link. Nothing is signed here; the hub's channel answers the request."""
        link, person, token = approval_owner(ws, request)
        with approvals.lock:
            approvals.reserve(token)
        try:
            planned = plan(gateway, ws, body)
            if planned.op != "addon.grant" or planned.sequence is not None:
                raise fail("internal", exit=1)
            # its own intent: the same add-on prepared for one click is another request (another binding at the host)
            planned = replace(planned, base={"via": "hub", "base": planned.base})

            def call(idem: str):
                sent = {"op": planned.op, "args": planned.args, "handoff": True}
                return app.state.human(ws, "human.prepare", sent, idem=idem, files=planned.files)

            prepared = send_prepare(planned, ws, body.retry, call, lambda: app.state.human(ws, "human.pending", {}))
            got = adapters.handoff.request(
                prepared, ws, expect_op="addon.grant", expect_events=expected(body, person), expect_material=None
            )
            if got["state"] == "submitted":
                raise read_done(app.state.human, ws, got["id"])
            if got["state"] not in OPEN:
                raise fail("human.request_state", "The request ended before it was approved. Try again.", exit=5)
            shown = panel(link.call("hub.handoff", {"kind": "addon", "request": got["id"]}))
        except BaseException:
            approvals.release(token)
            raise
        approvals.own(person, ws, got["id"], TTL)
        approvals.commit(token, ws, got["id"], until=approvals.clock() + TTL, kind="hub")
        if shown.get("kind") != "addon":
            raise fail("internal", exit=1)
        return {"id": got["id"], "state": got["state"], "expires_at": got["expires_at"]} | with_console_origin(
            shown, link, request
        )

    @app.get("/api/w/{ws}/hub/approve/{request_id}")
    def hub_approve_state(request_id: RequestId, request: Request, ws: str = Depends(workspace)):
        """Where a request this route prepared is now (host `human.pending`), so the panel follows the hub's answer."""
        if not approvals.owns(app.state.person_of(ws), ws, request_id):
            raise fail("not_found", exit=2)
        rows = app.state.human(ws, "human.pending", {"request_id": request_id})["requests"]
        if len(rows) != 1 or rows[0].get("id") != request_id:
            raise fail("not_found", exit=2)
        if rows[0].get("state") not in OPEN:
            approvals.settle_all(request_id)
        return adapters.handoff.state(rows[0])

    @app.post("/api/w/{ws}/hubsign")
    def hubsign_read(body: HubSign, request: Request, ws: str = Depends(workspace)):
        """A read: what to show for an approval that the hub's channel signs. Nothing is prepared, signed or logged."""
        link = gateway.enabled(ws)
        if not link.supports("hub.handoff", "hub/1"):
            raise fail("click.contract", "Update orch-core: it cannot hand an approval to the hub yet.", exit=9)
        if body.kind == "settings":
            if body.set is None or body.request is not None or body.ticket is not None:
                raise fail("invalid.input", "A settings change names the settings, nothing else.", exit=5)
        elif (
            body.set is not None
            or (body.kind == "addon") != (body.request is not None)
            or (body.kind == "addon") == (body.ticket is not None)
        ):
            raise fail("invalid.input", "An add-on names its request; a ticket approval names its ticket.", exit=5)
        shown = panel(link.call("hub.handoff", body.model_dump(exclude_none=True)))
        return with_console_origin(shown, link, request) if shown.get("kind") in ("addon", "settings") else shown
