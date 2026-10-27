"""Trusted browsers: enrol, list, revoke (run 12; see trusted.py). The key is used only by `POST /auth/switch` (E4).

* ``POST /api/trusted/enrol``   `{jwk}` a key, from a REAL local sign-in only (`Auth.enrol_key`), with the person
                                this computer's user is in each workspace now (`Gateway.people`).
* ``GET  /api/trusted``         the enrolled browsers (id, origin, created, last used); local sign-ins only.
* ``DELETE /api/trusted/{kid}`` revoke one: every session it opened ends.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .errors import step_up_response
from .policy import LOOPBACK


class EnrolBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jwk: dict


def _origin(request: Request) -> str:
    policies = request.app.state.policies
    policy = policies.get(request.scope.get("orch_listener", LOOPBACK)) or policies[LOOPBACK]
    return policy.origin_for(request.headers.get("host", ""))


def register(app: FastAPI, auth, gateway):
    def local_only(request: Request):
        """Listing and revoking need a full local session (a switch one never gets here: security.py closes it)."""
        token = request.state.token
        if auth.login_kind(token) != "local" or auth.listener_of(token) != LOOPBACK:
            return step_up_response(False)
        return None

    @app.post("/api/trusted/enrol")
    def enrol(body: EnrolBody, request: Request):
        kid = auth.enrol_key(request.state.token, body.jwk, _origin(request), gateway.people())
        return JSONResponse({"key_id": kid}) if kid else step_up_response(False)

    @app.get("/api/trusted")
    def listing(request: Request):
        return local_only(request) or JSONResponse(auth.trusted_list())

    @app.delete("/api/trusted/{kid}")
    def revoke(kid: str, request: Request):
        return local_only(request) or JSONResponse({"revoked": auth.revoke_key(kid)})
