"""The channel service (V3-D7). A Python service for D13's web layer and the dev CLI; no HTTP here.

Flow, browser: ``sign_in`` (user id + password + TOTP; rate limited) -> short-lived session token ->
``begin_registration(token, key_pub)`` -> the channel signs the returned digest -> ``complete_registration`` issues a
``hub.channel_cert`` with factor ``password+totp`` (protocol.md §6.2, SHORTCUT #908).
Flow, phone: the operator creates a one-time pairing code (``create_pairing_code``, a state-dir-owner action) and the
user enters it on the phone, inside a live session of the same user (``token=``, §6.3); ``begin_phone_registration`` /
``complete_registration`` issue factor ``se-unlocked``.
A password session can never mint ``se-unlocked``: the hub has no Secure Enclave attestation, so the factor is gated by
a code that only the state-dir owner can create (enforce by who can act, not by a label the caller picks).

**Registration proof (protocol.md §6.3).** ``begin_*`` returns the ``hub.channel_register`` object; the channel key
signs it with ``ChannelSigner.sign_object`` under its own label ``orch/v2/hub-channel-reg|`` and
``complete_registration`` checks it with the contract's ``verify_channel_register``. The factor is derived from the gate
the hub checked (pairing code -> ``se-unlocked``, session -> ``password+totp``), never from the caller. Registration and
revocation write a ``channel`` event to the audit chain (§10.5). A revocation is permanent (§6.2): no unrevoke.

``active_channel(user_id, channel_id)`` is the read interface of D6: the channel's current, unrevoked, unexpired
certificate, or ``None`` (fail closed on any error).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import struct
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..access import Access, AccessError
from ..config import dev_of
from ..contract import hub as H
from ..contract import phase4 as P4
from ..contract import suite as S
from ..contract.canon import b64u, unb64u
from ..keys import HubKeys, KeysError
from .store import ChannelsDB, StoreError

log = logging.getLogger(__name__)

CHANNELS_FILE = "channels.db"
SESSION_TTL_MS = 15 * 60_000
CHALLENGE_TTL_MS = 120_000
PAIRING_TTL_MS = 10 * 60_000
MAX_CHANNELS = 5
MAX_FAILURES = 5
FAIL_WINDOW_MS = 15 * 60_000
#: "password right, TOTP wrong" per user, whatever the source: with a leaked password this bounds TOTP guessing.
#: While it is tripped a sign-in is answered ``bad_credentials`` like any other failure (never a distinct code), so it is
#: no oracle for the password; it ends by time-out (rolling FAIL_WINDOW_MS) or ``Channels.unlock``.
MAX_TOTP_FAILURES = 10
#: failed PASSWORDS per (user, source) only: free tries, then a delay that doubles per failure up to the cap during which
#: a sign-in from that source is answered ``bad_credentials``. Never across sources: sign-in needs password AND TOTP,
#: so a stranger's wrong passwords must not be able to delay or lock the real user coming from another address.
PW_FREE_TRIES = 3
PW_BACKOFF_CAP_MS = 60_000
#: failed passwords per user across ALL sources (distributed guessing). It only alerts the operator (log, user id
#: only) past PW_USER_FREE: it NEVER refuses a correct password+TOTP, because a stranger can feed it and a refusal would
#: lock the real user out of every new address. A wrong password is refused anyway. Sources that signed in as this user
#: within KNOWN_SOURCE_MS are "known": the web layer exempts them from the network aggregate.
PW_USER_FREE = 10
KNOWN_SOURCE_MS = 30 * 86_400_000
MIN_PASSWORD = 10
CHANNEL_SESSION_MS = 12 * 3_600_000  # spec §7, protocol §6.4 (the contract's constant is the same)
#: the gate the hub checked for each channel kind (browser: live password+TOTP session; phone: operator pairing code)
GATE_OF = {"browser": "session", "phone": "pairing-code"}
_HEX32 = re.compile(r"[0-9a-f]{32}")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}
_DUMMY_SALT = b"\0" * 16


class ChannelError(Exception):
    """Every refusal; ``code`` is stable, ``str(e)`` never contains a secret."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


def registration_object(
    hub_id: str, user_id: str, kind: str, key_pub: str, nonce: str, expires_ms: int, dev: bool
) -> dict:
    """The ``hub.channel_register`` object the channel key signs (§6.3). ``attestation`` is the gate the hub checked."""
    return {
        "v": 2,
        "suite": S.SUITE_ID,
        "kind": "hub.channel_register",
        "hv": H.HV,
        "hub_id": hub_id,
        "user_id": user_id,
        "channel_kind": kind,
        "key_pub": key_pub,
        "attestation": GATE_OF[kind],
        "nonce": nonce,
        "expires_ms": expires_ms,
        "dev": dev,
    }


def _sha(x: str) -> str:
    return hashlib.sha256(x.encode()).hexdigest()


def _pw_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)


