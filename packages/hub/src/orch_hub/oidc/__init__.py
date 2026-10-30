"""Company login (HUB-PE-04, #1402): OpenID Connect authorization code + PKCE, id_token checks.

One job: turn a finished company login into a verified identity ``(provider, tenant-or-issuer, subject)``. It never opens a
session, never signs and never touches a key (the web layer decides what an identity may do). JWT signature checks are
``joserfc``'s; this module only checks the claims, with exact comparisons. The redirect URI comes from configuration, never
from a request. The client secret is handed in by the caller (hub secret store) and is never stored, logged or returned here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

SKEW_S = 120  # clock skew accepted on exp / iat / nbf
TX_TTL_S = 600
JWKS_REFETCH_MIN_S = 60  # an unknown kid causes at most one refetch per this many seconds
ALGS = ["RS256", "ES256"]  # never none, never HS*: the key set holds public keys only, the list is the second fence
FRESH_S = 300  # a "fresh" login means auth_time within this many seconds
_TENANT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DOMAIN = re.compile(r"(?:[a-z0-9-]{1,63}\.)+[a-z]{2,63}")
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"


class OidcError(Exception):
    """``code`` is stable and safe to show; ``detail`` is for the activity log and never carries a token or a secret."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class OidcConfig:
    provider: str  # "entra" | "google"
    client_id: str
    redirect_uri: str  # built from hub configuration, never from a Host header
    tenant: str = ""  # entra: the tenant guid (never common / organizations / consumers)
    domain: str = ""  # google: the Workspace domain (hd)

    def __post_init__(self) -> None:
        bad = OidcError("bad_config")
        if self.provider not in ("entra", "google") or not (0 < len(self.client_id) <= 200):
            raise bad
        r = self.redirect_uri
        local = r.startswith(("http://localhost:", "http://127.0.0.1:"))
        if not (r.startswith("https://") or local) or "#" in r or len(r) > 300:
            raise bad  # Entra and Google take https, or http on localhost only
        if self.provider == "entra" and not _TENANT.fullmatch(self.tenant):
            raise bad
        if self.provider == "google" and not _DOMAIN.fullmatch(self.domain):
            raise bad

    @property
    def issuer(self) -> str:
        return (
            f"https://login.microsoftonline.com/{self.tenant}/v2.0" if self.provider == "entra" else GOOGLE_ISSUERS[0]
        )

    @property
    def discovery_url(self) -> str:
        return self.issuer + "/.well-known/openid-configuration" if self.provider == "entra" else GOOGLE_DISCOVERY

    def issuer_ok(self, iss: object) -> bool:
        return iss == self.issuer or (self.provider == "google" and iss in GOOGLE_ISSUERS)


@dataclass(frozen=True)
class Identity:
    provider: str
    realm: str  # entra: tid, google: hd
    subject: str  # entra: oid, google: sub. The only thing a link is keyed on
    email: str  # shown only, never matched
    name: str
    amr: tuple[str, ...] | None  # None = the IdP sent no amr
    auth_time: int | None


