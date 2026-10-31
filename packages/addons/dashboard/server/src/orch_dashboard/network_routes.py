"""The proxy for hub links (R6 #1594, design section 3): the owner's "Network" setting in the hub settings.

| Method | Path                     | Host op          | Notes                                       |
|--------|--------------------------|------------------|---------------------------------------------|
| GET    | /api/w/{ws}/hub/network  | hub.network      | the proxy address, the user, whether a password is kept |
| PUT    | /api/w/{ws}/hub/network  | hub.network_set  | ``{proxy: string | null, user?, password?}``  |

Where the hub link goes is decided on this computer: a FULL session on the loopback listener, the session's person an
owner (the host checks again), never a remote session, a remote-device stream or a switch session (the route table and
``REMOTE_DEVICE`` refuse them too). The password goes to the host and is never in an answer, a log or an error; the
answer is a closed shape. Not a signed event: the host op is an owner click and a pin protects the link whatever the
proxy does."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Body, Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from . import reads
from .cosmetic_routes import full_session, refused
from .errors import HostError, fail
from .gateway import Gateway
from .market_routes import new_idem
from .stepup import LOCAL

log = logging.getLogger("orch.dashboard.network")

PLAIN = "The proxy address must look like http://name:3128 or https://name:3128."


class NetworkBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    proxy: Annotated[str, StringConstraints(max_length=300)] | None
    user: Annotated[str, StringConstraints(max_length=255)] | None = None
    password: Annotated[str, StringConstraints(max_length=1000)] | None = None


def view(raw) -> dict:
    """Of the host's answer only the closed fields."""
    raw = raw if isinstance(raw, dict) else {}
    proxy, user = raw.get("proxy"), raw.get("user")
    return {
        "proxy": proxy if isinstance(proxy, str) and len(proxy) <= 300 else None,
        "user": user if isinstance(user, str) and len(user) <= 255 else None,
        "has_password": raw.get("has_password") is True,
        "broken": raw.get("broken") is True,
    }


def register(app: FastAPI, gateway: Gateway) -> None:
    def owner_link(ws: str, request: Request, what: str):
        token = request.state.token
        if not full_session(app.state.auth, token) or app.state.auth.login_kind(token) != LOCAL:
            raise refused(f"Sign in on this computer to {what}.")
        link = gateway.enabled(ws)
        person = app.state.person_of(ws)
        role = next((m.get("role") for m in link.call("member.list")["members"] if m.get("person") == person), None)
        if role != "owner":
            message = "Only the workspace owner changes the network settings."
            raise HostError({"code": "role.denied", "message": message, "retryable": False}, 3)
        return link, person

    @app.get("/api/w/{ws}/hub/network")
    def network_get(request: Request, ws: str = Depends(reads.workspace)):
        link, _ = owner_link(ws, request, "see the network settings")
        return view(link.call("hub.network"))

    @app.put("/api/w/{ws}/hub/network")
    def network_put(request: Request, raw: dict = Body(...), ws: str = Depends(reads.workspace)):  # noqa: B008
        link, person = owner_link(ws, request, "change the network settings")
        try:
            body = NetworkBody.model_validate(raw).model_dump(exclude_none=False)
        except ValidationError:  # never echo the input (the password may be in it)
            raise fail("invalid.input", PLAIN, exit=1) from None
        args = {k: v for k, v in body.items() if k == "proxy" or v is not None}
        got = link.call("hub.network_set", args, write=True, idem=new_idem())
        log.warning(
            "network settings changed from the dashboard by %s (proxy set=%s)", person, body["proxy"] is not None
        )
        return view(got)
