"""The owner relay client's routes (orch-v2-host 11A.15.10 item 11): device login (``kind: "device"``), ``POST
/v2/accounts``, ``PUT /v2/devices/{d}``, ``GET /v2/devices/{d}`` and the primary's cert-queue routes (D(p)):
``GET /v2/persons/{p}/cert-requests``, ``POST /v2/cert-requests/{id}/challenge`` and ``.../rejection``.

Transport (11A.7, "as in 11A.7, in the owner client"): every call goes through one
:class:`orch.net.http.RelayConn`, the hardened stdlib connection R1 built (``http.client`` and ``ssl`` only;
``ssl.create_default_context()`` or only the named ``ca_file``; ``CERT_REQUIRED``, hostname check, TLS >= 1.2; no
proxy unless named, never for ``http``; no redirect followed; a total deadline per call; bodies read in bounded pieces
against the route's cap). **JUDGEMENT CALL:** the connection is *imported* from ``orch.net.http``, not copied. The
import ban (``tests/relay/test_import_ban.py``) restricts what ``orch.relay`` imports, not who imports it, and the
connection carries ten rounds of review; a second copy would drift. Nothing here is added to ``orch.relay``: the
owner's routes, its signer and its person-key work stay outside it (11A.15.10: "not in ``orch.relay``").

Origin pinning: ``origin`` is checked by :func:`orch.wsproto.wire.check_origin` (``https``, or ``http`` only for
loopback) and is the **only** origin the login signs; it is never derived from a response, a redirect or DNS. A test
passes a separate ``transport`` (a loopback fixture relay) while the signed origin stays the fixtures' one.

Closed error mapping. A call ends in a success object (exact field set, typed and bound to what was sent) or one of:

* :class:`OwnerTransportError` (kind ``connect``, ``tls``, ``deadline``, ``too_large``, ``truncated``,
  ``protocol``, ``request``, ``shutdown``, ``redirect``: R1's kinds);
* :class:`OwnerRefused`: a non-2xx answer; ``code`` is one of the route's documented codes (:data:`ROUTES`) or
  ``None`` (an answer the route does not define: never echoed);
* :class:`OwnerMisbehaving`: a 2xx answer outside the contract (malformed, other fields, a value that contradicts the
  request, a challenge served twice);
* :class:`OwnerRequestError`: refused locally before anything was sent (a body over the relay's cap, a bad id).

No secret reaches a log, an exception or a ``repr``: not the token, the invite, a challenge or a signature. Logs carry
the route template, status and code only.

Signer interfaces (the transport runs against fixtures behind them):

* :class:`DeviceSigner`: ``device_id`` (16 bytes), ``dk_sig_pub`` and ``sign(payload) -> 64 bytes`` over
  ``sig_relay_auth`` bytes only. :class:`orch.custody.passphrase.PrimarySession` (``orch.custody.primary.unlocked``)
  is one; a test passes a vector key.
* the person key is never used here: certificates are built by the caller (:func:`primary_certificate`, or the queue
  in :mod:`orch.relay_owner.queue`) through a ``PersonKeySource``.
"""

from __future__ import annotations

import collections
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from orch import crypto
from orch.identity import certs
from orch.net import http as rhttp
from orch.wsproto import wire

__all__ = [
    "MAX_LISTED_REQUESTS",
    "ROUTES",
    "DeviceSigner",
    "OwnerClient",
    "OwnerError",
    "OwnerMisbehaving",
    "OwnerRefused",
    "OwnerRequestError",
    "OwnerTransportError",
    "Route",
    "primary_certificate",
]

log = logging.getLogger("orch.relay_owner")

TOKEN_LIFETIME_S = 900.0
RELOGIN_MARGIN_S = 120.0
SMALL_CAP = 16 * 1024
LIST_CAP = 1 << 20
SMALL_DEADLINE_S = 30.0
MAX_WAIT_S = 25
MAX_LISTED_REQUESTS = 64
PRIMARY_SCOPES = ("look",)  # 11A.15.1 Q2: the primary's certificate, least privilege
_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,512}")
_INVITE = re.compile(r"[\x21-\x7e]{1,256}")
_SEEN_CHALLENGES = 64
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


