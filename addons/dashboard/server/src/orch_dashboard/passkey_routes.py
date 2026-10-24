"""Passkey login and enrollment on the remote listener (host spec 0.8.6 10.3; #1198 R1-5).

These four routes answer before login, from the internet, so they stay small: strict models (no extra key, bounded
base64url strings), one rate-limit charge per request (`Auth.remote_attempt`, per source), one single-use 120 s
challenge per ceremony, and ONE answer for every kind of failure (`passkey.invalid` for login, `enroll.expired` for
enrollment): nothing tells a caller whether a credential, a challenge or a code existed. The reason goes to the log.

* ``POST /auth/passkey/options``  a login challenge (discoverable credentials: no allow list).
* ``POST /auth/passkey/login``    the assertion; on success the same bearer as today, kind ``remote``.
* ``POST /auth/enroll/options``   with the one-time code: a registration challenge.
* ``POST /auth/enroll/register``  the registration; on success the passkey is stored, the code is spent, and the
                                  browser is logged in.

On the loopback listener none of them exists (404): the listener decides, never a header.
"""

import hashlib
import hmac
import logging
import secrets
import threading
from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

from . import passkeys as wa
from .auth import Auth
from .errors import response
from .passkey_store import MAX_NAME, Passkey, PasskeyStore, StoreError
from .policy import REMOTE
from .text import hidden

log = logging.getLogger("orch_dashboard.passkey")

CHALLENGE_TTL = 120  # seconds (host spec 10.3)
CEREMONY_CAP = 2048  # outstanding challenges; the oldest is dropped first
ENROLL_TTL = 900  # 15 min
ENROLL_TRIES = 5  # wrong codes in total, then every open link is dead
ENROLL_CODE_BYTES = 24  # 192 bits (the spec asks for at least 128)
TIMEOUT_MS = 120_000
BODY_LIMIT = 16_384  # security.py bounds these pre-login bodies

B64 = r"^[A-Za-z0-9_-]*$"


