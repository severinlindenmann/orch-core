"""SQLite store (``<state>/invites.db``) and the proof challenges of the invitation links. See the package docstring.

Rules enforced here, in one place:

* the token is never stored: only ``sha256(token)``. A row says what it is for (``kind``, ``for_user`` / ``name``), who made
  it and when it ends; ``used_ms`` is set exactly once;
* :meth:`Invites.redeem` runs the single-use claim and the caller's account change inside ONE ``BEGIN IMMEDIATE``
  transaction on this database: ``UPDATE ... WHERE used_ms IS NULL AND expires_ms > now``, the row count must be 1, then the
  account change, then ``COMMIT``. If the account change raises, the claim is rolled back and the link stays usable; two
  racing redemptions are serialised by the write lock, so exactly one gets the row;
* an unknown, expired, used or malformed token is the same :class:`InviteError` ``bad_invite``;
* proof challenges (a reset by a person who still has an approving device) live in memory only, five minutes, bound to
  the token hash, single use.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from ..contract.canon import CanonError, b64u, unb64u
from ..fsutil import PrivateFileError, open_private_db

INVITES_FILE = "invites.db"
INVITE_TTL_MS = 7 * 86_400_000
CHALLENGE_TTL_MS = 5 * 60_000
MAX_CHALLENGES = 4096
MAX_PER_TOKEN = 8
KINDS = ("join", "reset", "recovery")
#: domain separation: what a channel key signs for a reset is never an event digest (``hub-event-digest|``)
PROOF_LABEL = b"orch/v2/invite-proof|"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
T = TypeVar("T")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS invites (
  id TEXT PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('join','reset','recovery')),
  for_user TEXT,
  name TEXT,
  created_by TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  expires_ms INTEGER NOT NULL,
  used_ms INTEGER,
  redeemed_user TEXT,
  company INTEGER NOT NULL DEFAULT 0,
  label TEXT,
  idp_name TEXT
);
CREATE INDEX IF NOT EXISTS invites_for_user ON invites(for_user);
"""


class InviteError(Exception):
    """Always ``bad_invite`` (or ``store_error``): the caller can tell nothing more about a token."""

    def __init__(self, code: str = "bad_invite") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Invite:
    id: str  # a random reference for the activity log; unrelated to the token
    token_hash: str
    kind: str
    for_user: str | None
    name: str | None
    created_by: str
    created_ms: int
    expires_ms: int
    company: bool = False  # a join link that is redeemed ONLY by a company login (HUB-PE-04), never with a password


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def proof_digest(challenge: str, token_hash_hex: str) -> bytes:
    """The 32-byte digest a channel key signs with its ordinary channel-proof primitive: ``SHA-256(label || challenge ||
    token_hash)``, challenge and hash as raw bytes."""
    return hashlib.sha256(PROOF_LABEL + unb64u(challenge, 32) + bytes.fromhex(token_hash_hex)).digest()


