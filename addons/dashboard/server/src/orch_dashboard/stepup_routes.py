"""The passkey step-up ceremony and the passkey store's own routes (host spec 0.8.6 10.5; #1198 R1-6).

The browser asks for a step-up BEFORE the big action, with the review's digest (or the action's name where there is no
review), touches its passkey, and returns the assertion; the action itself follows within ten minutes and uses it once:

* ``POST /api/w/{ws}/stepup/options``  workspace actions (One click on or off, rotate, dashboard execution on, a typed
                                       key or shown secret at sign, a hub connect): a challenge for this session.
* ``POST /api/stepup/options``         host actions (adding a device, removing a passkey): the same.
* ``POST /api/stepup/verify``          the assertion. Held for this session only, keyed by the digest it was made for.
* ``GET  /api/passkeys``               the person's passkeys (name, created, last used).
* ``POST /api/passkeys/enroll-link``   "Add a device": a one-time enrollment link. Step-up.
* ``DELETE /api/passkeys/{id}``        remove one (never the last). Step-up.

The challenge is ``stepup_challenge(nonce, digest)`` (R1-2). For the ops the signer runs, the nonce is the signer's
(it draws and keeps it, and checks the assertion itself when the op arrives: core ``orch.click.stepup``); for the
server's own ops (hub connect, the passkey store) the server draws it and its check is the one that counts. These routes
exist only on the remote listener, where a session needs them: a session opened on this computer needs no step-up.
"""

import logging
import secrets
from types import SimpleNamespace
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Path, Request
from orch.click import passkeys as wa
from orch.click.passkey_store import LastPasskey, StoreError
from pydantic import StringConstraints

from . import reads
from .errors import fail, response
from .passkey_routes import ENROLL_TTL, TIMEOUT_MS, Strict, b64
from .policy import REMOTE
from .stepup import (
    SIGNER_ACTIONS,
    VIEW_ACTIONS,
    connect_extra,
    device_digest,
    refuse_step_up,
    removal_digest,
)

log = logging.getLogger("orch_dashboard.passkey")

ViewDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
CredentialId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]*$", min_length=22, max_length=1400)]
#: step-up challenges one session may have open at once: a stolen bearer cannot flush the owner's nonces
OPEN_STEPUPS = 4
#: action -> the signer op its nonce is bound to, and whether the call names a request
SIGNER_OPS = {
    "custody.on": "custody_on",
    "custody.off": "custody_off",
    "rotate": "rotate",
    "runner.on": "runner",
    "sign": "sign",
}
REQUEST_ACTIONS = frozenset({"custody.off", "rotate", "sign"})
RequestId = Annotated[str, StringConstraints(pattern=r"^hr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")]


class WorkspaceOptions(Strict):
    action: Literal["custody.on", "custody.off", "rotate", "runner.on", "sign", "hub.connect"]
    view_digest: ViewDigest | None = None
    request_id: RequestId | None = None  # custody.off, rotate, sign: the request the review belongs to
    hub_id: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None  # hub.connect
    invite_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None  # hub.connect


class HostOptions(Strict):
    action: Literal["device.add", "passkey.remove"]
    credential_id: b64(wa.MAX_CREDENTIAL_ID, wa.MIN_CREDENTIAL_ID) | None = None


class VerifyResponse(Strict):
    clientDataJSON: b64(wa.MAX_CLIENT_DATA)
    authenticatorData: b64(wa.MAX_AUTH_DATA)
    signature: b64(wa.MAX_SIGNATURE)
    userHandle: b64(wa.MAX_USER_HANDLE) | None = None


class VerifyBody(Strict):
    challenge_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{22,64}$")]
    id: b64(wa.MAX_CREDENTIAL_ID, wa.MIN_CREDENTIAL_ID)
    response: VerifyResponse


class EnrollLinkBody(Strict):
    link_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{22,64}$")]  # minted by the options of device.add


