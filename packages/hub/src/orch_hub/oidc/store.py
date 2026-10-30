"""What company login keeps on disk, all in the hub state dir, all 0600 through ``keys.fsutil`` (symlinks, other owners and
loose modes are refused):

* ``oidc.json``   the settings (no secret in it; the validator refuses any member it does not know)
* ``oidc.secret`` the IdP client secret, typed once by the admin, never returned by any API, never logged
* ``oidc.key``    32 random bytes that seal the login cookie (its own key, not derived from the client secret)
* ``oidc.db``     the links: (provider, realm, subject) -> hub person. The subject is the IdP's immutable id, never an email
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..fsutil import PrivateFileError, open_private_db
from ..keys.fsutil import StateError, read_file, write_atomic
from . import OidcConfig, OidcError

CONFIG_FILE, SECRET_FILE, KEY_FILE, DB_FILE = "oidc.json", "oidc.secret", "oidc.key", "oidc.db"
MODES = ("invited", "company")  # who may sign in: only linked people | anyone of the tenant/domain (a bare member)
FIELDS = frozenset(
    {"v", "provider", "client_id", "tenant", "domain", "redirect_uri", "mode", "idp_mfa", "enabled", "tested"}
)
SECRET_MAX = 512
_SCHEMA = """
CREATE TABLE IF NOT EXISTS oidc_links (
  provider TEXT NOT NULL, realm TEXT NOT NULL, subject TEXT NOT NULL, user_id TEXT NOT NULL UNIQUE,
  created_ms INTEGER NOT NULL, PRIMARY KEY (provider, realm, subject)
);
"""


def validate(d: Any) -> dict[str, Any]:
    """The clean settings, or ``OidcError('bad_config')``. Exact members, exact types, and the strict OidcConfig rules."""
    bad = OidcError("bad_config")
    if type(d) is not dict or set(d) != FIELDS or d["v"] != 1 or type(d["v"]) is not int:
        raise bad
    if d["mode"] not in MODES or any(type(d[k]) is not bool for k in ("idp_mfa", "enabled", "tested")):
        raise bad
    if any(type(d[k]) is not str for k in ("provider", "client_id", "tenant", "domain", "redirect_uri")):
        raise bad
    config_of(d)
    return dict(d)


def config_of(d: dict[str, Any]) -> OidcConfig:
    return OidcConfig(d["provider"], d["client_id"], d["redirect_uri"], tenant=d["tenant"], domain=d["domain"])


class OidcStore:
    def __init__(self, state: Path, *, now_ms: Any = None) -> None:
        self.state = Path(state)
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.RLock()
        try:
            self._db = open_private_db(str(self.state / DB_FILE), check_same_thread=False, isolation_level=None)
            self._db.executescript(_SCHEMA)
        except PrivateFileError, sqlite3.Error, OSError:
            raise OidcError("store_error") from None

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- settings, secret, key ---------------------------------------------------------------------------------------
    def load(self) -> dict[str, Any] | None:
        try:
            raw = read_file(self.state / CONFIG_FILE)
        except StateError as e:
            if e.code == "missing":
                return None
            raise OidcError("store_error") from None
        try:
            return validate(json.loads(raw))
        except ValueError, RecursionError:
            raise OidcError("store_error") from None

    def save(self, cfg: dict[str, Any]) -> dict[str, Any]:
        clean = validate(cfg)
        with self._lock:
            write_atomic(self.state / CONFIG_FILE, (json.dumps(clean, indent=2, sort_keys=True) + "\n").encode())
        return clean

    def set_secret(self, value: str) -> None:
        if (
            type(value) is not str
            or not (8 <= len(value) <= SECRET_MAX)
            or value != value.strip()
            or not value.isprintable()
        ):
            raise OidcError("bad_secret")
        with self._lock:
            write_atomic(self.state / SECRET_FILE, value.encode())

    def secret(self) -> str | None:
        try:
            return read_file(self.state / SECRET_FILE).decode()
        except StateError as e:
            if e.code == "missing":
                return None
            raise OidcError("store_error") from None

    def cookie_key(self) -> bytes:
        """The dedicated 32-byte cookie key; made on first use."""
        with self._lock:
            try:
                k = read_file(self.state / KEY_FILE)
            except StateError as e:
                if e.code != "missing":
                    raise OidcError("store_error") from None
                k = secrets.token_bytes(32)
                write_atomic(self.state / KEY_FILE, k)
            if len(k) != 32:
                raise OidcError("store_error")
            return k

    # --- links -------------------------------------------------------------------------------------------------------
    def link(self, user_id: str, provider: str, realm: str, subject: str) -> None:
        """One company identity per person and one person per identity. Never replaces: unlink first."""
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO oidc_links(provider, realm, subject, user_id, created_ms) VALUES (?,?,?,?,?)",
                    (provider, realm, subject, user_id, self._now()),
                )
            except sqlite3.IntegrityError:
                raise OidcError("already_linked") from None

    def user_for(self, provider: str, realm: str, subject: str) -> str | None:
        with self._lock:
            r = self._db.execute(
                "SELECT user_id FROM oidc_links WHERE provider=? AND realm=? AND subject=?", (provider, realm, subject)
            ).fetchone()
        return r[0] if r else None

    def linked(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute(
                "SELECT provider, realm, created_ms FROM oidc_links WHERE user_id=?", (user_id,)
            ).fetchone()
        return {"provider": r[0], "realm": r[1], "created_ms": r[2]} if r else None

    def unlink(self, user_id: str) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM oidc_links WHERE user_id=?", (user_id,)).rowcount > 0

    def unlink_all(self) -> None:
        """The identity provider changed: every old link belonged to the old one."""
        with self._lock:
            self._db.execute("DELETE FROM oidc_links")
