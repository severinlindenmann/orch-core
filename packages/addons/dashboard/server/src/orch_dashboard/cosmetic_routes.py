"""The workspace name and icon (run 13, D6): unsigned, owner only, one click.

| Method | Path                           | Host op                | Notes                              |
|--------|--------------------------------|------------------------|------------------------------------|
| PUT    | /api/w/{ws}/workspace/cosmetic | workspace.cosmetic.set | `if_current` is the compare-and-set |

Who: the browser session's person, and only from a FULL session (`full_session`): a session opened on this computer
(a launch code, or a switch session a trusted browser key upgraded) or a remote one a passkey login opened. A switch
session (read-mostly, from a hub ticket; non-GET routes are default-refused for it too, `security.SWITCH_POST`) and any
other remote session are refused here, explicitly. The call goes over the workspace link, whose peer IS the session's
person (the link is pinned to this computer user's person, `gateway.read_pins`; the host derives `call.viewer` from the
socket, as for every other person write). The page is told no before the write when that person is not the owner; the
host checks again (column P, owner role, the closed icon lists, the plain name).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

from . import reads
from .errors import HostError
from .gateway import Gateway
from .market_routes import new_idem
from .policy import LOOPBACK

Name = Annotated[str, StringConstraints(min_length=1, max_length=80)]


class Pair(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    icon: str
    colour: str


class Current(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    icon: Pair | None


class CosmeticBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Name | None = None
    icon: Pair | None = None
    if_current: Current


def refused(message: str) -> HostError:
    return HostError({"code": "op.not_allowed", "message": message, "retryable": False}, 3)


def full_session(auth, token) -> bool:
    """A full session: ``local`` on the loopback listener and not bound to a switch ticket, or ``remote`` opened by a
    passkey login. Never a switch session, never a remote session without its passkey."""
    if auth.switch_workspace(token) is not None:
        return False
    kind = auth.login_kind(token)
    if kind == "local":
        return auth.listener_of(token) == LOOPBACK
    if kind == "remote-device":  # R3: a stream's own credential; the route column and the host decide the rest
        return auth.listener_of(token) == "remote-device"
    return kind == "remote" and auth.passkey_session(token)


def register(app: FastAPI, gateway: Gateway) -> None:
    @app.put("/api/w/{ws}/workspace/cosmetic")
    def cosmetic(body: CosmeticBody, request: Request, ws: str = Depends(reads.workspace)):
        if not full_session(app.state.auth, request.state.token):
            raise refused("Sign in fully to change the workspace name or icon.")
        link = gateway.enabled(ws)
        person = link.welcome["peer"]["person"]  # the session's person: the host's `call.viewer` for this link
        role = next((m.get("role") for m in link.call("member.list")["members"] if m.get("person") == person), None)
        if role != "owner":
            raise HostError(
                {
                    "code": "role.denied",
                    "message": "Only the workspace owner changes its name and icon.",
                    "retryable": False,
                },
                3,
            )
        args = {"if_current": body.if_current.model_dump()}
        sent = body.model_fields_set
        if "name" in sent and body.name is not None:
            args["name"] = body.name
        if "icon" in sent:
            args["icon"] = body.icon.model_dump() if body.icon else None
        return link.call("workspace.cosmetic.set", args, write=True, idem=new_idem())