# --- errors ---------------------------------------------------------------------------------------------------------


class OwnerError(Exception):
    """Base of every owner-client failure. ``route`` is the route template; messages carry no relay text and no
    secret."""

    transient = False

    def __init__(self, message: str, route: str) -> None:
        super().__init__(message)
        self.route = route


class OwnerTransportError(OwnerError):
    """No relay answer (R1's transport kinds). Transient unless ``kind`` is ``redirect`` or ``request``."""

    def __init__(self, kind: str, route: str) -> None:
        super().__init__(f"relay transport {kind} on {route}", route)
        self.kind = kind
        self.transient = kind not in ("redirect", "request")


class OwnerRefused(OwnerError):
    """A non-2xx answer: ``status``, ``code`` (one of the route's documented codes, else ``None``) and
    ``retry_after`` (integer seconds, capped at 1 h). Transient: 5xx, 429 ``rate_limited``, 503 ``overloaded``."""

    def __init__(self, route: str, status: int, code: str | None, retry_after: int | None) -> None:
        super().__init__(f"relay refused {route}: {status} {code}", route)
        self.status, self.code, self.retry_after = status, code, retry_after
        self.transient = status >= 500 or (status == 429 and code == "rate_limited")


class OwnerMisbehaving(OwnerError):
    """A 2xx answer outside the contract (alarm ``relay_misbehaving``); ``what`` names the rule, never the data."""

    def __init__(self, route: str, what: str) -> None:
        super().__init__(f"relay answer outside the contract on {route}: {what}", route)
        self.what = what


class OwnerRequestError(OwnerError):
    """Refused locally: nothing was sent."""

    def __init__(self, route: str, what: str) -> None:
        super().__init__(f"refused locally on {route}: {what}", route)
        self.what = what


# --- routes ---------------------------------------------------------------------------------------------------------

_COMMON = frozenset({"malformed", "too_large", "rate_limited", "overloaded", "internal"})
_DP = frozenset({"unauthenticated", "expired", "cert_expired", "not_eligible", "revoked", "quota_exceeded"})


@dataclass(frozen=True)
class Route:
    """One route: its template (the log and error name), whether it carries the D(p) token, the exact success field
    set, the refusal codes it documents (the closed set; the relay HTTP fixtures), the response cap, the request cap
    and the total deadline."""

    template: str
    auth: bool
    ok: frozenset[str]
    codes: frozenset[str]
    cap: int = SMALL_CAP
    request_cap: int = SMALL_CAP
    deadline_s: float = SMALL_DEADLINE_S


ROUTES: Mapping[str, Route] = {
    "challenge": Route("POST /v2/session/challenge", False, frozenset({"challenge", "expires_ms"}), _COMMON),
    "login": Route(
        "POST /v2/session/login", False, frozenset({"token", "expires_ms"}),
        _COMMON | {"cert_expired", "cert_invalid", "bad_signature", "expired", "unknown_challenge", "revoked",
                   "quota_exceeded"},
    ),
    "logout": Route("POST /v2/session/logout", True, frozenset(), _COMMON, deadline_s=5.0),
    "account": Route(
        "POST /v2/accounts", False, frozenset({"person_id"}),
        _COMMON | {"cert_invalid", "wrong_suite", "bad_signature", "expired", "unknown_challenge", "not_eligible",
                   "revoked", "account_exists"},
    ),
    "device_put": Route(
        "PUT /v2/devices/{device}", False, frozenset({"device_id"}),
        _COMMON | {"cert_expired", "cert_invalid", "wrong_suite", "other_person", "revoked", "cert_conflict"},
    ),
    "device_get": Route("GET /v2/devices/{device}", True, frozenset({"cert"}), _COMMON | _DP | {"not_found"}),
    "cert_requests": Route(
        "GET /v2/persons/{person}/cert-requests", True, frozenset({"requests"}), _COMMON | _DP, cap=LIST_CAP
    ),
    "cert_challenge": Route(
        "POST /v2/cert-requests/{id}/challenge", True, frozenset(),
        _COMMON | _DP | {"wrong_suite", "bad_signature", "not_for_this_device", "other_person", "not_found",
                         "already_signed", "other_workspace", "replayed"},
    ),
    "cert_rejection": Route(
        "POST /v2/cert-requests/{id}/rejection", True, frozenset(),
        _COMMON | _DP | {"wrong_suite", "bad_signature", "not_for_this_device", "other_person", "not_found",
                         "already_signed", "other_workspace", "replayed"},
    ),
}  # fmt: skip