def b64(max_bytes: int, min_bytes: int = 1):
    return Annotated[
        str, StringConstraints(pattern=B64, min_length=(min_bytes * 4) // 3, max_length=(max_bytes * 4 + 2) // 3)
    ]


Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{22,64}$")]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{16,128}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LoginResponse(Strict):
    clientDataJSON: b64(wa.MAX_CLIENT_DATA)
    authenticatorData: b64(wa.MAX_AUTH_DATA)
    signature: b64(wa.MAX_SIGNATURE)
    userHandle: b64(wa.MAX_USER_HANDLE) | None = None


class LoginBody(Strict):
    challenge_id: Token
    id: b64(wa.MAX_CREDENTIAL_ID, wa.MIN_CREDENTIAL_ID)
    response: LoginResponse


class EnrollOptionsBody(Strict):
    code: Code


class RegisterResponse(Strict):
    clientDataJSON: b64(wa.MAX_CLIENT_DATA)
    attestationObject: b64(wa.MAX_ATTESTATION_OBJECT)


class RegisterBody(Strict):
    challenge_id: Token
    code: Code
    id: b64(wa.MAX_CREDENTIAL_ID, wa.MIN_CREDENTIAL_ID)
    name: Annotated[str, StringConstraints(min_length=1, max_length=MAX_NAME)]
    response: RegisterResponse


def _digest(code: str) -> bytes:
    return hashlib.sha256(b"orch/v2/dash-enroll|" + code.encode()).digest()


class EnrollCodes:
    """Enrollment codes kept as digests, never as text: single use, 15 min, and ``ENROLL_TRIES`` wrong codes in total
    kill every open one. ``mint`` is what `orch remote enable` and "Add a device" (R1-7, R1-6) call."""

    def __init__(self, clock):
        self.clock, self.lock = clock, threading.Lock()
        self.open: dict[bytes, float] = {}
        self.wrong = 0

    def mint(self) -> str:
        code = secrets.token_urlsafe(ENROLL_CODE_BYTES)
        with self.lock:
            now = self.clock()
            self.open = {d: until for d, until in self.open.items() if until > now}
            self.open[_digest(code)] = now + ENROLL_TTL
            self.wrong = 0  # a fresh link starts a fresh count
        return code

    def check(self, code: str) -> bytes | None:
        """The code's digest when it is open and alive, else None (a wrong code is counted; the fifth kills all).
        Compares against every open digest: no early exit on a match."""
        digest = _digest(code)
        with self.lock:
            now = self.clock()
            found = None
            for known, until in self.open.items():
                if hmac.compare_digest(known, digest) and until > now:
                    found = known
            if found is None:
                self.wrong += 1
                if self.wrong >= ENROLL_TRIES:
                    self.open.clear()
                return None
            return found

    def spend(self, digest: bytes) -> bool:
        """Single use: True for exactly the first caller."""
        with self.lock:
            return self.open.pop(digest, None) is not None


class Ceremonies:
    """Single-use challenges, 120 s, bound to their kind (and, for enrollment, to the code). Held in memory only."""

    def __init__(self, clock):
        self.clock, self.lock = clock, threading.Lock()
        self.items: dict[str, tuple[str, bytes, float, object]] = {}

    def start(self, kind: str, bound=None) -> tuple[str, bytes]:
        challenge, token = wa.new_challenge(), secrets.token_urlsafe(16)
        with self.lock:
            now = self.clock()
            self.items = {t: v for t, v in self.items.items() if v[2] > now}
            while len(self.items) >= CEREMONY_CAP:
                del self.items[next(iter(self.items))]
            self.items[token] = (kind, challenge, now + CHALLENGE_TTL, bound)
        return token, challenge

    def take(self, token: str, kind: str):
        """``(challenge, bound)`` once, or None (unknown, used, expired or another kind's). Taking spends it."""
        with self.lock:
            item = self.items.pop(token, None)
            if item is None or item[0] != kind or item[2] <= self.clock():
                return None
            return item[1], item[3]


class Passkeys:
    """The remote login service of one dashboard app."""

    def __init__(self, store: PasskeyStore, rp_id: str, clock):
        self.store, self.rp_id, self.origin = store, rp_id, "https://" + rp_id
        self.codes, self.ceremonies = EnrollCodes(clock), Ceremonies(clock)
        self.clock = clock

    def now(self) -> str:
        return datetime.fromtimestamp(self.clock(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def register(app: FastAPI, auth: Auth, service: Passkeys | None) -> None:
    """``service`` is None when remote access is off: the routes exist (the route table lists them) and answer 404."""

    def gate(request: Request):
        """The client address of a request on the remote listener, or a 404 / 429 answer. Read only when the scope says
        this is the remote listener (never from a header, never on loopback)."""
        if service is None or request.scope.get("orch_listener") != REMOTE:
            return response("not_found", 404)
        client = request.state.client if hasattr(request.state, "client") else None
        if client is None:
            return response("invalid.input", 400)
        if not auth.remote_attempt(client):
            return response("rate.limited", 429)
        return None

    def invalid(reason: str):
        log.warning("passkey login refused: %s", reason)
        return response("passkey.invalid", 401)

    def expired(reason: str):
        log.warning("enrollment refused: %s", reason)
        return response("enroll.expired", 401)

    @app.post("/auth/passkey/options")
    def login_options(request: Request):
        refused = gate(request)
        if refused:
            return refused
        token, challenge = service.ceremonies.start("login")
        return {
            "challenge_id": token,
            "publicKey": {
                "challenge": wa.b64u_encode(challenge),
                "rpId": service.rp_id,
                "timeout": TIMEOUT_MS,
                "userVerification": "required",
                "allowCredentials": [],
            },
        }

    @app.post("/auth/passkey/login")
    def login(body: LoginBody, request: Request):
        refused = gate(request)
        if refused:
            return refused
        taken = service.ceremonies.take(body.challenge_id, "login")
        if taken is None:
            return invalid("challenge")
        challenge, _ = taken
        try:
            credential_id = wa.b64u_decode(body.id, wa.MAX_CREDENTIAL_ID)
            record = service.store.find(credential_id)
            if record is None:
                return invalid("unknown credential")
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
            return invalid(exc.reason)
        except StoreError as exc:
            log.error("passkey store refused: %s", exc)
            return invalid("store")  # fail closed: no login while the store cannot be trusted
        try:
            service.store.touch(credential_id, when=service.now(), sign_count=result.sign_count)
        except (StoreError, OSError) as exc:
            log.error("passkey store not updated: %s", exc)  # the login itself was verified; last-used is display only
        return auth.open_remote()

    @app.post("/auth/enroll/options")
    def enroll_options(body: EnrollOptionsBody, request: Request):
        refused = gate(request)
        if refused:
            return refused
        digest = service.codes.check(body.code)
        if digest is None:
            return expired("code")
        try:
            existing = service.store.read()
        except StoreError as exc:
            log.error("passkey store refused: %s", exc)
            return expired("store")
        handle = existing[0].handle_bytes if existing else secrets.token_bytes(32)
        token, challenge = service.ceremonies.start("enroll", (digest, handle))
        return {
            "challenge_id": token,
            "publicKey": {
                "challenge": wa.b64u_encode(challenge),
                "rp": {"id": service.rp_id, "name": "orch"},
                "user": {"id": wa.b64u_encode(handle), "name": "orch", "displayName": "orch"},
                "pubKeyCredParams": [
                    {"type": "public-key", "alg": wa.ALG_ES256},
                    {"type": "public-key", "alg": wa.ALG_EDDSA},
                ],
                "timeout": TIMEOUT_MS,
                "attestation": "none",
                "authenticatorSelection": {
                    "residentKey": "required",
                    "requireResidentKey": True,
                    "userVerification": "required",
                },
                "excludeCredentials": [{"type": "public-key", "id": r.credential_id} for r in existing],
            },
        }

    @app.post("/auth/enroll/register")
    def enroll_register(body: RegisterBody, request: Request):
        refused = gate(request)
        if refused:
            return refused
        digest = service.codes.check(body.code)
        if digest is None:
            return expired("code")
        taken = service.ceremonies.take(body.challenge_id, "enroll")
        if taken is None:
            return expired("challenge")
        challenge, (bound, handle) = taken
        if not hmac.compare_digest(bound, digest):
            return expired("challenge bound to another code")
        name = body.name.strip()
        if not name or hidden(name) or not name.isprintable():
            return expired("name")
        try:
            credential_id = wa.b64u_decode(body.id, wa.MAX_CREDENTIAL_ID)
            reg = wa.verify_registration(
                credential_id=credential_id,
                client_data_json=wa.b64u_decode(body.response.clientDataJSON, wa.MAX_CLIENT_DATA),
                attestation_object=wa.b64u_decode(body.response.attestationObject, wa.MAX_ATTESTATION_OBJECT),
                challenge=challenge,
                rp_id=service.rp_id,
                origin=service.origin,
            )
        except wa.PasskeyError as exc:
            return expired(exc.reason)
        if not service.codes.spend(digest):  # the first browser that completes a registration wins
            return expired("code spent")
        when = service.now()
        record = Passkey(
            credential_id=wa.b64u_encode(reg.credential_id),
            user_handle=wa.b64u_encode(handle),
            public_key=wa.b64u_encode(reg.public_key),
            alg=reg.alg,
            backup_eligible=reg.backup_eligible,
            name=name,
            created=when,
            last_used=when,
            sign_count=reg.sign_count,
        )
        try:
            service.store.add(record)
        except (StoreError, OSError) as exc:
            log.error("passkey not stored: %s", exc)
            return expired("store")  # the code is spent: IT makes a new link
        log.warning("A passkey was added: %s, %s", name, when)  # the terminal and the log show it (host spec 10.1)
        return auth.open_remote()
