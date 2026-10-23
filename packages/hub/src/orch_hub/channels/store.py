"""SQLite store of the channel service (``<state>/channels.db``, 0600 via fsutil): password hashes, TOTP secrets,
failed-attempt counters, session token hashes, registration challenges, pairing code hashes, the registered channels
and the channel event log. Nothing here is ever printed or logged; tokens and codes are stored only as SHA-256."""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from ..fsutil import PrivateFileError, open_private_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
  user_id TEXT PRIMARY KEY, pw_salt BLOB, pw_hash BLOB, totp_secret BLOB, totp_last INTEGER NOT NULL DEFAULT 0,
  updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS failures (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, subject TEXT NOT NULL, ts_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS known_sources (user_id TEXT NOT NULL, source TEXT NOT NULL, ts_ms INTEGER NOT NULL, PRIMARY KEY (user_id, source));
CREATE INDEX IF NOT EXISTS failures_by ON failures(kind, subject, ts_ms);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS api_origins (op_cert_id TEXT PRIMARY KEY, origin TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS push_keys (
  channel_id TEXT PRIMARY KEY, push_pub TEXT NOT NULL, issued_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS challenges (
  challenge_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, key_pub TEXT NOT NULL,
  binding TEXT NOT NULL, label TEXT NOT NULL, expires_ms INTEGER NOT NULL, session TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pairing (code_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS channels (
  channel_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL, factor TEXT NOT NULL,
  created_ms INTEGER NOT NULL, revoked_ms INTEGER, revoked_by TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, action TEXT NOT NULL, user_id TEXT NOT NULL,
  channel_id TEXT, actor TEXT NOT NULL
);
"""


class StoreError(Exception):
    pass


class ChannelsDB:
    def __init__(self, path: str) -> None:
        self.lock = threading.RLock()
        try:
            self.db = open_private_db(path, check_same_thread=False, isolation_level=None)
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(sessions)")}
            for name, decl in (
                ("scope", "TEXT NOT NULL DEFAULT 'full'"),  # full (browser) | pairing | channel (H1, protocol §6.4)
                ("channel_id", "TEXT"),
                ("login_ms", "INTEGER"),  # the password sign-in a channel session's 12 h run from
            ):
                if name not in cols:
                    self.db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {decl}")
            if "session" not in {r[1] for r in self.db.execute("PRAGMA table_info(challenges)")}:
                self.db.execute("DROP TABLE challenges")  # open challenges live 120 s: drop, do not migrate
                self.db.executescript(SCHEMA)
        except PrivateFileError as e:
            raise StoreError(f"bad_private_file: {e}") from None
        except (sqlite3.Error, OSError) as e:
            raise StoreError(f"store_error: {type(e).__name__}") from None

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def q(self, sql: str, args: tuple = ()) -> list[tuple[Any, ...]]:
        try:
            with self.lock:
                return self.db.execute(sql, args).fetchall()
        except sqlite3.Error as e:
            raise StoreError(f"store_error: {type(e).__name__}") from None

    def w(self, sql: str, args: tuple = ()) -> int:
        try:
            with self.lock:
                return self.db.execute(sql, args).rowcount
        except sqlite3.Error as e:
            raise StoreError(f"store_error: {type(e).__name__}") from None

    def tx(self):
        return _Tx(self)


class _Tx:
    def __init__(self, d: ChannelsDB) -> None:
        self.d = d

    def __enter__(self) -> sqlite3.Connection:
        self.d.lock.acquire()
        try:
            self.d.db.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            self.d.lock.release()
            raise StoreError("store_error: begin") from None
        return self.d.db

    def __exit__(self, et, ev, tb) -> None:
        try:
            self.d.db.execute("COMMIT" if et is None else "ROLLBACK")
        except sqlite3.Error:
            if et is None:
                raise StoreError("store_error: commit") from None
        finally:
            self.d.lock.release()
