"""Hub backup routes (M4 S5, #1462; protocol §17; Opus conditions 7-12, 23). A mixin of :class:`~orch_hub.webapi.api.WebApi`
(it only uses ``self.d``). Every route is admin only (the pipeline's cookie, CSRF, origin, scope and switch-ticket rules apply:
a switch-ticket, phone or pairing session never reaches these). Setting or replacing the backup key and downloading a backup
check the password and a current code INSIDE the request (the A8-3 pattern), per request, no remembered sign-in.

The 24 words are shown once, only to a console reached on loopback, over HTTPS or on the private-network address; on a
plain-http office or public address the route refuses (``insecure_origin``) and the page shows the server command instead.
Nothing here puts a word, a key or a file into a log, the audit chain or the activity history."""

from __future__ import annotations

import hashlib
import ipaddress
import urllib.parse
from typing import Any

from ..backup import restore as RS
from ..backup.service import CLI_LINE, KEEP, MANUAL_GAP_MS, NAME, BackupError, BackupService
from ..contract import backup as B
from ..contract.canon import b64u
from ..settings import SettingsError, load

#: codes this feature adds to the API's STATUS table (the rest are existing ones)
STATUS = {
    "confirm_denied": 403,
    "insecure_origin": 403,
    "no_backup_key": 409,
    "backup_failed": 500,
    "backup_not_found": 404,
    "wrong_backup_key": 403,
    "backup_invalid": 400,
    "restore_busy": 409,
    "no_admin": 409,
    "restore_failed": 500,
    "words": 400,
}
_MAP = {"not_found": "backup_not_found", "audit_failed": "backup_failed"}


def backup_routes(r: Any) -> tuple[Any, ...]:
    return (
        r("GET", "/admin/backup", "admin", "a_backup", "Hub backups: is the backup key set up, the kept backups"),
        r(
            "POST",
            "/admin/backup/key",
            "admin",
            "a_backup_key",
            "Set up or replace the hub backup key: password + code in the request; the 24 words are shown once",
        ),
        r(
            "POST",
            "/admin/backup/key/confirm",
            "admin",
            "a_backup_confirm",
            "The new backup words are saved: backups under the old key are deleted (same session, no step-up)",
        ),
        r("POST", "/admin/backups", "admin", "a_backup_make", "Make a hub backup now (audited)"),
        r(
            "POST",
            "/admin/backups/download",
            "admin",
            "a_backup_download",
            "Download a kept backup (sealed file): password + code in the request (audited)",
        ),
        r(
            "POST",
            "/admin/backup/restore",
            "admin",
            "a_backup_restore",
            "Restore the hub from a kept backup: password + code + the 24 words in the request (audited; the hub restarts)",
        ),
        r(
            "POST",
            "/admin/backup/restored/dismiss",
            "admin",
            "a_backup_restored_dismiss",
            "Hide the 'restored from a backup' notice",
        ),
    )


def safe_to_show_words(origin: str | None, private_webs: frozenset[str] = frozenset()) -> bool:
    """May the 24 words be shown on a console opened from this origin? Loopback and https: yes. A plain-http origin only
    when it IS this hub's own private-network (VPN) address from ``hub-settings.json`` (``private_webs``); not any other
    address of that range, not an office or public address (the words would cross a network in clear)."""
    if type(origin) is not str:
        return False
    try:
        u = urllib.parse.urlsplit(origin)
        host = u.hostname or ""
    except ValueError:
        return False
    if u.scheme == "https":
        return bool(host)
    if u.scheme != "http":
        return False
    if host == "localhost":
        return True
    try:
        if ipaddress.ip_address(host).is_loopback:
            return True
    except ValueError:
        return False
    return origin in private_webs


def _err(code: str) -> Exception:
    from .api import ApiError  # late: api.py imports this module through features.py

    return ApiError(_MAP.get(code, code))


def _session_id(c: Any) -> str:
    """Who must confirm the new words: this browser session (a hash, never the token)."""
    return hashlib.sha256(c.token.encode()).hexdigest()