class Invites:
    def __init__(self, path: str | Path = ":memory:", *, now_ms: Callable[[], int] | None = None) -> None:
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.RLock()
        self._challenges: dict[str, dict[str, int]] = {}  # token hash -> {challenge: expires_ms}
        try:
            self._db = open_private_db(str(path), check_same_thread=False, isolation_level=None)
            self._db.executescript(_SCHEMA)
            if "redeemed_user" not in {r[1] for r in self._db.execute("PRAGMA table_info(invites)")}:
                # r11 P12b: a store made before the column (hub.dev, the run hubs); redeem() writes it in its transaction
                self._db.execute("ALTER TABLE invites ADD COLUMN redeemed_user TEXT")
            have = {r[1] for r in self._db.execute("PRAGMA table_info(invites)")}
            for col, decl in (("company", "INTEGER NOT NULL DEFAULT 0"), ("label", "TEXT"), ("idp_name", "TEXT")):
                if col not in have:  # a store made before company login
                    self._db.execute(f"ALTER TABLE invites ADD COLUMN {col} {decl}")
        except PrivateFileError as e:
            raise InviteError("store_error") from e
        except (sqlite3.Error, OSError) as e:
            raise InviteError("store_error") from e

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- making and reading -------------------------------------------------------------------------------------------

    def create(
        self, kind: str, *, created_by: str, name: str | None = None, for_user: str | None = None, company: bool = False
    ) -> tuple[str, str, int]:
        """A new link. Returns ``(token, invite_id, expires_ms)``; the token is returned here and never again. A reset or
        recovery for a person voids that person's earlier unused links (one live link at a time)."""
        if kind not in KINDS or (kind == "join") != (for_user is None) or (kind == "join" and not name):
            raise InviteError("store_error")
        if company and kind != "join":
            raise InviteError("store_error")
        token = secrets.token_urlsafe(32)  # 32 random bytes, URL-safe base64 (256 bits)
        iid, now = secrets.token_hex(8), self._now()
        try:
            with self._lock:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    self._db.execute("DELETE FROM invites WHERE expires_ms < ?", (now - 30 * 86_400_000,))
                    if for_user is not None:
                        self._db.execute(
                            "UPDATE invites SET used_ms=? WHERE for_user=? AND used_ms IS NULL", (now, for_user)
                        )
                    self._db.execute(
                        "INSERT INTO invites(id, token_hash, kind, for_user, name, created_by, created_ms, expires_ms, company) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            iid,
                            token_hash(token),
                            kind,
                            for_user,
                            name,
                            created_by,
                            now,
                            now + INVITE_TTL_MS,
                            int(company),
                        ),
                    )
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
        except sqlite3.Error as e:
            raise InviteError("store_error") from e
        return token, iid, now + INVITE_TTL_MS

    def void_for_user(self, user_id: str) -> None:
        """End every unused link made for this person (a disabling)."""
        try:
            with self._lock:
                self._db.execute(
                    "UPDATE invites SET used_ms=? WHERE for_user=? AND used_ms IS NULL", (self._now(), user_id)
                )
                self._challenges = {}
        except sqlite3.Error as e:
            raise InviteError("store_error") from e

    def company_labels(self) -> dict[str, dict[str, str]]:
        """``{user_id: {"email", "name"}}`` for people who joined through a company-login invitation: what the company
        account said at that moment, for the admin's People page (display text, never matched)."""
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT redeemed_user, label, idp_name FROM invites WHERE company=1 AND redeemed_user IS NOT NULL"
                ).fetchall()
        except sqlite3.Error as e:
            raise InviteError("store_error") from e
        return {u: {"email": e or "", "name": n or ""} for u, e, n in rows}

    def void_company(self) -> int:
        """End every unused company-login link (the provider, tenant or domain changed: they were made for the old one)."""
        try:
            with self._lock:
                n = self._db.execute(
                    "UPDATE invites SET used_ms=? WHERE company=1 AND used_ms IS NULL", (self._now(),)
                ).rowcount
                self._challenges = {}
                return n
        except sqlite3.Error as e:
            raise InviteError("store_error") from e

    def void_created_by(self, user_id: str) -> int:
        """End every unused link this person CREATED (their disabling or removal: a link outlives neither its maker's
        standing nor their account). Marks them used; writes nothing to any log. Returns how many were voided."""
        try:
            with self._lock:
                n = self._db.execute(
                    "UPDATE invites SET used_ms=? WHERE created_by=? AND used_ms IS NULL", (self._now(), user_id)
                ).rowcount
                self._challenges = {}
                return n
        except sqlite3.Error as e:
            raise InviteError("store_error") from e

    @staticmethod
    def _hash_of(token: object) -> str:
        if type(token) is not str or not _TOKEN.fullmatch(token):
            raise InviteError()
        return token_hash(token)

    def _row(self, h: str, now: int) -> Invite:
        rows = self._db.execute(
            "SELECT id, token_hash, kind, for_user, name, created_by, created_ms, expires_ms, company FROM invites "
            "WHERE token_hash=? AND used_ms IS NULL AND expires_ms>?",
            (h, now),
        ).fetchall()
        if not rows:
            raise InviteError()
        return Invite(*rows[0][:8], bool(rows[0][8]))

    def peek(self, token: object) -> Invite:
        """The link's row, without using it. ``bad_invite`` for unknown, expired, used or malformed."""
        h = self._hash_of(token)
        try:
            with self._lock:
                return self._row(h, self._now())
        except sqlite3.Error as e:
            raise InviteError("store_error") from e

    # --- redeeming ---------------------------------------------------------------------------------------------------

    def redeem(self, token: object, apply: Callable[[Invite], T]) -> T:
        """Claim the link and run ``apply(row)`` (the account change) in ONE transaction; return what ``apply`` returns.
        Not claimable (unknown, expired, used, malformed) is ``bad_invite`` before ``apply`` runs; if ``apply`` raises,
        the claim is rolled back and the link is still unused."""
        return self.redeem_hash(self._hash_of(token), apply)

    def redeem_hash(self, h: str, apply: Callable[[Invite], T]) -> T:
        """:meth:`redeem` for a link known by its hash (the company login keeps only the hash in its sealed cookie). The claim
        is the same atomic UPDATE, so the link is still single use."""
        if type(h) is not str or not re.fullmatch(r"[0-9a-f]{64}", h):
            raise InviteError()
        try:
            with self._lock:
                now = self._now()
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    cur = self._db.execute(
                        "UPDATE invites SET used_ms=? WHERE token_hash=? AND used_ms IS NULL AND expires_ms>?",
                        (now, h, now),
                    )
                    if cur.rowcount != 1:
                        raise InviteError()
                    out = apply(self._row_any(h))
                    uid = out.get("user_id") if isinstance(out, dict) else None
                    if type(uid) is str and uid:  # r11 P12b: who redeemed it, in the claim's own transaction
                        self._db.execute("UPDATE invites SET redeemed_user=? WHERE token_hash=?", (uid, h))
                    if (
                        isinstance(out, dict) and out.get("company_label") is not None
                    ):  # admin-only display labels, never matched
                        self._db.execute(
                            "UPDATE invites SET label=?, idp_name=? WHERE token_hash=?",
                            (out["company_label"], out.get("company_idp_name"), h),
                        )
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
                self._challenges.pop(h, None)
                return out
        except sqlite3.Error as e:
            raise InviteError("store_error") from e

    def status(self, invite_id: object, created_by: str) -> dict[str, Any] | None:
        """r11 P12b: the state of one JOIN link, for the admin who made it only: ``{"state": "waiting"}``, ``{"state":
        "joined", "user_id": ...}`` or ``{"state": "ended"}`` (expired, or voided without a redeem). None for a missing id,
        another creator's link or another kind, so a caller cannot tell those apart."""
        if type(invite_id) is not str or not re.fullmatch(r"[0-9a-f]{16}", invite_id):
            return None
        try:
            with self._lock:
                r = self._db.execute(
                    "SELECT kind, created_by, expires_ms, used_ms, redeemed_user, company, label, idp_name FROM invites WHERE id=?",
                    (invite_id,),
                ).fetchone()
        except sqlite3.Error as e:
            raise InviteError("store_error") from e
        if r is None or r[0] != "join" or r[1] != created_by:
            return None
        extra = {"company": True} if r[5] else {}
        if r[4]:
            if r[5]:  # only the maker sees what the company login said about the person (display text, never matched)
                extra.update(company_email=r[6] or "", company_name=r[7] or "")
            return {"state": "joined", "user_id": r[4], **extra}
        if r[3] is None and r[2] > self._now():
            return {"state": "waiting", **extra}
        return {"state": "ended", **extra}

    def _row_any(self, h: str) -> Invite:
        r = self._db.execute(
            "SELECT id, token_hash, kind, for_user, name, created_by, created_ms, expires_ms, company FROM invites "
            "WHERE token_hash=?",
            (h,),
        ).fetchone()
        return Invite(*r[:8], bool(r[8]))

    # --- proof challenges --------------------------------------------------------------------------------------------

    def new_challenge(self, th: str) -> str:
        """A random challenge bound to this token hash, valid five minutes. Bounded: per token and in total."""
        now = self._now()
        c = b64u(os.urandom(32))
        with self._lock:
            for k in list(self._challenges):
                live = {x: e for x, e in self._challenges[k].items() if e > now}
                if live:
                    self._challenges[k] = live
                else:
                    del self._challenges[k]
            mine = self._challenges.setdefault(th, {})
            if len(mine) >= MAX_PER_TOKEN:
                del mine[min(mine, key=mine.__getitem__)]  # the oldest goes
            if sum(len(v) for v in self._challenges.values()) >= MAX_CHALLENGES:
                raise InviteError("store_error")
            mine[c] = now + CHALLENGE_TTL_MS
        return c

    def take_challenge(self, th: str, challenge: object) -> bool:
        """Use a challenge: True once for a live one issued for THIS token hash, False otherwise (and it is gone)."""
        if type(challenge) is not str or len(challenge) > 64:
            return False
        try:
            unb64u(challenge, 32)
        except CanonError:  # CanonError is a ValueError
            return False
        with self._lock:
            exp = self._challenges.get(th, {}).pop(challenge, None)
        return exp is not None and exp > self._now()
