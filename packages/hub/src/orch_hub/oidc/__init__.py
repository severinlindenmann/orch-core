"""Company login (HUB-PE-04, #1402): OpenID Connect authorization code + PKCE, id_token checks.

One job: turn a finished company login into a verified identity ``(provider, tenant-or-issuer, subject)``. It never opens a
session, never signs and never touches a key (the web layer decides what an identity may do). JWT signature checks are
``joserfc``'s; this module only checks the claims, with exact comparisons. The redirect URI comes from configuration, never
from a request. The client secret is handed in by the caller (hub secret store) and is never stored, logged or returned here.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

SKEW_S = 120  # clock skew accepted on exp / iat / nbf
TX_TTL_S = 600
USED_MAX = 50_000
_AAD = b"orch-hub oidc tx v1"
_COOKIE_RX = re.compile(r"[A-Za-z0-9_-]+")
START_BURST, START_EVERY_S = 10, 6.0  # starts per source, one more every START_EVERY_S
FINISH_BURST, FINISH_EVERY_S = 10, 3.0
SOURCES_MAX = 100_000
NEG_S = 5.0  # a failed discovery / first key fetch is not retried for this long
PROVIDER_HOSTS = {
    "entra": frozenset({"login.microsoftonline.com"}),
    "google": frozenset({"accounts.google.com", "oauth2.googleapis.com", "www.googleapis.com"}),
}
COOKIE_MAX = 2048
JWKS_REFETCH_MIN_S = 60  # an unknown kid causes at most one refetch per this many seconds
ALGS = ["RS256", "ES256"]  # never none, never HS*: the key set holds public keys only, the list is the second fence
FRESH_S = 300  # a "fresh" login means auth_time within this many seconds
#: the Microsoft "personal accounts" tenant: it passes the guid check and would let any personal account in
CONSUMERS_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
_TENANT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DOMAIN = re.compile(r"(?:[a-z0-9-]{1,63}\.)+[a-z]{2,63}")
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"


#: what the sign-in page says for the refusals a person can act on (everything else: "Company login did not work.")
PLAIN = {
    "not_allowed": "This company account is not set up for this hub. Ask your hub admin.",
    "failed": "Company login did not work. Try again, or sign in with your password.",
    "rate_limited": "Too many tries. Try again in a minute.",
}


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
        if self.provider == "entra" and (not _TENANT.fullmatch(self.tenant) or self.tenant == CONSUMERS_TENANT):
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


def source_key(source: str) -> str:
    """The bucket for a peer address: IPv6 per /64 (one customer holds a whole /64), anything else as given."""
    try:
        ip = ipaddress.ip_address(source)
    except ValueError:
        return source[:64]
    if ip.version == 6 and ip.ipv4_mapped is not None:  # ::ffff:1.2.3.4 is the IPv4 peer 1.2.3.4
        ip = ip.ipv4_mapped
    return str(ipaddress.ip_network(f"{ip}/64", strict=False)) if ip.version == 6 else str(ip)


class _Limiter:
    """A token bucket per source in an LRU (OrderedDict): O(1) per call, no scan, bounded by ``cap``."""

    def __init__(self, burst: int, every_s: float, cap: int = SOURCES_MAX) -> None:
        self.burst, self.every_s, self.cap = burst, every_s, cap
        self._m: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def allow(self, source: str, now: float) -> bool:  # the caller holds the lock
        if type(source) is not str or not source:
            raise OidcError(
                "bad_config"
            )  # the route always passes the real peer; an empty source is a bug, not a bucket
        k = source_key(source)
        tokens, at = self._m.pop(k, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - at) / self.every_s)
        ok = tokens >= 1.0
        self._m[k] = (tokens - 1.0 if ok else tokens, now)  # most recent at the end
        while len(self._m) > self.cap:
            self._m.popitem(last=False)
        return ok


class Oidc:
    def __init__(
        self,
        cfg: OidcConfig,
        http: Http,
        box_key: bytes,
        now: Callable[[], float] = time.time,
        allowed_hosts: frozenset[str] | None = None,
    ) -> None:
        if type(box_key) is not bytes or len(box_key) != 32:
            raise OidcError("bad_config")
        self.cfg, self.http, self._box, self._now = cfg, http, box_key, now
        self._hosts = allowed_hosts if allowed_hosts is not None else PROVIDER_HOSTS[cfg.provider]
        # a cookie made under another issuer or client id (an earlier config) does not open
        self._aad = b"|".join((_AAD, cfg.issuer.encode(), cfg.client_id.encode()))
        self._lock = threading.Lock()
        self._done: OrderedDict[str, float] = OrderedDict()  # completed logins (nonce -> expiry), oldest first
        self._inflight: set[str] = set()
        self._start_lim = _Limiter(START_BURST, START_EVERY_S)
        self._finish_lim = _Limiter(FINISH_BURST, FINISH_EVERY_S)
        self._meta: dict[str, Any] | None = None
        self._keys: KeySet | None = None
        self._keys_at = -1e9
        self._meta_fail_at = -1e9

    # --- discovery + keys -------------------------------------------------------------------------------------------
    def _discover(self) -> dict[str, Any]:
        """The IdP's metadata. Fetched OUTSIDE the lock; a failure is remembered for NEG_S seconds so an outage costs one
        slow fetch per window, not one per request."""
        with self._lock:
            if self._meta is not None:
                return self._meta
            now = self._now()
            if now - self._meta_fail_at < NEG_S:
                raise OidcError("idp_unreachable")
        try:
            m = self.http.get_json(self.cfg.discovery_url)
            if not isinstance(m, dict) or not self.cfg.issuer_ok(m.get("issuer")):
                raise OidcError("bad_discovery")  # the document must name the issuer we pinned
            for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not self._host_ok(m.get(k)):
                    raise OidcError("bad_discovery")  # only the provider's own hosts, over https
        except OidcError:
            with self._lock:
                self._meta_fail_at = self._now()
            raise
        except Exception:  # noqa: BLE001 - fail closed
            with self._lock:
                self._meta_fail_at = self._now()
            raise OidcError("idp_unreachable") from None
        with self._lock:
            self._meta = m
        return m

    def _host_ok(self, url: object) -> bool:
        if type(url) is not str:
            return False
        u = urlsplit(url)
        return u.scheme == "https" and u.hostname in self._hosts and u.port in (None, 443) and not u.username

    def _fetch_keys(self) -> KeySet:
        try:
            return KeySet.import_key_set(self.http.get_json(self._discover()["jwks_uri"]))
        except OidcError:
            raise
        except Exception:  # noqa: BLE001 - network, JSON, bad key: fail closed
            raise OidcError("jwks_unavailable") from None

    def _key_for(self, kid: object) -> KeySet:
        with self._lock:
            ks, now = self._keys, self._now()
            window = NEG_S if ks is None else JWKS_REFETCH_MIN_S
            unknown = ks is None or (isinstance(kid, str) and not any(k.kid == kid for k in ks.keys))
            need = unknown and now - self._keys_at >= window
            if need:
                self._keys_at = now  # claimed before the fetch: a failure is the short negative cache too
            elif ks is None:
                raise OidcError("jwks_unavailable")
        if need:
            ks = self._fetch_keys()  # outside the lock
            with self._lock:
                self._keys = ks
        assert ks is not None
        if isinstance(kid, str) and not any(k.kid == kid for k in ks.keys):
            raise OidcError("unknown_kid")
        return ks

    # --- start / finish ---------------------------------------------------------------------------------------------
    def _seal(self, tx: dict[str, Any]) -> str:
        n = secrets.token_bytes(12)
        ct = AESGCM(self._box).encrypt(n, json.dumps(tx).encode(), self._aad)
        return _b64(n + ct)

    def _open(self, cookie: object) -> dict[str, Any] | None:
        """The login behind a cookie, or None. Strict: only the url-safe alphabet, no padding, and re-encoding the decoded
        bytes must give the input back exactly, so one login has exactly one string form."""
        if type(cookie) is not str or not (0 < len(cookie) <= COOKIE_MAX) or not _COOKIE_RX.fullmatch(cookie):
            return None
        try:
            raw = base64.b64decode(cookie + "=" * (-len(cookie) % 4), altchars=b"-_", validate=True)
            if _b64(raw) != cookie:
                return None
            tx = json.loads(AESGCM(self._box).decrypt(raw[:12], raw[12:], self._aad))
        except Exception:  # noqa: BLE001 - tampered, foreign, truncated: all the same refusal
            return None
        ok = isinstance(tx, dict) and all(
            type(tx.get(k)) is str for k in ("state", "nonce", "verifier", "purpose", "bind")
        )
        return tx if ok and type(tx.get("exp")) in (int, float) and type(tx.get("fresh")) is bool else None

    def start(
        self, *, fresh: bool = False, source: str = "", purpose: str = "signin", bind: str = ""
    ) -> tuple[str, str]:
        """``(authorization url, tx cookie value)``. ``fresh`` asks the IdP to re-authenticate the person. No server state:
        state, nonce and the PKCE verifier live in the sealed cookie (AES-GCM under the hub's key), so unauthenticated
        starts cost nothing to keep and can never block anyone."""
        with self._lock:
            ok = self._start_lim.allow(source, self._now())
        if not ok:
            raise OidcError("rate_limited")
        meta = self._discover()
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        cookie = self._seal(
            {
                "state": state,
                "nonce": nonce,
                "verifier": verifier,
                "fresh": fresh,
                "exp": self._now() + TX_TTL_S,
                "purpose": purpose,
                "bind": bind,
            }
        )
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

    def _complete(self, nonce: str, exp: float) -> None:
        """Remember a COMPLETED login. Only successes land here, so forged or failed finishes use no capacity. Expired
        entries leave from the front; when full the oldest goes (safe: the IdP's code is single-use, this is a second fence)."""
        now = self._now()
        with self._lock:
            while self._done and next(iter(self._done.values())) <= now:
                self._done.popitem(last=False)
            while len(self._done) >= USED_MAX:
                self._done.popitem(last=False)
            self._done[nonce] = exp

    def finish(
        self,
        cookie: str,
        state: str,
        code: str,
        client_secret: str,
        source: str = "",
        *,
        purpose: str = "signin",
        bind: str = "",
    ) -> Identity:
        """Check the callback, swap the code, verify the id_token. Raises :class:`OidcError`; never returns a half result.
        The caller clears the cookie whatever happens. Replay protection: the IdP's single-use code and the nonce in the
        id_token; here an in-flight lock per login (no parallel finishes) and a set of completed ones."""
        with self._lock:
            ok = self._finish_lim.allow(source, self._now())
        if not ok:
            raise OidcError("rate_limited")
        tx = self._open(cookie)
        if tx is None or tx["exp"] <= self._now():
            raise OidcError("bad_state")
        if not isinstance(state, str) or not secrets.compare_digest(state.encode(), tx["state"].encode()):
            raise OidcError("bad_state")  # URL and cookie must be the same login
        if (
            tx["purpose"] != purpose or tx["bind"] != bind
        ):  # a link / test login cannot be used to sign in, or by another person
            raise OidcError("bad_state")
        if not isinstance(code, str) or not (0 < len(code) <= 2048):
            raise OidcError("bad_code")
        nonce = tx["nonce"]
        with self._lock:
            if nonce in self._done or nonce in self._inflight:
                raise OidcError("bad_state")
            self._inflight.add(nonce)
        try:
            ident = self._exchange(tx, code, client_secret)
            self._complete(nonce, tx["exp"])
            return ident
        finally:
            with self._lock:
                self._inflight.discard(nonce)

    def _exchange(self, tx: dict[str, Any], code: str, client_secret: str) -> Identity:
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
