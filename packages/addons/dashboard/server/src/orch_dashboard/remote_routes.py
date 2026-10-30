"""Remote access, the loopback page (R2 T4, orch-core #1590; design note r2-host-design.md, spec D84-D93).

| Method | Path                                   | Host op              | Notes                                    |
|--------|----------------------------------------|----------------------|------------------------------------------|
| GET    | /api/w/{ws}/remote                     | remote.status, .pending, .sessions | the closed view of Settings |
| POST   | /api/w/{ws}/remote/off                 | remote.off           | turning off: one click, no signature     |
| POST   | /api/w/{ws}/remote/code                | remote.code          | a one-time code, shown once              |
| POST   | /api/w/{ws}/remote/devices/{id}/remove | remote.device_remove | removing: one click, no signature        |

Turning remote access ON and confirming a device are NOT here: they are person ops with the owner proof, signed by click
through `click/prepare` (`signing.RemoteSet`, `signing.RemoteDeviceConfirm`, the backup key sealed to the signer).

Who: a FULL session opened ON THIS COMPUTER (``stepup.LOCAL``), and the session's person must be an owner. Every route
is also REFUSED on the remote listener (`routegate.TABLE`), so a remote session never reaches them; this check is the
second wall (the host's column P and owner check is the third). Nothing here holds a key, a code or a secret: the code
is returned once and not logged."""

from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

from . import reads
from .adapters.addons import display
from .cosmetic_routes import full_session, refused
from .errors import HostError
from .gateway import Gateway
from .market_routes import new_idem
from .stepup import LOCAL

log = logging.getLogger("orch_dashboard.security")

LEVELS = ("view", "work", "terminals")
DEVICE_ID = re.compile(r"^rd_[0-9a-f]{32}$")
PENDING_ID = re.compile(r"^rp_[0-9a-f]{32}$")
MAX_DEVICES = 64
MAX_SESSIONS = 64


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


DeviceId = Annotated[str, StringConstraints(pattern=DEVICE_ID.pattern)]


def _int(v, high=10**15) -> int | None:
    return v if type(v) is int and 0 <= v <= high else None


def _name(v) -> str:
    return (display(v) or "A device")[:80] if isinstance(v, str) else "A device"


def _device(raw) -> dict | None:
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("device_id"), str)
        or not DEVICE_ID.fullmatch(raw["device_id"])
    ):
        return None
    return {
        "device_id": raw["device_id"],
        "name": _name(raw.get("name")),
        "level_cap": raw["level_cap"] if raw.get("level_cap") in LEVELS else "view",
        "added_ms": _int(raw.get("added_ms")),
        "expires_ms": _int(raw.get("expires_ms")),
        "words": _name(raw.get("words")) if isinstance(raw.get("words"), str) else "",
    }


def _pending(raw) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("pending"), str) or not PENDING_ID.fullmatch(raw["pending"]):
        return None
    return {
        "pending": raw["pending"],
        "name": _name(raw.get("name")),
        "words": raw["words"][:120] if isinstance(raw.get("words"), str) else "",
        "expires_ms": _int(raw.get("expires_ms")),
    }


def view(status, pending, sessions) -> dict:
    """Of the host's three reads only the closed fields, each checked. A shape the page cannot trust is "off"."""
    status = status if isinstance(status, dict) else {}
    devices = [d for d in (_device(r) for r in (status.get("devices") or [])[:MAX_DEVICES]) if d]
    names = {d["device_id"]: d["name"] for d in devices}
    rows = pending.get("pending") if isinstance(pending, dict) else None
    open_ = sessions.get("sessions") if isinstance(sessions, dict) else None
    return {
        "on": status.get("on") is True,
        "level": status.get("level") if status.get("level") in LEVELS else None,
        "until_ms": _int(status.get("until_ms")),
        "premises": status.get("premises") if status.get("premises") in ("yes", "no") else None,
        "off_reason": status.get("off_reason") if isinstance(status.get("off_reason"), str) else None,
        "isolated": status.get("isolated") if isinstance(status.get("isolated"), bool) else None,
        "hub_mode": status.get("hub_mode") if status.get("hub_mode") in ("none", "selective", "full") else "none",
        "devices": devices,
        "pending": [p for p in (_pending(r) for r in (rows if isinstance(rows, list) else [])[:8]) if p],
        "sessions": [
            {"device": names.get(r.get("device_id"), "A device"), "device_id": r.get("device_id")}
            for r in (open_ if isinstance(open_, list) else [])[:MAX_SESSIONS]
            if isinstance(r, dict) and isinstance(r.get("device_id"), str) and DEVICE_ID.fullmatch(r["device_id"])
        ],
    }


def register(app: FastAPI, gateway: Gateway):
    def owner_link(ws: str, request: Request, what: str):
        """A full session opened on this computer, whose person is a workspace owner. The host checks again."""
        token = request.state.token
        auth = app.state.auth
        if not (full_session(auth, token) and auth.login_kind(token) == LOCAL):
            raise refused(f"Change remote access on the computer itself to {what}.")
        link = gateway.enabled(ws)
        person = app.state.person_of(ws)
        role = next((m.get("role") for m in link.call("member.list")["members"] if m.get("person") == person), None)
        if role != "owner":
            raise HostError(
                {
                    "code": "role.denied",
                    "message": "Only the workspace owner changes remote access.",
                    "retryable": False,
                },
                3,
            )
        return link, person

    @app.get("/api/w/{ws}/remote")
    def remote_status(request: Request, ws: str = Depends(reads.workspace)):
        link, _ = owner_link(ws, request, "see it")
        return view(link.call("remote.status"), link.call("remote.pending"), link.call("remote.sessions"))

    @app.post("/api/w/{ws}/remote/off")
    def remote_off(_body: Empty, request: Request, ws: str = Depends(reads.workspace)):
        link, person = owner_link(ws, request, "turn it off")
        got = link.call("remote.off", {}, write=True, idem=new_idem())
        log.warning("remote access turned off from the dashboard by %s", person)
        return {"changed": isinstance(got, dict) and got.get("changed") is True}

    @app.post("/api/w/{ws}/remote/code")
    def remote_code(_body: Empty, request: Request, ws: str = Depends(reads.workspace)):
        link, person = owner_link(ws, request, "add a device")
        got = link.call("remote.code", {}, write=True, idem=new_idem())
        code = got.get("code") if isinstance(got, dict) else None
        if not isinstance(code, str) or not 8 <= len(code) <= 2048:
            raise HostError({"code": "internal", "message": "The host gave no code.", "retryable": True}, 1)
        log.warning("remote access: a device code was issued from the dashboard by %s", person)  # never the code
        return {"code": code, "expires_ms": _int(got.get("expires_ms"))}

    @app.post("/api/w/{ws}/remote/devices/{device_id}/remove")
    def remote_device_remove(device_id: DeviceId, _body: Empty, request: Request, ws: str = Depends(reads.workspace)):
        link, person = owner_link(ws, request, "remove a device")
        link.call("remote.device_remove", {"device_id": device_id}, write=True, idem=new_idem())
        log.warning("remote access: a device was removed from the dashboard by %s", person)
        return {"removed": True}
