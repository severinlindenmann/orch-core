"""Remote access, the loopback page (R2 T4, orch-core #1590; design note r2-host-design.md, spec D84-D93).

| Method | Path                                   | Host op              | Notes                                    |
|--------|----------------------------------------|----------------------|------------------------------------------|
| GET    | /api/w/{ws}/remote                     | remote.status, .pending, .sessions | the closed view of Settings |
| POST   | /api/w/{ws}/remote/off                 | remote.off           | turning off: one click, no signature     |
| POST   | /api/w/{ws}/remote/code                | remote.code          | a one-time code, shown once              |
| POST   | /api/w/{ws}/remote/devices/{id}/remove | remote.device_remove | removing: one click, no signature        |
| POST   | /api/w/{ws}/remote/sessions/{sid}/end  | remote.session_end   | End: one click, on this computer only    |
| POST   | /api/w/{ws}/remote/presence/nonce      | signer presence_nonce| inside a stream only (R3, D88)           |
| POST   | /api/w/{ws}/remote/presence            | signer presence      | inside a stream only (R3, D88)           |

`remote/off` is also answered inside a stream (an owner's own remote session turns remote access off, D84): then the
host takes the device and person from its session table (`remote.off {stream_id}`), never from the request.

Turning remote access ON and confirming a device are NOT here: they are person ops with the owner proof, signed by click
through `click/prepare` (`signing.RemoteSet`, `signing.RemoteDeviceConfirm`, the backup key sealed to the signer).

Who: a FULL session opened ON THIS COMPUTER (``stepup.LOCAL``), and the session's person must be an owner. Every route
is also REFUSED on the remote listener (`routegate.TABLE`), so a remote session never reaches them; this check is the
second wall (the host's column P and owner check is the third). Nothing here holds a key, a code or a secret: the code
is returned once and not logged."""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import reads
from .adapters.addons import display
from .cosmetic_routes import full_session, refused
from .errors import HostError
from .gateway import Gateway
from .market_routes import new_idem
from .remote_identity import REMOTE_STREAM
from .stepup import LOCAL

log = logging.getLogger("orch_dashboard.security")

LEVELS = ("view", "work", "terminals")
DEVICE_ID = re.compile(r"^rd_[0-9a-f]{32}$")
STREAM_ID = re.compile(r"^[0-9a-f]{32}$")
PENDING_ID = re.compile(r"^rp_[0-9a-f]{32}$")
MAX_DEVICES = 64
MAX_SESSIONS = 64


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


DeviceId = Annotated[str, StringConstraints(pattern=DEVICE_ID.pattern)]
StreamId = Annotated[str, StringConstraints(pattern=STREAM_ID.pattern)]


class Presence(BaseModel):
    """A device's presence signature over a batch of digests (D88); the signer checks every field."""

    model_config = ConfigDict(extra="forbid", strict=True)
    nonce: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    digests: Annotated[list[Annotated[str, StringConstraints(min_length=71, max_length=71)]], Field(min_length=1, max_length=16)]
    assertion: dict[str, Any]


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
            {
                "device": names.get(r.get("device_id"), "A device"),
                "device_id": r.get("device_id"),
                "stream_id": r["stream_id"],
                "level": r["level"] if r.get("level") in LEVELS else None,
                "since_ms": _int(r.get("since_ms")),
            }
            for r in (open_ if isinstance(open_, list) else [])[:MAX_SESSIONS]
            if isinstance(r, dict)
            and isinstance(r.get("device_id"), str)
            and DEVICE_ID.fullmatch(r["device_id"])
            and isinstance(r.get("stream_id"), str)
            and STREAM_ID.fullmatch(r["stream_id"])
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
        stream = REMOTE_STREAM.get()
        if stream is not None:  # an owner's own remote session: the host knows the device, we name only the stream
            got = gateway.enabled(ws).call("remote.off", {"stream_id": stream}, write=True, idem=new_idem())
            log.warning("remote access turned off from a remote session")
            return {"changed": isinstance(got, dict) and got.get("changed") is True}
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

    @app.post("/api/w/{ws}/remote/sessions/{stream_id}/end")
    def remote_session_end(stream_id: StreamId, _body: Empty, request: Request, ws: str = Depends(reads.workspace)):
        link, person = owner_link(ws, request, "end a session")
        link.call("remote.session_end", {"stream_id": stream_id}, write=True, idem=new_idem())
        log.warning("remote access: a session was ended from the dashboard by %s", person)
        return {"ended": True}

    def in_stream() -> str:
        stream = REMOTE_STREAM.get()
        if stream is None:  # the column refuses these on every listener but a stream; this is the second wall
            raise refused("A presence is given on the remote device.")
        return stream

    @app.post("/api/w/{ws}/remote/presence/nonce")
    def remote_presence_nonce(_body: Empty, ws: str = Depends(reads.workspace)):
        in_stream()
        got = app.state.presence_nonce(ws)  # the signer client stamps the stream itself
        nonce = got.get("nonce") if isinstance(got, dict) else None
        if not isinstance(nonce, str) or not 1 <= len(nonce) <= 64:
            raise HostError({"code": "internal", "message": "The signer gave no nonce.", "retryable": True}, 1)
        return {"nonce": nonce}

    @app.post("/api/w/{ws}/remote/presence")
    def remote_presence(body: Presence, ws: str = Depends(reads.workspace)):
        in_stream()
        got = app.state.presence(ws, body.nonce, body.digests, body.assertion)
        return {"ok": True, "n": got.get("n") if isinstance(got, dict) and type(got.get("n")) is int else None}
