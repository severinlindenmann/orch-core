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
from typing import TypeVar

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
  used_ms INTEGER
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
        except PrivateFileError as e:
            raise InviteError("store_error") from e
        except (sqlite3.Error, OSError) as e:
            raise InviteError("store_error") from e

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- making and reading -------------------------------------------------------------------------------------------

    def create(
        self, kind: str, *, created_by: str, name: str | None = None, for_user: str | None = None
    ) -> tuple[str, str, int]:
        """A new link. Returns ``(token, invite_id, expires_ms)``; the token is returned here and never again. A reset or
        recovery for a person voids that person's earlier unused links (one live link at a time)."""
        if kind not in KINDS or (kind == "join") != (for_user is None) or (kind == "join" and not name):
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
                        "INSERT INTO invites(id, token_hash, kind, for_user, name, created_by, created_ms, expires_ms) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (iid, token_hash(token), kind, for_user, name, created_by, now, now + INVITE_TTL_MS),
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
            "SELECT id, token_hash, kind, for_user, name, created_by, created_ms, expires_ms FROM invites "
            "WHERE token_hash=? AND used_ms IS NULL AND expires_ms>?",
            (h, now),
        ).fetchall()
        if not rows:
            raise InviteError()
        return Invite(*rows[0])

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
        h = self._hash_of(token)
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
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
                self._challenges.pop(h, None)
                return out
        except sqlite3.Error as e:
            raise InviteError("store_error") from e

    def _row_any(self, h: str) -> Invite:
        r = self._db.execute(
            "SELECT id, token_hash, kind, for_user, name, created_by, created_ms, expires_ms FROM invites "
            "WHERE token_hash=?",
            (h,),
        ).fetchone()
        return Invite(*r)

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