def register(app: FastAPI, gateway) -> None:
    auth = app.state.auth
    signer = gateway.click

    def service_of(request: Request, remote_only: bool):
        """The passkey service, or a 404: remote access is off, or (for the ceremony) not the remote listener."""
        service = app.state.passkeys
        if service is None or (remote_only and request.scope.get("orch_listener") != REMOTE):
            raise fail("not_found", exit=2)
        return service

    def issue(request: Request, action: str, scope: str, fields: dict):
        service = service_of(request, remote_only=True)
        wanted = {
            "custody.on": set(),
            "custody.off": {"view_digest", "request_id"},
            "rotate": {"view_digest", "request_id"},
            "runner.on": {"view_digest"},
            "sign": {"view_digest", "request_id"},
            "hub.connect": {"hub_id", "invite_sha256"},
            "device.add": set(),
            "passkey.remove": {"credential_id"},
        }[action]
        given = {k for k, v in fields.items() if v is not None}
        if given != wanted:  # exactly the fields this action has, no more and no fewer
            raise fail("invalid.input", "That does not match this action.", exit=5)
        client = getattr(request.state, "client", None)
        if client is None:
            return response("invalid.input", 400)
        # a stolen bearer must not loop this: it costs a signer round trip and evicts others' nonces
        if (
            not auth.remote_attempt(client)
            or service.ceremonies.open_for("stepup", request.state.token) >= OPEN_STEPUPS
        ):
            return response("rate.limited", 429)
        extra = {}
        if action in VIEW_ACTIONS:
            digest = fields["view_digest"]
        elif action == "custody.on":
            digest = wa.action_digest(action, scope)
        elif action == "hub.connect":
            digest = wa.action_digest(action, scope, connect_extra(fields["hub_id"], fields["invite_sha256"]))
        elif action == "device.add":
            extra["link_id"] = secrets.token_urlsafe(16)  # this touch is for the link asked for with it
            digest = device_digest(extra["link_id"])
        else:
            digest = removal_digest(fields["credential_id"])
        try:
            records = service.store.read()
        except StoreError as exc:
            log.error("passkey store refused: %s", exc)
            raise fail("passkey.invalid", "The passkey store cannot be trusted.", exit=3) from None
        if not records:
            raise fail("passkey.invalid", "No passkey is set up.", exit=3)
        signed = action in SIGNER_ACTIONS
        if signed:
            if not signer.passkey_stepups():
                raise fail("click.contract", "Update orch-core: its signing helper cannot check a passkey yet.", exit=9)
            asked = {"workspace_id": scope, "view_digest": digest, "op": SIGNER_OPS[action]}
            if action in REQUEST_ACTIONS:
                asked["request_id"] = fields["request_id"]
            got = signer.call("stepup", asked)
            try:
                nonce = wa.b64u_decode(got.get("nonce"), wa.CHALLENGE_BYTES) if isinstance(got, dict) else b""
            except wa.PasskeyError:
                nonce = b""
            if len(nonce) != wa.CHALLENGE_BYTES:
                raise fail("internal", "The signing helper answered something unexpected.", exit=9)
        else:
            nonce = wa.new_challenge()
        challenge = wa.stepup_challenge(nonce, digest)
        bound = (request.state.token, action, digest, nonce, signed)
        token, _ = service.ceremonies.start("stepup", bound, challenge=challenge)
        return {
            "challenge_id": token,
            "view_digest": digest,
            **extra,
            "publicKey": {
                "challenge": wa.b64u_encode(challenge),
                "rpId": service.rp_id,
                "timeout": TIMEOUT_MS,
                "userVerification": "required",
                "allowCredentials": [{"type": "public-key", "id": r.credential_id} for r in records],
            },
        }

    @app.post("/api/w/{ws}/stepup/options")
    def workspace_options(body: WorkspaceOptions, request: Request, ws: str = Depends(reads.workspace)):
        return issue(request, body.action, ws, body.model_dump(exclude={"action"}))

    @app.post("/api/stepup/options")
    def host_options(body: HostOptions, request: Request):
        return issue(request, body.action, "-", body.model_dump(exclude={"action"}))

    @app.post("/api/stepup/verify")
    def verify(body: VerifyBody, request: Request):
        service = service_of(request, remote_only=True)

        def refused(why: str):
            log.warning("step-up refused: %s", why)
            return fail("passkey.stepup_required", "That check was not accepted. Try again.", exit=3)

        taken = service.ceremonies.take(body.challenge_id, "stepup")  # spent now, whatever follows
        if taken is None:
            raise refused("challenge")
        challenge, (token, action, digest, nonce, signed) = taken
        if token != request.state.token:
            raise refused("another session")
        try:
            credential_id = wa.b64u_decode(body.id, wa.MAX_CREDENTIAL_ID)
            record = service.store.find(credential_id)
            if record is None:
                raise refused("unknown credential")
            handle = wa.b64u_decode(body.response.userHandle, wa.MAX_USER_HANDLE) if body.response.userHandle else None
            result = wa.verify_assertion(
                credential_id=credential_id,
                client_data_json=wa.b64u_decode(body.response.clientDataJSON, wa.MAX_CLIENT_DATA),
                authenticator_data=wa.b64u_decode(body.response.authenticatorData, wa.MAX_AUTH_DATA),
                signature=wa.b64u_decode(body.response.signature, wa.MAX_SIGNATURE),
                user_handle=handle,
                public_key=record.key_bytes,
                expected_user_handle=record.handle_bytes,
                expected_backup_eligible=record.backup_eligible,
                challenge=challenge,
                rp_id=service.rp_id,
                origin=service.origin,
            )
        except wa.PasskeyError as exc:
            raise refused(exc.reason) from None
        except StoreError as exc:
            log.error("passkey store refused: %s", exc)
            raise refused("store") from None
        try:
            service.store.touch(credential_id, when=service.now(), sign_count=max(result.sign_count, record.sign_count))
        except (StoreError, OSError) as exc:
            log.error("passkey store not updated: %s", exc)  # display only
        proof = None
        if signed:  # what the signer checks again, on its own
            response = {k: v for k, v in body.response.model_dump().items() if v is not None}
            proof = {"nonce": wa.b64u_encode(nonce), "id": body.id, "response": response}
        if not auth.stepup_grant(token, action, digest, proof, record.credential_id):
            raise refused("session")
        return {"ok": True, "view_digest": digest, "valid_for": 600}

    @app.get("/api/passkeys")
    def passkeys(request: Request):
        service = service_of(request, remote_only=False)
        try:
            records = service.store.read()
        except StoreError as exc:
            log.error("passkey store refused: %s", exc)
            raise fail("passkey.invalid", "The passkey store cannot be trusted.", exit=3) from None
        return {
            "passkeys": [
                {"id": r.credential_id, "name": r.name, "created": r.created, "last_used": r.last_used} for r in records
            ]
        }

    @app.post("/api/passkeys/enroll-link")
    def enroll_link(body: EnrollLinkBody, request: Request):
        refuse_step_up(auth, request, "passkeys/enroll-link", body, "-")
        service = service_of(request, remote_only=False)
        code = service.codes.mint()
        log.warning("A device link was made (valid %s minutes)", ENROLL_TTL // 60)  # the code itself is never logged
        return {"code": code, "link": f"{service.origin}/#enroll={code}", "expires_in": ENROLL_TTL}

    @app.delete("/api/passkeys/{credential_id}")
    def remove(request: Request, credential_id: CredentialId):
        refuse_step_up(auth, request, "passkeys/{credential_id}", SimpleNamespace(credential_id=credential_id), "-")
        service = service_of(request, remote_only=False)
        try:
            raw = wa.b64u_decode(credential_id, wa.MAX_CREDENTIAL_ID)
            records = service.store.read()
            target = next((r for r in records if r.credential_id == wa.b64u_encode(raw)), None)
            if target is None:
                raise fail("not_found", exit=2)
            service.store.remove(raw)  # refuses the last one under its lock
            auth.drop_credential(target.credential_id)  # every session and step-up that passkey made, this one too
        except LastPasskey:
            raise fail(
                "invalid.input", "This is the last passkey. Add another first, or turn remote access off.", exit=5
            ) from None
        except wa.PasskeyError:
            raise fail("invalid.input", exit=5) from None
        except StoreError as exc:
            log.error("passkey store refused: %s", exc)
            raise fail("passkey.invalid", "The passkey store cannot be trusted.", exit=3) from None
        log.warning("A passkey was removed: %s", target.name)
        return {"removed": True}
