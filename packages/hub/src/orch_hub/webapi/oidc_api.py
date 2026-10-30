"""Company login routes (HUB-PE-04, #1402). A mixin of :class:`~orch_hub.webapi.api.WebApi` plus its route table.

Company login opens SESSIONS only (``via='oidc'``). It never registers or uses a signing key: a browser key still needs the
hub password and a TOTP code (``_h_reg_begin``), so the factor of every key stays truthful and the identity provider can
never mint one. The login cookie is sealed (see :mod:`orch_hub.oidc`); the redirect URI comes from the hub's own
configuration, never from a request header. The client secret is read from the secret store only to swap a code and
appears in no answer, log or activity record.
"""

from __future__ import annotations

import contextlib
from typing import Any

from ..oidc import PLAIN, TX_TTL_S, Identity, OidcError, mfa_ok
from ..oidc.store import MODES, validate

STATUS = {"oidc_failed": 400, "oidc_not_allowed": 403, "oidc_not_ready": 409, "oidc_rate_limited": 429}
CALLBACK_PATH = "/oidc/callback"
#: settings whose change means the old links belong to another identity provider
REALM_FIELDS = ("provider", "tenant", "domain")
IDENTITY_FIELDS = ("provider", "client_id", "tenant", "domain", "redirect_uri")


def oidc_routes(r: Any) -> tuple[Any, ...]:
    return (
        r("GET", "/oidc/options", "none", "oidc_options", "Is company login on, and which provider"),
        r("GET", "/oidc/pending", "none", "oidc_pending", "What the login cookie of this browser is for"),
        r("POST", "/oidc/start", "none", "oidc_start", "Begin a company login: the address to send the browser to"),
        r(
            "POST",
            "/oidc/finish",
            "none",
            "oidc_finish",
            "Finish a company login: signs me in (a session, never a key)",
        ),
        r("GET", "/oidc/me", "session", "oidc_me", "Is my account linked to a company login"),
        r(
            "POST",
            "/oidc/link/start",
            "session",
            "oidc_link_start",
            "Link my company login: needs my password and code",
        ),
        r("POST", "/oidc/link/finish", "session", "oidc_link_finish", "Finish linking my company login"),
        r("DELETE", "/oidc/link", "session", "oidc_unlink", "Remove my company login link (ends its sessions)"),
        r("GET", "/admin/oidc", "admin", "a_oidc", "Company login settings (never the secret)"),
        r(
            "PUT",
            "/admin/oidc",
            "admin",
            "a_oidc_save",
            "Save the company login settings and, optionally, the client secret",
        ),
        r("POST", "/admin/oidc/test/start", "admin", "a_oidc_test_start", "Test company login: begin a login as me"),
        r(
            "POST",
            "/admin/oidc/test/finish",
            "admin",
            "a_oidc_test_finish",
            "Test company login: finish; passing allows turning it on",
        ),
    )


def _err(code: str) -> Exception:
    from .api import ApiError

    return ApiError(code)