def totp_code(secret: bytes, counter: int) -> str:
    mac = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 15
    return f"{(struct.unpack('>I', mac[off : off + 4])[0] & 0x7FFFFFFF) % 10**6:06d}"


class Channels:
    def __init__(
        self,
        keys: HubKeys,
        access: Access,
        path: str | Path,
        *,
        now_ms: Callable[[], int] | None = None,
        max_channels: int = MAX_CHANNELS,
        audit: Any = None,
        on_revoke: Callable[[str], None] | None = None,
        dev_mode: bool,
    ) -> None:
        self.keys, self.acc = keys, access
        self._dev = dev_mode
        #: called with the channel id after a revocation is final (the relay cleanup, orch_hub.relay); never raises out
        self.on_revoke = on_revoke
        self.audit = audit  # orch_hub.audit.AuditLog; None = no chain (tests of other things)
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self.max_channels = max_channels
        try:
            self.db = ChannelsDB(str(path))
        except StoreError as e:
            raise ChannelError("store_error", str(e)) from None

    def close(self) -> None:
        self.db.close()

    # --- plumbing --------------------------------------------------------------------------------------------------

    def _user(self, user_id: object) -> str:
        if type(user_id) is not str or not _HEX32.fullmatch(user_id):
            raise ChannelError("malformed", "user_id")
        return user_id

    def _known_user(self, user_id: object) -> str:
        u = self._user(user_id)
        try:
            if not self.acc.has_user(u):
                raise ChannelError("unknown_user")
        except AccessError as e:
            raise ChannelError("store_error", e.code) from None
        return u

    def _guard(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except StoreError as e:
            raise ChannelError("store_error", str(e)) from None

    def _event(self, action: str, user_id: str, channel_id: str | None, actor: str) -> None:
        self.db.w(
            "INSERT INTO events(ts_ms, action, user_id, channel_id, actor) VALUES (?,?,?,?,?)",
            (self._now(), action, user_id, channel_id, actor),
        )

    def events(self) -> list[dict[str, Any]]:
        """The hub-local channel event log (pairing codes, register, revoke). The audit chain (§10.5) carries the
        register and revoke events too."""
        rows = self._guard(
            lambda: self.db.q("SELECT ts_ms, action, user_id, channel_id, actor FROM events ORDER BY id")
        )
        return [dict(zip(("ts_ms", "action", "user_id", "channel_id", "actor"), r, strict=True)) for r in rows]

    # --- credentials (operator, dev) -------------------------------------------------------------------------------

    def set_password(self, user_id: str, password: str) -> None:
        u = self._known_user(user_id)
        if type(password) is not str or len(password) < MIN_PASSWORD:
            raise ChannelError("weak_password", f"at least {MIN_PASSWORD} characters")
        salt = os.urandom(16)
        h = _pw_hash(password, salt)
        now = self._now()

        def go() -> None:
            self.db.w(
                "INSERT INTO credentials(user_id, pw_salt, pw_hash, updated_ms) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET pw_salt=excluded.pw_salt, pw_hash=excluded.pw_hash, "
                "updated_ms=excluded.updated_ms",
                (u, salt, h, now),
            )
            self.db.w("DELETE FROM sessions WHERE user_id=?", (u,))  # a changed password ends every session

        self._guard(go)

    def new_totp(self, user_id: str) -> dict[str, str]:
        """Generate (replace) the user's TOTP secret. Returned once, for the operator to hand over; never logged."""
        u = self._known_user(user_id)
        secret = os.urandom(20)
        now = self._now()

        def go() -> None:
            self.db.w(
                "INSERT INTO credentials(user_id, totp_secret, totp_last, updated_ms) VALUES (?,?,0,?) "
                "ON CONFLICT(user_id) DO UPDATE SET totp_secret=excluded.totp_secret, totp_last=0, "
                "updated_ms=excluded.updated_ms",
                (u, secret, now),
            )
            self.db.w("DELETE FROM sessions WHERE user_id=?", (u,))

        self._guard(go)
        b32 = base64.b32encode(secret).decode().rstrip("=")
        return {"secret": b32, "uri": f"otpauth://totp/orch-hub:{u}?secret={b32}&issuer=orch-hub"}

    # --- rate limiting ---------------------------------------------------------------------------------------------

    def _reserve(self, kind: str, subject: str) -> int:
        """Count and record the attempt in ONE immediate transaction before any expensive work, so parallel attempts
        cannot all pass the check. The row stays counted (a failure) unless :meth:`_succeed` removes it."""
        now = self._now()
        with self._tx() as db:
            db.execute("DELETE FROM failures WHERE ts_ms < ?", (now - FAIL_WINDOW_MS,))
            (n,) = db.execute("SELECT COUNT(*) FROM failures WHERE kind=? AND subject=?", (kind, subject)).fetchone()
            if n >= MAX_FAILURES:
                raise ChannelError("rate_limited")
            return db.execute(
                "INSERT INTO failures(kind, subject, ts_ms) VALUES (?,?,?)", (kind, subject, now)
            ).lastrowid

    def _judge(self, user: str, subject: str, ok_pw: bool, ok_totp: bool, rid: int) -> bool:
        """After the full password and TOTP check: is this sign-in blocked by the per-user TOTP lock or the password
        backoff for this (user, source)? One transaction with the same statements in every case. A blocked attempt is
        not counted, and a blocked CORRECT one gives its ``login`` reservation back (a refusal must not burn the real
        user's own pair). The cross-source failed-password count never blocks: it only raises the operator alert."""
        now = self._now()
        with self._tx() as db:
            db.execute("DELETE FROM failures WHERE ts_ms < ?", (now - FAIL_WINDOW_MS,))
            (nt,) = db.execute("SELECT COUNT(*) FROM failures WHERE kind='totp' AND subject=?", (user,)).fetchone()
            npw, last = db.execute(
                "SELECT COUNT(*), COALESCE(MAX(ts_ms),0) FROM failures WHERE kind='pw' AND subject=?", (subject,)
            ).fetchone()
            delay = 0 if npw < PW_FREE_TRIES else min(PW_BACKOFF_CAP_MS, 1000 * 2 ** (npw - PW_FREE_TRIES))
            (nall,) = db.execute(
                "SELECT COUNT(*) FROM failures WHERE kind='pw' AND (subject=? OR subject LIKE ?)", (user, user + "|%")
            ).fetchone()
            blocked = nt >= MAX_TOTP_FAILURES or now < last + delay
            kind = "noop" if (blocked or (ok_pw and ok_totp)) else ("pw" if not ok_pw else "totp")
            row = db.execute(
                "INSERT INTO failures(kind, subject, ts_ms) VALUES (?,?,?)",
                (kind, subject if kind == "pw" else user, now),
            ).lastrowid
            if kind == "noop":
                db.execute("DELETE FROM failures WHERE id=?", (row,))
            elif kind == "totp" and nt + 1 == MAX_TOTP_FAILURES:
                log.warning("sign-in TOTP lock tripped for user %s", user)
            elif kind == "pw" and nall + 1 == PW_USER_FREE:
                log.warning("distributed failed-password guessing at user %s", user)
            db.execute("DELETE FROM failures WHERE id=? AND ?=1", (rid, int(blocked and ok_pw and ok_totp)))
        return blocked

    def known_source(self, user_id: str, source: str) -> bool:
        """Did this source sign in as this user within KNOWN_SOURCE_MS? Cheap read, no hashing; any problem is False."""
        try:
            u = self._user(user_id)
            rows = self.db.q(
                "SELECT 1 FROM known_sources WHERE user_id=? AND source=? AND ts_ms>?",
                (u, source, self._now() - KNOWN_SOURCE_MS),
            )
        except ChannelError, sqlite3.Error:
            return False
        return bool(rows)

    def unlock(self, user_id: str) -> None:
        """Operator reset of the per-user TOTP lock and password backoff (the time-out does the same on its own)."""
        u = self._user(user_id)

        def go() -> None:
            with self._tx() as db:
                db.execute(
                    "DELETE FROM failures WHERE kind IN ('totp','pw') AND (subject=? OR subject LIKE ?)", (u, u + "|%")
                )

        self._guard(go)

    def _succeed(self, db: Any, kind: str, subject: str, rowid: int) -> None:
        """Inside the success transaction: drop this attempt and the earlier settled failures, not other attempts that
        are still in flight (they are indistinguishable from failures until they finish, so they stay counted)."""
        db.execute("DELETE FROM failures WHERE id=?", (rowid,))
        db.execute("DELETE FROM failures WHERE kind=? AND subject=? AND id<?", (kind, subject, rowid))

    # --- sessions --------------------------------------------------------------------------------------------------

    def _credentials(self, user_id: str, password: str, totp: str, source: str) -> tuple[str, str, int, int]:
        """The password + TOTP check shared by sign-in and the registration re-entry. Returns ``(user, subject, attempt,
        matched TOTP step)``; the caller claims the step in its own transaction. Password and TOTP together; any mismatch is one generic ``bad_credentials`` (no hint which part, none whether
        the user exists). After :data:`MAX_FAILURES` failures in :data:`FAIL_WINDOW_MS` the pair (user, ``source``) is
        ``rate_limited`` even with correct credentials, until the window passes. ``source`` is the caller's network
        address (the web layer's); the counter is per pair so a stranger who knows a user id cannot lock that user out
        from the user's own address. Without a ``source`` the counter is per user."""
        u = self._user(user_id)
        subject = u if not source else f"{u}|{str(source)[:64]}"
        if type(password) is not str or type(totp) is not str or len(password) > 1024:
            raise ChannelError("bad_credentials")
        rid = self._guard(lambda: self._reserve("login", subject))
        row = self._guard(
            lambda: self.db.q("SELECT pw_salt, pw_hash, totp_secret, totp_last FROM credentials WHERE user_id=?", (u,))
        )
        salt, ph, secret, last = row[0] if row else (None, None, None, 0)
        ok = bool(row) and ph is not None and secret is not None
        try:
            ok = ok and self.acc.has_user(u)
        except AccessError:
            ok = False
        h = _pw_hash(password, salt or _DUMMY_SALT)  # always spend the hashing time
        ok_pw = ok and hmac.compare_digest(h, ph or b"")
        counter = self._now() // 30_000
        match = 0
        if secret is not None and totp.isascii() and totp.isdigit() and len(totp) == 6:
            for c in (counter - 1, counter, counter + 1):
                if hmac.compare_digest(totp_code(secret, c), totp) and c > last:
                    match = c
        blocked = self._guard(lambda: self._judge(u, subject, ok_pw, bool(match), rid))
        if blocked or not (
            ok_pw and match
        ):  # one answer for all: wrong password, wrong TOTP, or a tripped lock/backoff
            raise ChannelError("bad_credentials")  # the reserved attempt stays counted
        return u, subject, rid, match

    def sign_in(
        self, user_id: str, password: str, totp: str, source: str = "", *, scope: str = "full"
    ) -> dict[str, Any]:
        """Password and TOTP together (:meth:`_credentials`: any mismatch is one generic ``bad_credentials``). ``scope``:
        ``full`` (the browser) or ``pairing`` (the phone before it binds, protocol §6.4)."""
        if scope not in ("full", "pairing"):  # a channel session is made by :meth:`bind_session` only
            raise ChannelError("malformed", "scope")
        u, subject, rid, match = self._credentials(user_id, password, totp, source)
        token = secrets.token_urlsafe(32)
        now = self._now()

        def go() -> None:
            with self._tx() as db:  # one transaction: claim the code, then the session; a lost race is a failure
                if (
                    db.execute(
                        "UPDATE credentials SET totp_last=? WHERE user_id=? AND totp_last<?", (match, u, match)
                    ).rowcount
                    != 1
                ):
                    raise ChannelError("bad_credentials")
                self._succeed(db, "login", subject, rid)
                db.execute(
                    "DELETE FROM failures WHERE (kind='totp' AND subject=?) OR (kind='pw' AND subject=?)", (u, subject)
                )
                if source:  # remembered 30 days: exempt from the network aggregate
                    db.execute(
                        "INSERT OR REPLACE INTO known_sources(user_id, source, ts_ms) VALUES (?,?,?)", (u, source, now)
                    )
                    db.execute("DELETE FROM known_sources WHERE ts_ms <= ?", (now - KNOWN_SOURCE_MS,))
                db.execute("DELETE FROM sessions WHERE expires_ms <= ?", (now,))
                db.execute(
                    "INSERT INTO sessions(token_hash, user_id, created_ms, expires_ms, scope, login_ms) VALUES (?,?,?,?,?,?)",
                    (_sha(token), u, now, now + SESSION_TTL_MS, scope, now),
                )

        self._guard(go)
        return {"token": token, "user_id": u, "expires_ms": now + SESSION_TTL_MS}

    def session_user(self, token: str) -> str:
        if type(token) is not str or not token or len(token) > 200:
            raise ChannelError("no_session")
        rows = self._guard(
            lambda: self.db.q("SELECT user_id, expires_ms FROM sessions WHERE token_hash=?", (_sha(token),))
        )
        if not rows or rows[0][1] <= self._now():
            raise ChannelError("no_session")
        try:
            if not self.acc.has_user(rows[0][0]):  # a removed user's sessions are dead
                raise ChannelError("no_session")
        except AccessError:
            raise ChannelError("no_session") from None
        return rows[0][0]

    def session_info(self, token: str) -> dict[str, Any]:
        """The live session behind ``token``: ``{user_id, scope, channel_id, expires_ms, login_ms}`` (``scope`` is
        ``full`` for a browser, ``pairing`` before a phone binds, ``channel`` after). ``no_session`` for anything else."""
        user = self.session_user(token)
        rows = self._guard(
            lambda: self.db.q(
                "SELECT scope, channel_id, expires_ms, login_ms, created_ms FROM sessions WHERE token_hash=?",
                (_sha(token),),
            )
        )
        if not rows:
            raise ChannelError("no_session")
        scope, cid, exp, login, created = rows[0]
        return {
            "user_id": user,
            "scope": scope,
            "channel_id": cid,
            "expires_ms": exp,
            "login_ms": login if login is not None else created,
        }

    def pin_api_origin(self, op_cert_id: str, origin: str) -> bool:
        """The first API origin signed under an op cert is the only one for that cert (protocol §6.4, D1: a new origin
        needs a new HK_op and the old cert revoked). True when ``origin`` is the pinned one."""
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO api_origins(op_cert_id, origin) VALUES (?,?)", (op_cert_id, origin))
            return (
                db.execute("SELECT origin FROM api_origins WHERE op_cert_id=?", (op_cert_id,)).fetchone()[0] == origin
            )

    def channel_state(self, user_id: str, channel_id: str) -> str:
        """``active`` | ``revoked`` | ``expired`` | ``unknown`` for one channel of the user (fail closed: ``unknown``)."""
        try:
            if channel_id in self.acc.revoked_channels():  # the access model's list is the truth (L2H1-2)
                return "revoked"
            for c in self.channels(user_id):
                if c["channel_id"] == channel_id:
                    return c["status"]
        except ChannelError, AccessError:
            pass
        return "unknown"

    def bind_session(self, token: str, signed: Any) -> dict[str, Any]:
        """``POST /api/v1/session/channel`` (protocol §6.4): a channel key binds the live PAIRING session ``token`` of its
        user to itself. Checks with the contract's ``verify_channel_auth``; the pairing session is consumed and the
        channel session made in ONE transaction, so two racing binds never both win (the loser is ``no_session``). The
        channel session lives until 12 h after the password sign-in or the certificate's end, whichever is first."""
        from ..contract import channelapi as CA

        now = self._now()
        new_token = secrets.token_urlsafe(32)
        with self._tx() as db:
            row = db.execute(
                "SELECT user_id, scope, created_ms, login_ms FROM sessions WHERE token_hash=? AND expires_ms>?",
                (_sha(str(token)), now),
            ).fetchone()
            if row is None or row[1] != "pairing":
                raise ChannelError("no_session")
            user = row[0]
            try:
                if not self.acc.has_user(user):
                    raise ChannelError("no_session")
            except AccessError:
                raise ChannelError("no_session") from None
            revoked = self.acc.revoked_channels()
            directory: dict[str, dict[str, Any]] = {}
            for cid, kind, rv in db.execute(
                "SELECT channel_id, kind, revoked_ms FROM channels WHERE user_id=?", (user,)
            ).fetchall():
                cert = self._cert_of(user, cid)
                directory[cid] = {
                    "user_id": user,
                    "kind": kind,
                    "key_pub": cert["o"]["key_pub"] if cert else "",
                    "expires_ms": cert["o"]["expires_ms"] if cert else 0,
                    "revoked": rv is not None or cid in revoked,
                }
            try:
                got = CA.verify_channel_auth(
                    signed,
                    hub_id=self.keys.hub_id,
                    session_user=user,
                    session_token=token,
                    channels=directory,
                    now_ms=now,
                    dev_mode=self._dev,
                )
            except H.Refused as r:
                raise ChannelError(r.code) from None
            cid = got["channel_id"]
            cert = self._cert_of(user, cid)
            if cert is None:
                raise ChannelError("unknown_channel")
            login = row[3] if row[3] is not None else row[2]
            expires = min(login + CHANNEL_SESSION_MS, cert["o"]["expires_ms"])
            if db.execute("DELETE FROM sessions WHERE token_hash=? AND scope='pairing'", (_sha(token),)).rowcount != 1:
                raise ChannelError("no_session")
            db.execute("DELETE FROM sessions WHERE expires_ms <= ?", (now,))
            db.execute(
                "INSERT INTO sessions(token_hash, user_id, created_ms, expires_ms, scope, channel_id, login_ms) "
                "VALUES (?,?,?,?,?,?,?)",
                (_sha(new_token), user, now, expires, "channel", cid, login),
            )
        return {"token": new_token, "user_id": user, "channel_id": cid, "expires_ms": expires, "channel_cert": cert}

    def sign_out(self, token: str) -> None:
        self._guard(lambda: self.db.w("DELETE FROM sessions WHERE token_hash=?", (_sha(str(token)),)))

    # --- registration ----------------------------------------------------------------------------------------------

    def create_pairing_code(self, user_id: str) -> dict[str, Any]:
        """Operator-only (the dev CLI, tied to the state dir owner): a one-time code for a phone to pair once."""
        u = self._known_user(user_id)
        code = "-".join(secrets.token_hex(2) for _ in range(3))
        now = self._now()
        self._guard(lambda: self.db.w("INSERT INTO pairing VALUES (?,?,?)", (_sha(code), u, now + PAIRING_TTL_MS)))
        self._guard(lambda: self._event("pairing_code", u, None, "operator"))
        return {"code": code, "expires_ms": now + PAIRING_TTL_MS}

    def reauth(self, user_id: str, password: str, totp: str, source: str = "") -> None:
        """Password + TOTP typed again (A8-3), through the same limiter state, hashing and single-use TOTP step as
        :meth:`sign_in`, without making a session. The code must be a later step than the last one accepted for the user
        (a code used at sign-in is refused: the person waits for the next one). ``bad_credentials`` for any mismatch."""
        u, subject, rid, match = self._credentials(user_id, password, totp, source)

        def go() -> None:
            with self._tx() as db:
                if (
                    db.execute(
                        "UPDATE credentials SET totp_last=? WHERE user_id=? AND totp_last<?", (match, u, match)
                    ).rowcount
                    != 1
                ):
                    raise ChannelError("bad_credentials")
                self._succeed(db, "login", subject, rid)
                db.execute(
                    "DELETE FROM failures WHERE (kind='totp' AND subject=?) OR (kind='pw' AND subject=?)", (u, subject)
                )

        self._guard(go)

    def begin_registration(self, token: str, key_pub: bytes, label: str = "") -> dict[str, Any]:
        """Browser channel, inside a session. The caller has re-verified password + TOTP (:meth:`reauth`, A8-3)."""
        u = self.session_user(token)
        return self._begin(u, "browser", key_pub, _sha(token), label, _sha(token))

    def begin_phone_registration(
        self, user_id: str, pairing_code: str, key_pub: bytes, label: str = "", *, token: str = ""
    ) -> dict[str, Any]:
        """A phone needs the operator's pairing code AND a live session of the same user (§6.3, L2P4-D7a): the code
        alone is no bearer credential for ``se-unlocked``."""
        u = self._user(user_id)
        if self.session_user(token) != u:  # ``no_session``; before the code is looked at or any attempt is counted
            raise ChannelError("no_session")
        if type(pairing_code) is not str or len(pairing_code) > 64:
            raise ChannelError("bad_pairing_code")
        rid = self._guard(lambda: self._reserve("pair", u))
        if not self._pairing_valid(u, _sha(pairing_code)):
            raise ChannelError("bad_pairing_code")  # the reserved attempt stays counted
        with self._tx() as db:
            self._succeed(db, "pair", u, rid)
        return self._begin(u, "phone", key_pub, _sha(pairing_code), label, _sha(token))

    def _pairing_valid(self, u: str, code_hash: str) -> bool:
        rows = self._guard(lambda: self.db.q("SELECT user_id, expires_ms FROM pairing WHERE code_hash=?", (code_hash,)))
        return bool(rows) and rows[0][0] == u and rows[0][1] > self._now()

    def _begin(self, u: str, kind: str, key_pub: bytes, binding: str, label: str, session: str) -> dict[str, Any]:
        if not S.valid_public_key(key_pub):
            raise ChannelError("malformed", "key_pub is not a valid P-256 public key")
        if type(label) is not str or len(label) > 60 or any(ord(c) < 32 or ord(c) == 127 for c in label):
            raise ChannelError("malformed", "label")
        self._known_user(u)
        now = self._now()
        nonce = b64u(os.urandom(32))
        ch = registration_object(
            self.keys.hub_id, u, kind, b64u(key_pub), nonce, now + CHALLENGE_TTL_MS, dev_of(self.keys)
        )

        def go() -> None:
            self.db.w("DELETE FROM challenges WHERE expires_ms <= ?", (now,))
            self.db.w(
                "INSERT INTO challenges VALUES (?,?,?,?,?,?,?,?)",
                (nonce, u, kind, b64u(key_pub), binding, label, now + CHALLENGE_TTL_MS, session),
            )

        self._guard(go)
        return {"challenge_id": nonce, "challenge": ch}

    def complete_registration(self, challenge_id: str, signed: dict[str, Any]) -> dict[str, Any]:
        """Check the signed ``hub.channel_register`` (§6.3) and issue the certificate. The challenge is single use: any
        attempt, good or bad, consumes it. Returns the signed ``hub.channel_cert``."""
        if type(challenge_id) is not str or len(challenge_id) > 64:
            raise ChannelError("challenge_invalid")
        now = self._now()
        with self._tx() as db:  # consumed and committed before any check, so a refused attempt cannot be retried
            rows = db.execute(
                "SELECT user_id, kind, key_pub, binding, label, expires_ms, session FROM challenges WHERE challenge_id=?",
                (challenge_id,),
            ).fetchall()
            db.execute("DELETE FROM challenges WHERE challenge_id=?", (challenge_id,))
        if not rows or rows[0][5] <= now:
            raise ChannelError("challenge_invalid")
        u, kind, key_b64, binding, label, exp, session = rows[0]
        with self._tx() as db:
            self._binding_alive(db, u, kind, binding, now)
            # the challenge is what the hub stored when it opened the registration (§6.3): this user, this key, the gate
            # it checked (session or pairing code) and whether a live session of the user opened it
            live = db.execute(
                "SELECT expires_ms FROM sessions WHERE token_hash=? AND user_id=?", (session, u)
            ).fetchall()
            key_pub = unb64u(key_b64, 65)
            cid0 = H.channel_id(key_pub)
            owner = db.execute("SELECT user_id FROM channels WHERE channel_id=?", (cid0,)).fetchall()
            gate = {
                "user_id": u,
                "key_pub": key_b64,
                "nonce": challenge_id,
                "expires_ms": exp,
                "gate": GATE_OF[kind],
                "session": bool(live) and live[0][0] > now,
            }
            try:
                got = P4.verify_channel_register(
                    signed,
                    hub_id=self.keys.hub_id,
                    challenge=gate,
                    now_ms=now,
                    dev_mode=self._dev,
                    revoked_channels=self.acc.revoked_channels(),
                    registered={cid0: owner[0][0]} if owner else None,
                )
            except H.Refused as r:
                raise ChannelError(r.code) from None
            factor, cid = got["factor"], got["channel_id"]
            old = db.execute("SELECT revoked_ms FROM channels WHERE channel_id=?", (cid,)).fetchall()
            if old:
                raise ChannelError("channel_revoked" if old[0][0] is not None else "channel_exists")
            (n,) = db.execute("SELECT COUNT(*) FROM channels WHERE user_id=? AND revoked_ms IS NULL", (u,)).fetchone()
            if n >= self.max_channels:
                raise ChannelError("too_many_channels")
            # fail closed: the chain record comes first, so a chain that cannot be written issues nothing (§6.3)
            self._audit(cid, u, "register", factor, u if kind == "browser" else None)
            try:
                cert = self.keys.issue_channel(u, key_pub, factor)
            except KeysError as e:
                raise ChannelError("issue_failed", e.code) from None
            db.execute(
                "INSERT INTO channels(channel_id, user_id, kind, label, factor, created_ms) VALUES (?,?,?,?,?,?)",
                (cid, u, kind, label, factor, now),
            )
            if kind == "phone":
                db.execute("DELETE FROM pairing WHERE code_hash=?", (binding,))  # pair once
            db.execute(
                "INSERT INTO events(ts_ms, action, user_id, channel_id, actor) VALUES (?,?,?,?,?)",
                (now, "register", u, cid, f"u_{u}" if kind == "browser" else "pairing"),
            )
        return cert

    def _audit(self, channel_id: str, user_id: str, change: str, factor: str, actor: str | None) -> None:
        """One ``channel`` event on the audit chain (§10.5). Inside the registration transaction, so a chain that cannot
        be written refuses the registration instead of leaving an unaudited channel."""
        if self.audit is None:
            return
        try:
            self.audit.channel_change(
                channel_id=channel_id, user_id=user_id, change=change, factor=factor, actor_user_id=actor
            )
        except Exception as e:  # noqa: BLE001 - fail closed on any chain error
            raise ChannelError("internal", type(e).__name__) from None

    def _tx(self):
        return self.db.tx()

    def _binding_alive(self, db: Any, u: str, kind: str, binding: str, now: int) -> None:
        if kind == "browser":
            r = db.execute("SELECT expires_ms FROM sessions WHERE token_hash=? AND user_id=?", (binding, u)).fetchall()
        else:
            r = db.execute("SELECT expires_ms FROM pairing WHERE code_hash=? AND user_id=?", (binding, u)).fetchall()
        if not r or r[0][0] <= now:
            raise ChannelError("no_session" if kind == "browser" else "bad_pairing_code")
        try:
            if not self.acc.has_user(u):
                raise ChannelError("unknown_user")
        except AccessError:
            raise ChannelError("unknown_user") from None

    # --- list / revoke ---------------------------------------------------------------------------------------------

    def channels(self, user_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT channel_id, user_id, kind, label, factor, created_ms, revoked_ms, revoked_by FROM channels"
        rows = self._guard(
            lambda: self.db.q(
                sql + (" WHERE user_id=?" if user_id else "") + " ORDER BY created_ms, channel_id",
                (self._user(user_id),) if user_id else (),
            )
        )
        out = []
        for cid, u, kind, label, factor, created, revoked, by in rows:
            cert = self._cert_of(u, cid)
            expires = cert["o"]["expires_ms"] if cert else None
            status = (
                "revoked"
                if revoked is not None
                else ("expired" if expires is not None and expires <= self._now() else "active")
            )
            out.append(
                {
                    "channel_id": cid,
                    "user_id": u,
                    "kind": kind,
                    "label": label,
                    "factor": factor,
                    "created_ms": created,
                    "expires_ms": expires,
                    "status": status,
                    "revoked_by": by,
                }
            )
        return out

    def channels_for_session(self, token: str) -> list[dict[str, Any]]:
        return self.channels(self.session_user(token))

    def _cert_of(self, user_id: str, channel_id: str) -> dict[str, Any] | None:
        try:
            for c in self.keys.channel_certs(user_id):
                if c["o"]["channel_id"] == channel_id:
                    return c
        except KeysError, OSError, KeyError, TypeError:
            return None
        return None

    def unpurged_revoked(self) -> list[str]:
        """Channels the store lists as revoked whose relay cleanup is not marked done (#52): a revoke made by another
        process (the CLI) is only visible here. Sorted, so a tick is deterministic."""
        rows = self._guard(
            lambda: self.db.q(
                "SELECT channel_id FROM channels WHERE revoked_ms IS NOT NULL "
                "AND channel_id NOT IN (SELECT channel_id FROM relay_purged) ORDER BY channel_id"
            )
        )
        return [r[0] for r in rows]

    def mark_relay_purged(self, channel_id: str) -> None:
        """The relay confirmed the purge of this revoked channel: never repeated by the periodic pass."""
        self._guard(
            lambda: self.db.w(
                "INSERT OR IGNORE INTO relay_purged (channel_id, ts_ms) VALUES (?,?)", (channel_id, self._now())
            )
        )

    def revoke(self, channel_id: str, *, actor: str | None = None) -> None:
        """``actor`` None = the operator at the hub's terminal (the CLI, tied to the state dir owner). Otherwise a user
        id the caller has already authenticated: the owner of the channel or a hub-admin may revoke. Anyone else gets
        ``forbidden`` (the same code for a channel that does not exist, so ids cannot be probed)."""
        cid = self._user(channel_id)
        if actor is not None:
            self._known_user(actor)
        rows = self._guard(lambda: self.db.q("SELECT user_id, revoked_ms FROM channels WHERE channel_id=?", (cid,)))
        try:
            allowed = actor is None or (bool(rows) and (rows[0][0] == actor or self.acc.is_hub_admin(actor)))
        except AccessError as e:
            raise ChannelError("store_error", e.code) from None
        if not allowed:
            raise ChannelError("forbidden")
        if not rows:  # only the operator gets here for an id this hub never registered (§6.3, L2P4-D7b)
            raise ChannelError("unknown_channel")
        now = self._now()
        try:
            self.acc.revoke_channel(cid)  # first: the snapshot source and active_channel's check. Permanent.
        except AccessError as e:
            raise ChannelError("store_error", e.code) from None
        if rows and rows[0][1] is None:
            by = f"u_{actor}" if actor else "operator"
            self._guard(
                lambda: self.db.w(
                    "UPDATE channels SET revoked_ms=?, revoked_by=? WHERE channel_id=?",
                    (now, by, cid),
                )
            )
            self._guard(lambda: self._event("revoke", rows[0][0], cid, by))
            (factor,) = self._guard(lambda: self.db.q("SELECT factor FROM channels WHERE channel_id=?", (cid,)))[0]
            self._audit(cid, rows[0][0], "revoke", factor, actor)
            if self.on_revoke is not None:
                try:
                    self.on_revoke(cid)
                except Exception:  # noqa: BLE001 - the revocation is done; a failing hook must not undo or hide it
                    log.warning("on_revoke hook failed")

    def remove_user(self, user_id: str, *, actor: str | None = None) -> None:
        """Remove a user: revoke (audit, purge at the relay through ``on_revoke``) EVERY channel of theirs first, then
        delete the user (L2H1-4). Idempotent: a retry skips what is revoked and finishes the job, so a failure part-way
        never leaves a deleted user with live channels. ``actor`` may not remove a hub-admin unless an admin
        (checked before anything is revoked)."""
        u = self._user(user_id)
        try:
            self.acc.check_remove_user(u, actor)
        except AccessError as e:
            raise ChannelError(e.code) from None
        for c in self.channels(u):
            if c["status"] != "revoked":
                self.revoke(c["channel_id"])  # the operator-level path: the removal is the authorised act
        try:
            self.acc.remove_user(u, actor=actor)
        except AccessError as e:
            raise ChannelError(e.code) from None

    def revoke_as(self, token: str, channel_id: str) -> None:
        self.revoke(channel_id, actor=self.session_user(token))

    # --- the read interface of the custodial engine (D6) -----------------------------------------------------------

    def active_channel(self, user_id: str, channel_id: str) -> dict[str, Any] | None:
        """The signed ``hub.channel_cert`` of this user's channel if it is registered here, not revoked, not expired,
        issued under the newest op cert and names this user and key; otherwise ``None``. Reads the revocation list on
        every call, so a revocation takes effect at once. Any error is ``None`` (fail closed)."""
        try:
            if type(user_id) is not str or type(channel_id) is not str:
                return None
            if not (_HEX32.fullmatch(user_id) and _HEX32.fullmatch(channel_id)):
                return None
            if not self.acc.has_user(user_id) or channel_id in self.acc.revoked_channels():
                return None
            reg = self.db.q("SELECT revoked_ms FROM channels WHERE channel_id=? AND user_id=?", (channel_id, user_id))
            if not reg or reg[0][0] is not None:
                return None
            cert = self._cert_of(user_id, channel_id)
            if cert is None:
                return None
            now = self.keys._now()
            held = H.verify_card(self.keys.card(), now_ms=now, dev_mode=self._dev, pinned_root=self.keys.hk_root_pub)
            o = H.verify_channel_cert(
                cert, held, now_ms=now, dev_mode=self._dev, revoked_op_certs=self.keys.revoked_op_certs()
            )
            if o["user_id"] != user_id or o["channel_id"] != channel_id:
                return None
            return cert
        except Exception:  # noqa: BLE001 - fail closed on anything (Refused, KeysError, StoreError, AccessError, OSError)
            return None
