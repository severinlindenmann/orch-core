"""The service helper's signed action (HUB-SV-02, #1396): what an admin signs, and the single-use nonce that makes it fresh.

The HELPER issues ``{nonce, iat}`` at prepare (never the client), keeps the exact context, and consumes the nonce BEFORE
it runs anything (single use, 60 s). The admin's channel key signs ``"orch/v2/hub-channel-proof|" || digest`` with::

    digest = SHA-256( "orch.hub.service.action/v1\\n" || cj(ctx) )
    ctx = {kind, hub_id, host_id, part, action, target_version, nonce, iat, channel_id, user_id, text_sha256}

``text`` is the sentence the console shows; ``text_sha256`` binds it, so the console recomputes the digest from what it
displays (WYSIWYS). Nothing here runs a command."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..contract import suite as S
from ..contract.canon import cj
from ..fsutil import open_private_db

TAG = "orch.hub.service.action/v1"
ACTIONS = ("start", "stop", "restart", "update")
VERBS_RUN = ("start", "stop", "restart")  # what ``run`` may execute today ("update" waits for signed releases)
PART_RE = re.compile(r"[a-z][a-z0-9]{0,15}")
HEX32 = re.compile(r"[0-9a-f]{32}")
HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,39}")
TTL_MS = 60_000
MAX_PENDING = 64


class HelperError(Exception):
    """A refusal with a stable ``code`` (the web layer maps it); the text never carries secrets."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


#: The effect, in plain words, per part and action (P05): what the admin cares about, no machine name. The part and the
#: action stay bound in the signed context (``part``, ``action``, ``host_id``); only this displayed sentence is short.
_EFFECT = {
    ("Relay", "start"): "Phone alerts work again.",
    ("Relay", "stop"): "Phone alerts pause until you start it again.",
    ("Publish", "start"): "Shared pages are visible again to people outside the company.",
    ("Publish", "stop"): "Shared pages stop being visible to people outside the company until you start it again.",
    ("Hub", "start"): "The hub starts.",
    ("Hub", "stop"): "Stop the Hub.",
}


def text_for(label: str, action: str, host_id: str, target_version: str | None) -> str:
    """The sentence the admin reviews. Plain words, one line, built only from checked values. ``host_id`` is not shown
    (it stays bound in the signed context)."""
    if action == "restart":
        eff = "Unavailable for a few seconds."
    elif action == "update":
        eff = f"Update {label} to {target_version}."
    else:
        eff = _EFFECT.get((label, action), f"{action.capitalize()} {label}.")
    if label == "Hub" and action in ("stop", "restart"):
        eff += " This console goes offline too; workspaces keep working on their own."
    return eff


def context(
    *,
    hub_id: str,
    host_id: str,
    part: str,
    action: str,
    target_version: str | None,
    nonce: str,
    iat: int,
    channel_id: str,
    user_id: str,
    text: str,
) -> dict[str, Any]:
    return {
        "kind": TAG,
        "hub_id": hub_id,
        "host_id": host_id,
        "part": part,
        "action": action,
        "target_version": target_version,
        "nonce": nonce,
        "iat": iat,
        "channel_id": channel_id,
        "user_id": user_id,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def digest(ctx: dict[str, Any]) -> bytes:
    return S.sha256(TAG.encode("ascii") + b"\n" + cj(ctx))


class NonceStore:
    """Open prepared actions: nonce -> (ctx, text). ``take`` deletes in the same transaction that reads, so a nonce
    works once whatever happens next (a wrong proof burns it too)."""

    def __init__(self, path: str | Path, *, now_ms: Callable[[], int] | None = None) -> None:
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._mu = threading.Lock()
        self._db = open_private_db(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS pending (nonce TEXT PRIMARY KEY, user_id TEXT NOT NULL, exp_ms INTEGER NOT NULL, "
            "ctx TEXT NOT NULL, text TEXT NOT NULL)"
        )

    def close(self) -> None:
        self._db.close()

    def put(self, nonce: str, user_id: str, ctx: dict[str, Any], text: str) -> int:
        now = self._now()
        exp = now + TTL_MS
        with self._mu:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute("DELETE FROM pending WHERE exp_ms <= ?", (now,))
                (n,) = self._db.execute("SELECT COUNT(*) FROM pending").fetchone()
                if n >= MAX_PENDING:
                    self._db.execute("ROLLBACK")
                    raise HelperError("busy")
                # only this person's newest open action counts: an older one of theirs is dropped
                self._db.execute("DELETE FROM pending WHERE user_id=?", (user_id,))
                self._db.execute(
                    "INSERT INTO pending VALUES (?,?,?,?,?)", (nonce, user_id, exp, cj(ctx).decode("utf-8"), text)
                )
                self._db.execute("COMMIT")
            except sqlite3.Error:
                self._rollback()
                raise HelperError("unavailable") from None
        return exp

    def take(self, nonce: object) -> tuple[dict[str, Any], str, int] | None:
        """``(ctx, text, exp_ms)`` and the row is gone; None when unknown. Expiry is the caller's check (the row is
        still consumed)."""
        if type(nonce) is not str or not HEX32.fullmatch(nonce):
            return None
        with self._mu:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT exp_ms, ctx, text FROM pending WHERE nonce=?", (nonce,)).fetchone()
                if row is not None:
                    self._db.execute("DELETE FROM pending WHERE nonce=?", (nonce,))
                self._db.execute("COMMIT")
            except sqlite3.Error:
                self._rollback()
                raise HelperError("unavailable") from None
        return None if row is None else (json.loads(row[1]), row[2], row[0])

    def _rollback(self) -> None:
        try:
            self._db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