class OidcHandlers:
    d: Any

    # --- plumbing ----------------------------------------------------------------------------------------------------
    def _oidc_svc(self) -> Any:
        if getattr(self.d, "oidc", None) is None:
            raise _err("unavailable")
        return self.d.oidc

    def _tx_cookie_name(self) -> str:
        return ("__Host-" if self.d.secure_cookies else "") + "orch_oidc_tx"

    def _tx_set(self, value: str) -> tuple[str, str]:
        a = f"{self._tx_cookie_name()}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={TX_TTL_S}"
        return ("Set-Cookie", a + ("; Secure" if self.d.secure_cookies else ""))

    def _tx_clear(self) -> tuple[str, str]:
        a = f"{self._tx_cookie_name()}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        return ("Set-Cookie", a + ("; Secure" if self.d.secure_cookies else ""))

    def _oidc_fail(self, e: OidcError, c: Any, *, detail: bool = False) -> tuple[int, dict, list]:
        """The cookie is cleared on every outcome. Public answers carry plain words, never the internal reason."""
        if e.code == "rate_limited":
            return 429, {"error": "oidc_rate_limited", "message": PLAIN["rate_limited"]}, [self._tx_clear()]
        if e.code == "not_allowed":
            return 403, {"error": "oidc_not_allowed", "message": PLAIN["not_allowed"]}, [self._tx_clear()]
        body = {"error": "oidc_failed", "message": PLAIN["failed"]}
        if detail:  # the admin's own test shows why
            body["reason"] = e.code
        return 400, body, [self._tx_clear()]

    def _redirect_uri(self, c: Any) -> str:
        base = self.d.api_origin
        if base is None and len(self.d.allowed_origins) == 1:
            base = next(iter(self.d.allowed_origins))
        if base is None:
            raise _err("unavailable")
        return base.rstrip("/") + CALLBACK_PATH

    def _need_browser(self, c: Any) -> None:
        if c.origin is None:  # a cookie is only ever set for a request the browser labelled
            raise _err("not_allowed")

    # --- public ------------------------------------------------------------------------------------------------------
    def _h_oidc_options(self, c: Any, p: dict, q: dict, b: dict | None):
        svc = getattr(self.d, "oidc", None)
        try:
            cfg = svc.settings() if svc is not None else None
        except OidcError:
            cfg = None
        on = bool(cfg and cfg["enabled"] and cfg["tested"])
        return 200, {"enabled": on, "provider": cfg["provider"] if on else None}

    def _h_oidc_pending(self, c: Any, p: dict, q: dict, b: dict | None):
        """The page the identity provider returns to asks which finish route to call. Read from the sealed cookie, so it
        needs no browser storage and cannot be set by anyone else; nothing is consumed."""
        svc = getattr(self.d, "oidc", None)
        o = svc.instance() if svc is not None else None
        return 200, {"purpose": o.purpose_of(c.cookie_of(self._tx_cookie_name())) if o is not None else None}

    def _h_oidc_start(self, c: Any, p: dict, q: dict, b: dict | None):
        self._need_browser(c)
        try:
            o, _cfg, _secret = self._oidc_svc().ready()
            url, cookie = o.start(source=c.source)
        except OidcError as e:
            return self._oidc_fail(e, c)
        return 200, {"url": url}, [self._tx_set(cookie)]

    def _h_oidc_finish(self, c: Any, p: dict, q: dict, b: dict | None):
        from .api import _need

        self._need_browser(c)
        b = _need(b, code=str, state=str)
        svc = self._oidc_svc()
        try:
            o, cfg, secret = svc.ready()
            ident = o.finish(c.cookie_of(self._tx_cookie_name()), b["state"], b["code"], secret, c.source)
            if not mfa_ok(ident):  # the IdP sent amr and it lacks mfa
                raise OidcError("not_allowed")
            uid = self._oidc_user(svc, cfg, ident)
            s = self.d.channels.open_company_session(uid)
        except OidcError as e:
            self._record_refusal(e)
            return self._oidc_fail(e, c)
        except Exception:  # noqa: BLE001 - a person we cannot sign in is the same plain refusal
            return self._oidc_fail(OidcError("not_allowed"), c)
        self._record("oidc.signed_in", actor_id=uid, subject_user=uid, detail={"mode": cfg["mode"]})
        ttl = max(0, (s["expires_ms"] - self.d.now_ms()) // 1000)
        return (
            200,
            {"user_id": uid, "expires_ms": s["expires_ms"]},
            [self._session_cookie(s["token"], ttl), self._tx_clear()],
        )

    def _record_refusal(self, e: OidcError) -> None:
        return None  # refusals are not recorded: they carry no person and anyone on the internet can cause them

    def _oidc_user(self, svc: Any, cfg: dict, ident: Identity) -> str:
        """The hub person for a verified identity. Linked: that person. Not linked: in "company" mode a new bare member
        (no groups, never an admin); otherwise nobody (the same ``not_allowed`` as for a disabled person)."""
        from ..access import AccessError

        uid = svc.store.user_for(ident.provider, ident.realm, ident.subject)
        if uid is not None:
            if not self.d.access.is_active(uid):
                raise OidcError("not_allowed")
            return uid
        if cfg["mode"] != "company":
            raise OidcError("not_allowed")
        name = (ident.name or ident.email or "Company login").strip()[:80] or "Company login"
        name = "".join(ch for ch in name if ch.isprintable())[:80] or "Company login"
        uid = self.d.keys.issue_uk(None)["o"]["user_id"]
        try:
            self.d.access.add_user(uid, name)  # no group is added: a bare member
            svc.store.link(uid, ident.provider, ident.realm, ident.subject)
        except AccessError, OidcError:
            try:
                self.d.access.remove_user(uid)
            except AccessError:
                pass
            raise OidcError("not_allowed") from None
        self._record("oidc.member_created", actor_id=uid, subject_user=uid, detail={"mode": "company"})
        return uid

    # --- my own link ---------------------------------------------------------------------------------------------------
    def _h_oidc_me(self, c: Any, p: dict, q: dict, b: dict | None):
        svc = getattr(self.d, "oidc", None)
        cfg = svc.settings() if svc is not None else None
        on = bool(cfg and cfg["enabled"] and cfg["tested"])
        linked = svc.store.linked(c.user_id) if svc is not None else None
        return 200, {
            "enabled": on,
            "provider": cfg["provider"] if on else None,
            "linked": linked,
            "via_company": c.via == "oidc",
        }

    def _h_oidc_link_start(self, c: Any, p: dict, q: dict, b: dict | None):
        from .api import _need

        self._need_browser(c)
        b = _need(b, password=str, totp=str)
        svc = self._oidc_svc()
        # linking changes who can sign in as this person: a fresh password + code, through the same caps as sign-in
        self._guarded_credentials(
            c, c.user_id, lambda: self.d.channels.reauth(c.user_id, b["password"], b["totp"], c.source)
        )
        try:
            o, _cfg, _secret = svc.ready()
            url, cookie = o.start(fresh=True, source=c.source, purpose="link", bind=c.user_id)
        except OidcError as e:
            return self._oidc_fail(e, c)
        return 200, {"url": url}, [self._tx_set(cookie)]

    def _h_oidc_link_finish(self, c: Any, p: dict, q: dict, b: dict | None):
        from .api import _need

        self._need_browser(c)
        b = _need(b, code=str, state=str)
        svc = self._oidc_svc()
        try:
            o, cfg, secret = svc.ready()
            ident = o.finish(
                c.cookie_of(self._tx_cookie_name()),
                b["state"],
                b["code"],
                secret,
                c.source,
                purpose="link",
                bind=c.user_id,
            )
            svc.store.link(c.user_id, ident.provider, ident.realm, ident.subject)
        except OidcError as e:
            return self._oidc_fail(e, c)
        self._record("oidc.linked", actor_id=c.user_id, subject_user=c.user_id, detail={"mode": cfg["mode"]})
        return 200, {"ok": True}, [self._tx_clear()]

    def _h_oidc_unlink(self, c: Any, p: dict, q: dict, b: dict | None):
        svc = self._oidc_svc()
        if svc.store.unlink(c.user_id):
            self.d.channels.end_company_sessions(c.user_id)
            self._record("oidc.unlinked", actor_id=c.user_id, subject_user=c.user_id)
        return 200, {"ok": True}

    # --- admin ---------------------------------------------------------------------------------------------------------
    def _h_a_oidc(self, c: Any, p: dict, q: dict, b: dict | None):
        svc = self._oidc_svc()
        cfg = svc.settings()
        out: dict[str, Any] = {
            "configured": cfg is not None,
            "has_secret": svc.store.secret() is not None,  # a yes/no, never the value
            "redirect_uri": None,
            "secure_address": False,
        }
        with contextlib.suppress(Exception):  # no address configured: the page says so
            out["redirect_uri"] = self._redirect_uri(c)
            out["secure_address"] = out["redirect_uri"].startswith(
                ("https://", "http://localhost:", "http://127.0.0.1:")
            )
        if cfg is not None:
            out.update(
                {
                    k: cfg[k]
                    for k in ("provider", "client_id", "tenant", "domain", "mode", "idp_mfa", "enabled", "tested")
                }
            )
        return 200, out

    def _h_a_oidc_save(self, c: Any, p: dict, q: dict, b: dict | None):
        from .api import _need

        b = _need(b, provider=str, client_id=str, tenant=str, domain=str, mode=str, idp_mfa=bool, enabled=bool)
        if b.keys() - {"provider", "client_id", "tenant", "domain", "mode", "idp_mfa", "enabled", "client_secret"}:
            raise _err("malformed")
        svc = self._oidc_svc()
        secret = b.get("client_secret", "")
        if type(secret) is not str:
            raise _err("malformed")
        old = svc.settings()
        new = {
            "v": 1,
            "provider": b["provider"],
            "client_id": b["client_id"].strip(),
            "tenant": b["tenant"].strip().lower(),
            "domain": b["domain"].strip().lower(),
            "redirect_uri": self._redirect_uri(c),
            "mode": b["mode"],
            "idp_mfa": b["idp_mfa"],
            "enabled": b["enabled"],
            "tested": False,
        }
        if new["provider"] == "entra":
            new["domain"] = ""
        elif new["provider"] == "google":
            new["tenant"] = ""
        try:
            new_secret = secret != ""
            if old is not None and not new_secret and all(old[k] == new[k] for k in IDENTITY_FIELDS):
                new["tested"] = old["tested"]  # nothing that identifies the provider changed: the test still counts
            validate(new)
            if new["enabled"] and (
                not new["tested"] or not new["idp_mfa"] or (svc.store.secret() is None and not new_secret)
            ):
                raise _err("oidc_not_ready")
            realm_changed = old is not None and any(old[k] != new[k] for k in REALM_FIELDS)
            if new_secret:
                svc.store.set_secret(secret)
            svc.store.save(new)
            if realm_changed:  # another provider, tenant or domain: every old link and every company-login session ends
                svc.store.unlink_all()
                self.d.channels.end_company_sessions(None)
        except OidcError:
            raise _err("malformed") from None
        self._record("oidc.settings_changed", actor_id=c.user_id, subject_user=c.user_id)
        return self._h_a_oidc(c, p, q, None)

    def _h_a_oidc_test_start(self, c: Any, p: dict, q: dict, b: dict | None):
        self._need_browser(c)
        svc = self._oidc_svc()
        try:
            o, _cfg, _secret = svc.for_test()
            url, cookie = o.start(fresh=True, source=c.source, purpose="test", bind=c.user_id)
        except OidcError as e:
            return self._oidc_fail(e, c, detail=True)
        return 200, {"url": url}, [self._tx_set(cookie)]

    def _h_a_oidc_test_finish(self, c: Any, p: dict, q: dict, b: dict | None):
        from .api import _need

        self._need_browser(c)
        b = _need(b, code=str, state=str)
        svc = self._oidc_svc()
        try:
            o, cfg, secret = svc.for_test()
            ident = o.finish(
                c.cookie_of(self._tx_cookie_name()),
                b["state"],
                b["code"],
                secret,
                c.source,
                purpose="test",
                bind=c.user_id,
            )
        except OidcError as e:
            return self._oidc_fail(e, c, detail=True)
        # the test passes: the provider answered with a verified id_token for the admin; it is remembered, nothing else
        svc.store.save({**cfg, "tested": True})
        return 200, {"ok": True, "email": ident.email, "name": ident.name, "mfa": mfa_ok(ident)}, [self._tx_clear()]


assert MODES
