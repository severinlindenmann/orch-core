"""Trusted browsers: enrol, list, revoke, and the switch session's way up (run 12; see trusted.py).

* ``POST /api/trusted/enrol``   `{jwk}` a browser key, from a REAL local sign-in only (`Auth.enrol_key`).
* ``GET  /api/trusted``         the enrolled browsers (id, origin, created, last used); local sign-ins only.
* ``DELETE /api/trusted/{kid}`` revoke one: every session it upgraded is read-mostly again.
* ``POST /api/auth/challenge``  a switch session asks for the nonce its browser key must sign.
* ``POST /api/auth/upgrade``    `{key_id, signature}` -> a new full (local) session, or 401.
"""

from typing import Annotated

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from .errors import response, step_up_response
from .policy import LOOPBACK


class EnrolBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jwk: dict


class UpgradeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    signature: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{86}$")]


def _origin(request: Request) -> str:
    policies = request.app.state.policies
    policy = policies.get(request.scope.get("orch_listener", LOOPBACK)) or policies[LOOPBACK]
    return policy.origin_for(request.headers["host"])


def register(app: FastAPI, auth):
    def local_only(request: Request):
        """Listing and revoking need a full local session (a switch one never gets here: security.py closes it)."""
        token = request.state.token
        if auth.login_kind(token) != "local" or auth.listener_of(token) != LOOPBACK:
            return step_up_response(False)
        return None

    @app.post("/api/trusted/enrol")
    def enrol(body: EnrolBody, request: Request):
        kid = auth.enrol_key(request.state.token, body.jwk, _origin(request))
        return JSONResponse({"key_id": kid}) if kid else step_up_response(False)

    @app.get("/api/trusted")
    def listing(request: Request):
        return local_only(request) or JSONResponse(auth.trusted_list())

    @app.delete("/api/trusted/{kid}")
    def revoke(kid: str, request: Request):
        return local_only(request) or JSONResponse({"revoked": auth.revoke_key(kid)})

    @app.post("/api/auth/challenge")
    def challenge(request: Request):
        nonce = auth.challenge(request.state.token)
        return JSONResponse({"nonce": nonce}) if nonce else response("session.required", 401)

    @app.post("/api/auth/upgrade")
    def upgrade(body: UpgradeBody, request: Request):
        minted = auth.upgrade(request.state.token, body.key_id, body.signature, _origin(request))
        return JSONResponse(minted) if minted else response("session.required", 401)
