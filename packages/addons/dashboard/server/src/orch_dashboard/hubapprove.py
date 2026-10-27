"""Hub signing from Mission Control (V3 C11, D78/D81): in a workspace joined to a hub in full mode this server signs
nothing and holds no hub credential. It can only (1) ask the host for the workspace's signing mode and (2) ask the
host's read `hub.handoff` for what the "Approve on your phone or in the hub" panel shows: a summary, the 12-hex short
hash and one link the HOST built from its pinned hub address.

**This module imports no hub code, opens no connection to a hub, and builds no link.** The link is checked against
the host's closed grammar (`URL`) and dropped if it does not match; agent-written text (`summary.title`) is one plain
field the page renders as text, never as a link.
"""

import re
from typing import Annotated, Literal

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict, StringConstraints

from .errors import fail
from .gateway import Gateway
from .reads import Key, workspace

#: modes in which no local key signs ("unknown" fails closed as full)
HUB_ONLY = ("full", "unknown")
MODES = ("standalone", "selective", "full", "unknown")
SHORT = re.compile(r"^[0-9a-f]{4} [0-9a-f]{4} [0-9a-f]{4}$")
#: the host's grammar: https://<host>[:port]/#/w/<workspace>/t/<ticket ULID>[/g/<gate>]
URL = re.compile(
    r"^https://(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9a-fA-F:.]+\])(?::[0-9]{1,5})?/#/w/[0-9a-f]{32}/t/"
    r"[0-7][0-9A-HJKMNP-TV-Z]{25}(?:/g/(?:requirements|plan|code|verify))?$"
)
Qid = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]


class HubSign(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["gate", "verdict", "question", "status"]
    ticket: Key
    gate: Literal["requirements", "plan", "code", "verify"] | None = None
    question: Qid | None = None


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


#: what the hub cannot sign yet: `addon.granted` is not a custodial type (protocol.md, "Not custodial in hv 0")
NOT_IN_HUB = ("addon_grant", "market_grant")


def refuse_if_hub_only(gateway: Gateway, ws: str, action: str | None = None) -> None:
    """`sign.hub_only` before any local signing is prepared: full (or unknown) hub mode takes no click signature."""
    if mode_of(gateway, ws) in HUB_ONLY:
        if action in NOT_IN_HUB:  # run-11 FB15: never point to a hub screen that does not exist
            raise fail(
                "sign.hub_only",
                "This workspace signs through the hub, and the hub cannot approve add-on installs yet. "
                "Leave the hub first to install add-ons here.",
                exit=3,
            )
        raise fail(
            "sign.hub_only",
            "Approvals go through the hub in this workspace: approve on your phone or in the hub.",
            exit=3,
        )


def panel(got: dict) -> dict:
    """Of the host's answer only the closed fields, each checked; anything off is the dashboard's own `internal`."""
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


def register(app: FastAPI, gateway: Gateway):
    @app.post("/api/w/{ws}/hubsign")
    def hubsign_read(body: HubSign, ws: str = Depends(workspace)):
        """A read: what to show for an approval that the hub's channel signs. Nothing is prepared, signed or logged."""
        link = gateway.enabled(ws)
        if not link.supports("hub.handoff", "hub/1"):
            raise fail("click.contract", "Update orch-core: it cannot hand an approval to the hub yet.", exit=9)
        return panel(link.call("hub.handoff", body.model_dump(exclude_none=True)))