class BackupHandlers:
    d: Any

    def _backups(self) -> BackupService:
        if self.d.keys is None:
            raise _err("unavailable")
        return BackupService(self.d.keys, audit=self.d.audit, activity=self.d.activity, now_ms=self.d.now_ms)

    def _private_webs(self) -> frozenset[str]:
        """The web addresses of kind ``private`` in the hub's settings file (what the install chose); none if unreadable."""
        try:
            s = load(self.d.settings_file.parent) if self.d.settings_file is not None else None
        except SettingsError:
            return frozenset()
        return frozenset(a["web"] for a in (s["addresses"] if s else ()) if a["kind"] == "private")

    def _stepup(self, c: Any, b: dict | None, *extra: str) -> dict:
        """The password and a current code in THIS request (A8-3); the same caps and limiter as every sign-in check."""
        if b is None or set(b) != {"password", "totp", *extra} or any(type(b[k]) is not str for k in b):
            raise _err("malformed")
        pw, totp = b["password"], b["totp"]
        self._guarded_credentials(c, c.user_id, lambda: self.d.channels.reauth(c.user_id, pw, totp, c.source))  # type: ignore[attr-defined]
        return b

    def _h_a_backup(self, c: Any, p: dict, q: dict, b: dict | None):
        svc = self._backups()
        try:
            info = svc.key_info()
            kept = svc.list()
        except BackupError as e:
            raise _err(e.code) from None
        return 200, {
            "key_set": info is not None,
            "key_set_ms": None if info is None else info["set_ms"],
            "key_pending": info is not None and info["pending"] is not None,
            "backups": kept,
            "keep": KEEP,
            "schedule": "daily",
            "cli_line": CLI_LINE,
            "restore_cli_line": RS.CLI_LINE,
            "restored": RS.notice(self.d.keys.state),
        }

    def _h_a_backup_key(self, c: Any, p: dict, q: dict, b: dict | None):
        if not safe_to_show_words(
            c.origin, self._private_webs()
        ):  # before any code is spent: nothing is made, nothing is shown
            raise _err("insecure_origin")
        self._stepup(c, b)
        words = ""
        try:
            words, made = self._backups().set_key(c.user_id, _session_id(c))
            body = {"words": words, "backup": made}
        except BackupError as e:
            raise _err(e.code) from None
        finally:
            del words
        return 200, body, [("Pragma", "no-cache")]

    def _h_a_backup_confirm(self, c: Any, p: dict, q: dict, b: dict | None):
        if b not in (None, {}):
            raise _err("malformed")
        try:
            return 200, {"ok": True, "removed": self._backups().confirm(_session_id(c))}
        except BackupError as e:
            raise _err(e.code) from None

    def _h_a_backup_make(self, c: Any, p: dict, q: dict, b: dict | None):
        if b not in (None, {}):
            raise _err("malformed")
        try:
            return 200, {"ok": True, "backup": self._backups().make(c.user_id, MANUAL_GAP_MS)}
        except BackupError as e:
            raise _err(e.code) from None

    def _h_a_backup_download(self, c: Any, p: dict, q: dict, b: dict | None):
        b = self._stepup(c, b, "name")
        try:
            data = self._backups().read(b["name"], c.user_id)
        except BackupError as e:
            raise _err(e.code) from None
        return 200, {"name": b["name"], "data": b64u(data)}

    def _h_a_backup_restore(self, c: Any, p: dict, q: dict, b: dict | None):
        """Cond 7 + 17 + 20: the words are typed only where they are safe to type (like the 24 words shown), password + code
        are checked in THIS request, the file is one of the kept backups (the body limit rules out an upload; copy the file
        into the backups folder or use the server command), and every restore check runs before a byte is written."""
        if not safe_to_show_words(c.origin, self._private_webs()):
            raise _err("insecure_origin")
        b = self._stepup(c, b, "name", "words")
        name, words = b["name"], b["words"]
        if NAME.fullmatch(name) is None:
            raise _err("backup_not_found")
        keys = self.d.keys
        path = keys.state / "backups" / name
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > B.MAX_TOTAL + (8 << 20):
                raise _err("backup_not_found")
            data = path.read_bytes()
        except OSError:
            raise _err("backup_not_found") from None
        ctl = self.d.settings_ctl
        try:
            got = RS.restore(
                keys.state,
                data,
                words,
                keys=keys,
                actor=c.user_id,
                audit=self.d.audit,
                activity=self.d.activity,
                now_ms=self.d.now_ms,
            )
        except RS.RestoreError as e:
            raise _err(e.code) from None
        finally:
            words = b = None  # the words go nowhere else
        restarting = bool(ctl is not None and ctl.restart is not None)
        if restarting:
            ctl.restart()
        out = {k: got[k] for k in ("made_ms", "gap_ms", "suspended")}
        return (202 if restarting else 200), {"ok": True, "restarting": restarting, **out}

    def _h_a_backup_restored_dismiss(self, c: Any, p: dict, q: dict, b: dict | None):
        if b not in (None, {}):
            raise _err("malformed")
        RS.dismiss_notice(self.d.keys.state)
        return 200, {"ok": True}
