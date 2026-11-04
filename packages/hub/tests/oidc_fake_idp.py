"""An in-process identity provider for the company-login tests: discovery, JWKS, token endpoint, signed id_tokens."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from orch_hub.oidc import Http, OidcConfig

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "client-abc"
REDIRECT = "http://localhost:8100/oidc/callback"
NOW = 1_800_000_000


def entra_cfg(**kw: Any) -> OidcConfig:
    return OidcConfig("entra", CLIENT, REDIRECT, tenant=kw.get("tenant", TENANT))


def google_cfg() -> OidcConfig:
    return OidcConfig("google", CLIENT, REDIRECT, domain="acme.example")


class FakeIdp(Http):
    def __init__(self, cfg: OidcConfig) -> None:
        self.cfg = cfg
        self.key = RSAKey.generate_key(2048, {"kid": "k1", "use": "sig"})
        self.extra: list[Any] = []  # more keys published
        self.jwks_fails = False
        self.jwks_calls = 0
        self.claims: dict[str, Any] = {}  # overrides for the next id_token
        self.drop: set[str] = set()
        self.signer: Any = None  # replace to sign differently
        self.alg = "RS256"
        self.header_extra: dict[str, Any] = {}
        self.last_form: dict[str, str] = {}
        self.nonce = ""
        self.oid_of: dict[str, str] = {}  # nonce -> oid, same purpose
        self.nonce_of: dict[str, str] = {}  # code -> nonce, for tests that run several logins at once
        self.challenge: str | None = None  # when set, the token endpoint checks PKCE like a real IdP

    def get_json(self, url: str) -> dict[str, Any]:
        if url.endswith("openid-configuration"):
            return {
                "issuer": self.cfg.issuer,
                "authorization_endpoint": "https://idp.example/authorize",
                "token_endpoint": "https://idp.example/token",
                "jwks_uri": "https://idp.example/jwks",
            }
        self.jwks_calls += 1
        if self.jwks_fails:
            raise OSError("down")
        return KeySet([self.key, *self.extra]).as_dict(private=False)

    def base_claims(self, nonce: str | None = None) -> dict[str, Any]:
        c: dict[str, Any] = {
            "iss": self.cfg.issuer,
            "aud": CLIENT,
            "sub": "sub-1",
            "exp": NOW + 300,
            "iat": NOW - 5,
            "nonce": self.nonce if nonce is None else nonce,
            "email": "anna@acme.example",
            "name": "Anna",
        }
        if self.cfg.provider == "entra":
            c.update(tid=self.cfg.tenant, oid="oid-1")
        else:
            c.update(hd=self.cfg.domain, email_verified=True)
        return c

    def token(self, nonce: str | None = None) -> str:
        c = {**self.base_claims(nonce), **self.claims}
        if nonce in self.oid_of:
            c["oid"] = self.oid_of[nonce]
        for k in self.drop:
            c.pop(k, None)
        h = {"alg": self.alg, "kid": self.key.kid, **self.header_extra}
        return jwt.encode(h, c, self.signer or self.key)

    def post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        self.last_form = data
        if self.challenge is not None:
            got = (
                base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            )
            if got != self.challenge:
                raise OSError("invalid_grant: PKCE verification failed")
        return {"id_token": self.token(self.nonce_of.get(data["code"])), "access_token": "x"}


def verifier_ok(idp: FakeIdp, auth_url: str) -> dict[str, str]:
    from urllib.parse import parse_qs, urlsplit

    return {k: v[0] for k, v in parse_qs(urlsplit(auth_url).query).items()}