class Http:
    """The two network calls. Tests replace this with the fake IdP; the real one lives in the web layer (5 s timeouts)."""

    def get_json(self, url: str) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    def post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Oidc:
    def __init__(self, cfg: OidcConfig, http: Http, now: Callable[[], float] = time.time) -> None:
        self.cfg, self.http, self._now = cfg, http, now
        self._lock = threading.Lock()
        self._tx: dict[str, dict[str, Any]] = {}  # tx cookie -> {state, nonce, verifier, fresh, exp}; single use
        self._meta: dict[str, Any] | None = None
        self._keys: KeySet | None = None
        self._keys_at = -1e9

    # --- discovery + keys -------------------------------------------------------------------------------------------
    def _discover(self) -> dict[str, Any]:
        if self._meta is None:
            m = self.http.get_json(self.cfg.discovery_url)
            if not isinstance(m, dict) or not self.cfg.issuer_ok(m.get("issuer")):
                raise OidcError("bad_discovery")  # the document must name the issuer we pinned
            for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not (
                    isinstance(m.get(k), str) and m[k].startswith(("https://", "http://localhost", "http://127.0.0.1"))
                ):
                    raise OidcError("bad_discovery")
            self._meta = m
        return self._meta

    def _load_keys(self) -> KeySet:
        try:
            doc = self.http.get_json(self._discover()["jwks_uri"])
            ks = KeySet.import_key_set(doc)
        except OidcError:
            raise
        except Exception:  # network, JSON, bad key: fail closed  # noqa: BLE001 - fail closed
            raise OidcError("jwks_unavailable") from None
        self._keys, self._keys_at = ks, self._now()
        return ks

    def _key_for(self, kid: object) -> KeySet:
        with self._lock:
            ks = self._keys or self._load_keys()
            if isinstance(kid, str) and not any(k.kid == kid for k in ks.keys):
                if self._now() - self._keys_at < JWKS_REFETCH_MIN_S:
                    raise OidcError("unknown_kid")  # one refetch per window, then refuse
                ks = self._load_keys()
                if not any(k.kid == kid for k in ks.keys):
                    raise OidcError("unknown_kid")
            return ks

    # --- start / finish ---------------------------------------------------------------------------------------------
    def start(self, *, fresh: bool = False) -> tuple[str, str]:
        """``(authorization url, tx cookie value)``. ``fresh`` asks the IdP to re-authenticate the person."""
        meta = self._discover()
        now = self._now()
        cookie, state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
        with self._lock:
            self._tx = {k: v for k, v in self._tx.items() if v["exp"] > now}
            if len(self._tx) >= 1000:
                raise OidcError("busy")
            self._tx[cookie] = {
                "state": state,
                "nonce": nonce,
                "verifier": verifier,
                "fresh": fresh,
                "exp": now + TX_TTL_S,
            }
        q = {
            "response_type": "code",
            "client_id": self.cfg.client_id,
            "redirect_uri": self.cfg.redirect_uri,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
        }
        if fresh:
            q.update(prompt="login", max_age="0")
        if self.cfg.provider == "google":
            q["hd"] = self.cfg.domain
        return meta["authorization_endpoint"] + "?" + urlencode(q), cookie

    def finish(self, cookie: str, state: str, code: str, client_secret: str) -> Identity:
        """Check the callback, swap the code, verify the id_token. Raises :class:`OidcError`; never returns a half result."""
        with self._lock:  # single use: the row is gone whatever happens next
            tx = self._tx.pop(cookie, None) if isinstance(cookie, str) else None
        now = self._now()
        if tx is None or tx["exp"] <= now:
            raise OidcError("bad_state")
        if not isinstance(state, str) or not secrets.compare_digest(state.encode(), tx["state"].encode()):
            raise OidcError("bad_state")
        if not isinstance(code, str) or not (0 < len(code) <= 2048):
            raise OidcError("bad_code")
        try:
            tok = self.http.post_form(
                self._discover()["token_endpoint"],
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.cfg.redirect_uri,
                    "client_id": self.cfg.client_id,
                    "client_secret": client_secret,
                    "code_verifier": tx["verifier"],
                },
            )
        except OidcError:
            raise
        except Exception:  # noqa: BLE001 - fail closed
            raise OidcError("token_exchange_failed") from None
        raw = tok.get("id_token") if isinstance(tok, dict) else None
        if not isinstance(raw, str):
            raise OidcError("token_exchange_failed")
        return self.verify(raw, tx["nonce"], fresh=tx["fresh"])

    def verify(self, raw: str, nonce: str, *, fresh: bool = False) -> Identity:
        now = self._now()
        kid = _header_kid(raw)
        ks = self._key_for(kid)
        try:
            tok = jwt.decode(raw, ks, algorithms=ALGS)
        except JoseError, ValueError, KeyError:
            raise OidcError("bad_token") from None
        if tok.header.get("alg") not in ALGS:
            raise OidcError("bad_token")
        c = tok.claims
        cfg = self.cfg
        if not cfg.issuer_ok(c.get("iss")):
            raise OidcError("bad_issuer")
        aud = c.get("aud")
        if type(aud) is not str or aud != cfg.client_id:  # a list (several audiences) is refused
            raise OidcError("bad_audience")
        if "azp" in c and c["azp"] != cfg.client_id:
            raise OidcError("bad_audience")
        for k in ("exp", "iat"):
            if type(c.get(k)) is not int:
                raise OidcError("bad_token")
        if c["exp"] + SKEW_S < now or c["iat"] - SKEW_S > now:
            raise OidcError("expired")
        if "nbf" in c and (type(c["nbf"]) is not int or c["nbf"] - SKEW_S > now):
            raise OidcError("expired")
        n = c.get("nonce")
        if type(n) is not str or not secrets.compare_digest(n.encode(), nonce.encode()):
            raise OidcError("bad_nonce")
        sub = c.get("sub")
        if type(sub) is not str or not (0 < len(sub) <= 255):
            raise OidcError("bad_token")
        at = c.get("auth_time")
        at = at if type(at) is int else None
        if fresh and (at is None or now - at > FRESH_S):
            raise OidcError("not_fresh")
        amr = c.get("amr")
        amr_t = tuple(a for a in amr if type(a) is str) if isinstance(amr, list) else None
        email = c.get("email") if type(c.get("email")) is str else (c.get("preferred_username") or "")
        name = c.get("name") if type(c.get("name")) is str else ""
        if cfg.provider == "entra":
            if c.get("tid") != cfg.tenant:
                raise OidcError("wrong_tenant")
            oid = c.get("oid")
            if type(oid) is not str or not oid:
                raise OidcError("bad_token")
            return Identity("entra", cfg.tenant, oid, str(email)[:200], name[:200], amr_t, at)
        if c.get("hd") != cfg.domain:
            raise OidcError("wrong_domain")
        if c.get("email_verified") is not True:
            raise OidcError("email_unverified")
        return Identity("google", cfg.domain, sub, str(email)[:200], name[:200], amr_t, at)


def _header_kid(raw: str) -> object:
    try:
        h = raw.split(".")[0]
        return json.loads(base64.urlsafe_b64decode(h + "=" * (-len(h) % 4))).get("kid")
    except Exception:  # noqa: BLE001 - fail closed
        raise OidcError("bad_token") from None


def mfa_ok(ident: Identity) -> bool:
    """Admin said the IdP asks a second step. Refuse only when the IdP sent ``amr`` and it lacks ``mfa``."""
    return ident.amr is None or "mfa" in ident.amr