# --- the signer interface -------------------------------------------------------------------------------------------


@runtime_checkable
class DeviceSigner(Protocol):
    """The primary's relay-login key: ``sign`` signs ``sig_relay_auth`` bytes and nothing else (custody's
    ``PRIMARY_SIG_LABELS``). :class:`orch.custody.passphrase.PrimarySession` satisfies it."""

    device_id: bytes
    dk_sig_pub: bytes

    def sign(self, payload: bytes) -> bytes: ...


def _hex16(value: object, what: str, route: str) -> str:
    try:
        return crypto.hex_id(crypto.unhex(value, 16))
    except crypto.EncodingError:
        raise OwnerRequestError(route, what) from None


# --- the client -----------------------------------------------------------------------------------------------------


class OwnerClient:
    """One owner (person-path) client against one relay origin, used by one command (one thread at a time; the login
    is single-flight anyway).

    ``origin`` is the stored relay origin, the one the login signs. ``signer`` is the primary's device key
    (:class:`DeviceSigner`). ``transport`` defaults to a :class:`orch.net.http.RelayConn` to ``origin`` with
    ``ca_file``/``proxy``; tests pass a loopback connection. The token lives in memory only."""

    def __init__(
        self,
        origin: str,
        signer: DeviceSigner,
        *,
        transport: rhttp.Transport | None = None,
        ca_file: str | None = None,
        proxy: str | None = None,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        try:
            self._origin = wire.check_origin(origin)
        except wire.WireError:
            raise OwnerRequestError("origin", "origin") from None
        dev, pub = getattr(signer, "device_id", None), getattr(signer, "dk_sig_pub", None)
        try:
            ok = type(dev) is bytes and len(dev) == 16 and crypto.device_id(crypto.validate_public_key(pub)) == dev
        except crypto.CryptoError:
            ok = False
        if not ok:
            raise OwnerRequestError("signer", "the signer's device id is not of its public key")
        self._signer = signer
        self._device = dev.hex()
        if transport is None:
            try:
                transport = rhttp.RelayConn(self._origin, ca_file=ca_file, proxy=proxy)
            except rhttp.TransportError as e:
                raise OwnerTransportError(e.kind, "origin") from None
        self._t = transport
        self._mono = mono
        self._lock = threading.Lock()
        self._token: str | None = None
        self._relogin_at = 0.0
        self._seen: collections.deque[bytes] = collections.deque(maxlen=_SEEN_CHALLENGES)

    def __repr__(self) -> str:
        return f"OwnerClient({self._origin}, device={self._device}, logged_in={self._token is not None})"

    @property
    def origin(self) -> str:
        return self._origin

    @property
    def device_id(self) -> str:
        """The primary's device id, 32 lower-case hex."""
        return self._device

    @property
    def logged_in(self) -> bool:
        return self._token is not None

    def close(self) -> None:
        """Forget the token and close the connection (no logout; see :meth:`logout`)."""
        self._token = None
        close = getattr(self._t, "close", None)
        if close is not None:
            close()

    def shutdown(self) -> None:
        """End a blocked call from another thread (11A.7 shutdown)."""
        self._t.shutdown()

    # -- the call primitive -------------------------------------------------------------------------------------------

    def _call(
        self, name: str, method: str, path: str, *, body: Any = None, deadline_s: float | None = None
    ) -> dict[str, Any]:
        route = ROUTES[name]
        data = None
        if body is not None:
            try:
                data = wire.dumps_body(body, cap=route.request_cap)
            except wire.WireError as e:
                raise OwnerRequestError(route.template, e.code) from None
        deadline = route.deadline_s if deadline_s is None else deadline_s
        resent = False
        while True:
            headers: dict[str, str] = {}
            if data is not None:
                headers["Content-Type"] = "application/json"
            token = None
            if route.auth:
                token = self._ensure_login()
                headers["Authorization"] = f"Bearer {token}"
            try:
                r = self._t.request(
                    method, path, route=route.template, headers=headers, body=data, deadline_s=deadline, cap=route.cap
                )
            except rhttp.TransportError as e:
                raise OwnerTransportError(e.kind, route.template) from None
            if 200 <= r.status < 300:
                try:
                    obj = wire.loads_body(r.body, cap=route.cap)
                except wire.WireError:
                    raise OwnerMisbehaving(route.template, "malformed success body") from None
                if type(obj) is not dict or set(obj) != route.ok:
                    raise OwnerMisbehaving(route.template, "success field set")
                return obj
            err = self._refusal(route, r)
            log.info("relay %s refused status=%d code=%s", route.template, err.status, err.code)
            if route.auth and not resent and err.status == 401 and err.code in ("unauthenticated", "expired"):
                resent = True  # once: log in again and resend the identical bytes
                self._relogin(token)
                continue
            raise err

    @staticmethod
    def _refusal(route: Route, r: rhttp.Response) -> OwnerRefused:
        code = None
        try:
            obj = wire.loads_body(r.body, cap=SMALL_CAP)
            c = obj.get("error") if type(obj) is dict else None
            code = c if type(c) is str and _CODE.fullmatch(c) and c in route.codes else None
        except wire.WireError:
            pass
        ra = r.headers.get("retry-after")
        retry_after = (
            min(int(ra), 3600) if type(ra) is str and ra.isascii() and ra.isdigit() and len(ra) <= 10 else None
        )
        return OwnerRefused(route.template, r.status, code, retry_after)

    # -- session ------------------------------------------------------------------------------------------------------

    def challenge(self) -> bytes:
        """A fresh login challenge (32 bytes). A challenge this client already signed is never signed again: a relay
        that serves one twice is misbehaving."""
        route = ROUTES["challenge"].template
        obj = self._call("challenge", "POST", "/v2/session/challenge", body={})
        try:
            ch = wire.b64_field(obj["challenge"], max_len=wire.CHALLENGE_LEN, exact=wire.CHALLENGE_LEN)
        except wire.WireError:
            raise OwnerMisbehaving(route, "challenge") from None
        if type(obj["expires_ms"]) is not int or obj["expires_ms"] < 0:  # shape only: relay time is never trusted
            raise OwnerMisbehaving(route, "expires_ms")
        if any(wire.ct_equal(ch, s) for s in self._seen):
            raise OwnerMisbehaving(route, "a challenge this client already signed")
        self._seen.append(ch)
        return ch

    def _proof(self, ch: bytes) -> str:
        """The device's signature over ``auth_bytes(origin, challenge, "device", id)``, checked under its own key
        before it leaves (a wrong key fails here, not at the relay)."""
        msg = wire.auth_bytes(self._origin, ch, "device", self._device)
        sig = self._signer.sign(msg)
        if type(sig) is not bytes or not crypto.verify(self._signer.dk_sig_pub, sig, msg):
            raise OwnerRequestError("signer", "the device signer returned a bad signature")
        return crypto.b64u(sig)

    def login(self) -> None:
        """Log in now as the primary device (``kind: "device"``)."""
        with self._lock:
            self._login()

    def _ensure_login(self) -> str:
        with self._lock:
            if self._token is None or self._mono() >= self._relogin_at:
                self._login()
            assert self._token is not None
            return self._token

    def _relogin(self, stale: str | None) -> None:
        with self._lock:
            if self._token is None or self._token == stale:
                self._token = None
                self._login()

    def _login(self) -> None:
        t0 = self._mono()
        ch = self.challenge()
        body = {"challenge": crypto.b64u(ch), "kind": "device", "id": self._device, "sig": self._proof(ch)}
        obj = self._call("login", "POST", "/v2/session/login", body=body)
        token = obj["token"]
        if type(token) is not str or not _TOKEN.fullmatch(token):
            raise OwnerMisbehaving(ROUTES["login"].template, "token")
        if type(obj["expires_ms"]) is not int or obj["expires_ms"] < 0:
            raise OwnerMisbehaving(ROUTES["login"].template, "expires_ms")
        self._token = token
        self._relogin_at = t0 + TOKEN_LIFETIME_S - RELOGIN_MARGIN_S
        log.info("relay owner login ok device=%s", self._device)

    def logout(self) -> None:
        """Best effort; the token is forgotten whatever the answer."""
        token, self._token = self._token, None
        if token is None:
            return
        try:
            self._t.request(
                "POST", "/v2/session/logout", route=ROUTES["logout"].template,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, body=b"{}",
                deadline_s=ROUTES["logout"].deadline_s, cap=SMALL_CAP,
            )  # fmt: skip
        except Exception:  # noqa: BLE001 - best effort by contract (11A.7 shutdown)
            log.info("relay owner logout failed (ignored)")

    # -- account and devices ------------------------------------------------------------------------------------------

    def create_account(self, invite: str, pk_pub: bytes, device_cert: dict[str, Any]) -> str:
        """``POST /v2/accounts {invite, pk_pub, device_cert, challenge, sig}``: the operator's single-use invite, the
        person key and the primary's PK-signed certificate, proved by the primary's device key over a fresh challenge
        (``auth_bytes(origin, challenge, "device", id)``). The certificate must be this device's and verify under
        ``pk_pub`` (checked before anything is sent). Returns the person id the relay created (it must be
        ``person_id(pk_pub)``). The invite is a secret: never logged or shown."""
        route = ROUTES["account"].template
        if type(invite) is not str or not _INVITE.fullmatch(invite):
            raise OwnerRequestError(route, "invite")
        pk = self._check_own_cert(device_cert, pk_pub, route)
        ch = self.challenge()
        body = {
            "invite": invite, "pk_pub": crypto.b64u(pk), "device_cert": device_cert,
            "challenge": crypto.b64u(ch), "sig": self._proof(ch),
        }  # fmt: skip
        obj = self._call("account", "POST", "/v2/accounts", body=body)
        want = crypto.person_id(pk).hex()
        if type(obj["person_id"]) is not str or not wire.ct_equal(obj["person_id"].encode(), want.encode()):
            raise OwnerMisbehaving(route, "person_id is not the person key's")
        return want

    def _check_own_cert(self, cert: object, pk_pub: object, route: str) -> bytes:
        try:
            pk = crypto.validate_public_key(pk_pub) if type(pk_pub) is bytes else None
        except crypto.CryptoError:
            pk = None
        if pk is None:
            raise OwnerRequestError(route, "pk_pub")
        try:
            o = certs.check_cert(cert, pk)
        except Exception:  # noqa: BLE001 - Refused or a shape error: never sent
            raise OwnerRequestError(route, "device_cert does not verify under pk_pub") from None
        if o["device_id"] != self._device:
            raise OwnerRequestError(route, "device_cert is not this device's")
        return pk

    def register_device(self, cert: dict[str, Any], pk_pub: bytes) -> str:
        """``PUT /v2/devices/{device} {cert}`` (no session: the PK-signed certificate is the authority). Used for the
        primary's own certificate on a second relay registration and, by the queue, for a certificate it signed for a
        new device. The certificate is checked under ``pk_pub`` before it is sent; the answer must name its device.
        Idempotent: an identical certificate is a free 200; another one for the same id is 409 ``cert_conflict``."""
        route = ROUTES["device_put"].template
        try:
            pk = crypto.validate_public_key(pk_pub) if type(pk_pub) is bytes else None
            o = certs.check_cert(cert, pk) if pk is not None else None
        except Exception:  # noqa: BLE001
            o = None
        if o is None:
            raise OwnerRequestError(route, "cert does not verify under pk_pub")
        device = o["device_id"]
        obj = self._call("device_put", "PUT", f"/v2/devices/{device}", body={"cert": cert})
        if obj["device_id"] != device:
            raise OwnerMisbehaving(route, "device_id is not the certificate's")
        return device

    def get_device(self, device_id: str, pk_pub: bytes) -> dict[str, Any]:
        """``GET /v2/devices/{device}`` (D(p)): the stored certificate, verified here under ``pk_pub`` (never trusted
        because the relay served it) and bound to ``device_id``."""
        route = ROUTES["device_get"].template
        device = _hex16(device_id, "device_id", route)
        obj = self._call("device_get", "GET", f"/v2/devices/{device}")
        try:
            o = certs.check_cert(obj["cert"], pk_pub)
        except Exception:  # noqa: BLE001
            raise OwnerMisbehaving(route, "cert does not verify under the pinned person key") from None
        if o["device_id"] != device:
            raise OwnerMisbehaving(route, "cert is another device's")
        return obj["cert"]

    # -- the cert queue (D(p)) ----------------------------------------------------------------------------------------

    def cert_requests(self, person_id: str, *, wait: int | None = None) -> list[dict[str, Any]]:
        """``GET /v2/persons/{person}/cert-requests[?wait=N]``: the open requests addressed to this primary, as the
        relay serves them (``{o, sig}`` objects, at most :data:`MAX_LISTED_REQUESTS`, no duplicate ``request_id``).
        Nothing in them is trusted: the queue runs ``check_cert_request`` on each."""
        r = ROUTES["cert_requests"]
        person = _hex16(person_id, "person_id", r.template)
        path = f"/v2/persons/{person}/cert-requests"
        deadline = None
        if wait is not None:
            if type(wait) is not int or not 0 <= wait <= MAX_WAIT_S:
                raise OwnerRequestError(r.template, "wait")
            path += f"?wait={wait}"
            deadline = wait + 15.0
        obj = self._call("cert_requests", "GET", path, deadline_s=deadline)
        reqs = obj["requests"]
        if type(reqs) is not list or len(reqs) > MAX_LISTED_REQUESTS:
            raise OwnerMisbehaving(r.template, "requests")
        ids: set[str] = set()
        for x in reqs:
            if type(x) is not dict or set(x) != {"o", "sig"} or type(x["o"]) is not dict:
                raise OwnerMisbehaving(r.template, "a request is not an {o, sig} object")
            rid = x["o"].get("request_id")
            if type(rid) is not str or rid in ids:
                raise OwnerMisbehaving(r.template, "a request id is missing or listed twice")
            ids.add(rid)
        return reqs

    def post_challenge(self, request_id: str, challenge: dict[str, Any]) -> None:
        """``POST /v2/cert-requests/{id}/challenge {challenge}``: the exact stored bytes (an identical resend is a free
        200)."""
        rid = _hex16(request_id, "request_id", ROUTES["cert_challenge"].template)
        self._call("cert_challenge", "POST", f"/v2/cert-requests/{rid}/challenge", body={"challenge": challenge})

    def post_rejection(self, request_id: str, rejection: dict[str, Any]) -> None:
        """``POST /v2/cert-requests/{id}/rejection {rejection}``: the exact stored bytes."""
        rid = _hex16(request_id, "request_id", ROUTES["cert_rejection"].template)
        self._call("cert_rejection", "POST", f"/v2/cert-requests/{rid}/rejection", body={"rejection": rejection})


# --- the primary's own certificate ----------------------------------------------------------------------------------


def primary_certificate(pk: Any, signer: DeviceSigner, dk_kx_pub: bytes, *, label: str, created_ms: int) -> dict:
    """The primary's PK-signed certificate (11A.15.1, Q2): ``scopes_max: ["look"]`` (least privilege),
    ``expires_ms: null`` (v1.7), ``label_sealed`` under the label key derived from ``PK``. ``pk`` is an entered
    ``PersonKeySource`` that may sign ``sig_device_cert``. The label is cleaned (``clean_label``) before it is
    sealed."""
    from orch.identity.pairing import clean_label

    if type(label) is not str:
        raise TypeError("label")
    person = pk.person_id
    device_id = signer.device_id
    label_sealed = certs.seal_device_label(pk.derive_label_key(), person, device_id, clean_label(label))
    return certs.make_device_cert(
        pk.pk_pub, pk.signer("sig_device_cert"), dk_sig_pub=signer.dk_sig_pub, dk_kx_pub=dk_kx_pub,
        label_sealed=label_sealed, created_ms=created_ms, expires_ms=None, scopes_max=list(PRIMARY_SCOPES),
    )  # fmt: skip
