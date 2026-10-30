"""The one place that holds the running :class:`~orch_hub.oidc.Oidc`: one instance per hub process, shared by every thread,
rebuilt only when the identity settings really change (a save with the same settings keeps it, and with it the set of
completed logins). A new instance is safe because the cookie AAD binds issuer and client id: an old cookie does not open."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from . import Http, Oidc, OidcError
from .http import UrlHttp
from .store import OidcStore, config_of


class OidcService:
    def __init__(
        self,
        store: OidcStore,
        http: Http | None = None,
        *,
        hosts: frozenset[str] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._now = now
        self.store = store
        self.http = http or UrlHttp()
        self._hosts = hosts  # None = the provider's own hosts; tests pass the fake IdP's
        self._lock = threading.Lock()
        self._key: tuple[Any, ...] | None = None
        self._oidc: Oidc | None = None

    def settings(self) -> dict[str, Any] | None:
        return self.store.load()

    def instance(self) -> Oidc | None:
        """The Oidc for the saved settings, or None when nothing is saved."""
        cfg = self.settings()
        if cfg is None:
            return None
        key = (cfg["provider"], cfg["client_id"], cfg["tenant"], cfg["domain"], cfg["redirect_uri"])
        with self._lock:
            if self._oidc is None or key != self._key:
                self._oidc = Oidc(
                    config_of(cfg), self.http, self.store.cookie_key(), self._now, allowed_hosts=self._hosts
                )
                self._key = key
            return self._oidc

    def ready(self) -> tuple[Oidc, dict[str, Any], str]:
        """For a sign-in: enabled, tested, with a secret. Anything else is ``not_allowed`` (no hint which)."""
        cfg = self.settings()
        secret = self.store.secret()
        if cfg is None or not cfg["enabled"] or not cfg["tested"] or secret is None:
            raise OidcError("not_allowed")
        o = self.instance()
        assert o is not None
        return o, cfg, secret

    def for_test(self) -> tuple[Oidc, dict[str, Any], str]:
        """For the admin's test login: saved settings and a secret, enabled or not."""
        cfg = self.settings()
        secret = self.store.secret()
        if cfg is None or secret is None:
            raise OidcError("not_allowed")
        o = self.instance()
        assert o is not None
        return o, cfg, secret
