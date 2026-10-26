"""B10 unsigned phone hints. Only the pinned local person connection may send them."""

import os
from typing import Literal

from fastapi import Depends, FastAPI, Path
from pydantic import BaseModel, ConfigDict, Field

from .errors import fail
from .reads import workspace


class PhoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ref: str = Field(min_length=1, max_length=64)
    gate: Literal["requirements", "plan"]
    gate_gen: int = Field(ge=0, le=2**53 - 1)
    idem: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class PhoneCancel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^ph_[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def person_link(gateway, ws):
    if os.environ.get("ORCH_GRANT") or os.environ.get("ORCH_SESSION"):
        raise fail("op.not_allowed", "Phone requests are only available to a person.", exit=3)
    link = gateway.enabled(ws)
    peer = link.welcome.get("peer", {})
    if peer.get("class") != "person" or not peer.get("person") or peer.get("grant") or peer.get("session"):
        raise fail("op.not_allowed", "Phone requests are only available to a person.", exit=3)
    return link


def register(app: FastAPI, gateway):
    @app.get("/api/w/{ws}/phone/available")
    def available(ws: str = Depends(workspace)):
        link = person_link(gateway, ws)
        supported = all(link.supports(op, "phone/1") for op in ("phone.request", "phone.cancel", "phone.status"))
        if not supported:
            return {"phones": 0}
        person = link.welcome["peer"]["person"]
        members = link.call("member.list")["members"]
        phones = sum(
            1
            for m in members
            if m["person"] == person
            for d in m["devices"]
            if d.get("valid") is True and "decide" in d.get("scopes", []) and set(d["scopes"]) <= {"look", "decide"}
        )
        return {"phones": phones}

    @app.post("/api/w/{ws}/phone/request")
    def request(body: PhoneRequest, ws: str = Depends(workspace)):
        return person_link(gateway, ws).call(
            "phone.request", body.model_dump(exclude={"idem"}), write=True, idem=body.idem
        )

    @app.post("/api/w/{ws}/phone/cancel")
    def cancel(body: PhoneCancel, ws: str = Depends(workspace)):
        return person_link(gateway, ws).call("phone.cancel", {"id": body.id}, write=True, idem="cancel-" + body.id)

    @app.get("/api/w/{ws}/phone/status/{id}")
    def status(id: str = Path(pattern=r"^ph_[0-7][0-9A-HJKMNP-TV-Z]{25}$"), ws: str = Depends(workspace)):
        return person_link(gateway, ws).call("phone.status", {"id": id})
