"""Who may press a button, read at the moment of use (HUB-SV-02).

The helper keeps NO copy of the admin list or of the revoked channels. Every call opens the access store and the channel
store READ ONLY (``mode=ro``), and the hub's keys fresh (so a rotated card or a revoked ``HK_op`` is respected), and fails
closed: an unreadable store is ``unavailable``, never "allowed". The proof itself is the hub's own check
(:func:`~orch_hub.custodial.engine.check_channel_proof`): current certificate chained to the current card, factor, and the
signature over the digest."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from ..access.cli import ACCESS_FILE
from ..access.store import ADMIN_GROUP
from ..channels.service import CHANNELS_FILE
from ..contract.hub import Refused
from ..custodial.engine import check_channel_proof
from ..keys import HubKeys, KeysError
from .helper_proto import HEX32, HelperError


class ReadOnlyAuth:
    def __init__(self, state: Path, *, now_ms: Callable[[], int] | None = None) -> None:
        self._state = state
        self._now = now_ms or (lambda: int(time.time() * 1000))

    def _q(self, name: str, sql: str, args: tuple = ()) -> list[tuple]:
        try:
            with closing(sqlite3.connect(f"file:{self._state / name}?mode=ro", uri=True, timeout=2)) as db:
                return db.execute(sql, args).fetchall()
        except sqlite3.Error, OSError:
            raise HelperError("unavailable") from None

    def _keys(self) -> HubKeys:
        try:
            return HubKeys.open(self._state, prune=False, now_ms=self._now)
        except KeysError:
            raise HelperError("unavailable") from None

    def hub_id(self) -> str:
        return self._keys().hub_id

    def is_admin(self, user_id: str) -> bool:
        """A member of the hub-admin group who is not disabled and not waiting for a recovery link."""
        if not HEX32.fullmatch(user_id):
            return False
        rows = self._q(
            ACCESS_FILE,
            "SELECT 1 FROM members m JOIN users u ON u.user_id = m.user_id "
            "WHERE m.grp=? AND m.user_id=? AND u.disabled_ms IS NULL AND u.must_recover=0",
            (ADMIN_GROUP, user_id),
        )
        return bool(rows)

    def channel_revoked(self, channel_id: str) -> bool:
        if self._q(ACCESS_FILE, "SELECT 1 FROM revoked_channels WHERE channel_id=?", (channel_id,)):
            return True
        row = self._q(CHANNELS_FILE, "SELECT revoked_ms FROM channels WHERE channel_id=?", (channel_id,))
        return not row or row[0][0] is not None  # unknown counts as not usable

    # the ChannelRegistry that check_channel_proof reads
    def active_channel(self, user_id: str, channel_id: str) -> dict[str, Any] | None:
        try:
            if not (HEX32.fullmatch(user_id) and HEX32.fullmatch(channel_id)) or self.channel_revoked(channel_id):
                return None
            own = self._q(
                CHANNELS_FILE, "SELECT 1 FROM channels WHERE channel_id=? AND user_id=?", (channel_id, user_id)
            )
            if not own:
                return None
            for c in self._keys().channel_certs(user_id):
                if c["o"]["channel_id"] == channel_id:
                    return c
        except HelperError, KeysError, OSError, KeyError, TypeError:
            return None
        return None

    def check_admin_proof(self, user_id: str, channel_id: str, digest: bytes, proof: object) -> None:
        """Raises ``HelperError`` (the hub's own refusal code) unless ``user_id`` is a hub admin NOW and ``proof`` is
        their current channel's signature over ``digest``."""
        if not self.is_admin(user_id):
            raise HelperError("forbidden_role")
        if type(proof) is not str:
            raise HelperError("bad_channel_proof")
        try:
            check_channel_proof(
                keys=self._keys(),
                channels=self,
                user_id=user_id,
                channel_id=channel_id,
                digest=digest,
                proof=proof,
                now_ms=self._now(),
            )
        except Refused as e:
            raise HelperError(e.code) from None
