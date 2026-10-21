"""The runner's grant store (D-P2-27; H11c): at most one runner grant per (workspace, person), **in memory only**.

The host forwards a deposited secret once, in ``grant.put {deposit_id, workspace_id, person, grant, secret, until}``;
a newer grant for the same (workspace, person) replaces the older one for new launches (running agents keep the value
they were started with). ``grant.drop {grant}`` forgets one. A ``spawn`` of ``kind:"agent"`` names the grant id; the
runner resolves the secret here and puts it in that agent's environment as ``ORCH_GRANT`` (and nowhere else: never in
a report, the run table, a log line or a shell's environment). An expired grant is never handed out.

The store never touches a disk: a runner restart loses every grant (the person deposits again), and ``runner.hello``
reports only ``{workspace_id, person, grant, until}`` so the host can revalidate and drop what it no longer allows.

:class:`RedactFilter` is a logging filter for the runner's handlers that replaces every secret the store holds (and
any grant-shaped string) in a record's message (defence in depth: no code path logs a secret).
"""

from __future__ import annotations

import calendar
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

__all__ = ["GrantError", "GrantStore", "RedactFilter"]

GRANT_SHAPE = re.compile(r"f?gr_[0-7][0-9A-HJKMNP-TV-Z]{25}\.[A-Za-z0-9_-]{43}")  # person (gr_) and follow-up (fgr_)
_GRANT_ID = re.compile(r"gr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_FGRANT_ID = re.compile(r"fgr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_RUN = re.compile(r"r_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_WS = re.compile(r"[0-9a-f]{32}")
_PERSON = re.compile(r"p_[0-9a-f]{32}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z")
MAX_HELD = 64
MAX_RUN_SLOTS = 64


class GrantError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _epoch(stamp: str) -> float:
    return float(calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")))


@dataclass(frozen=True)
class _Held:
    grant: str
    secret: str
    until: str
    until_s: float
    deposit_id: str

    def __repr__(self) -> str:  # never the secret, whatever prints this
        return f"_Held({self.grant}, until={self.until})"


@dataclass(frozen=True)
class _RunSlot:
    workspace_id: str
    person: str
    grant: str
    secret: str
    until: str
    until_s: float

    def __repr__(self) -> str:  # never the secret, whatever prints this
        return f"_RunSlot({self.grant}, until={self.until})"


class GrantStore:
    def __init__(self, *, clock: Any = time.time) -> None:
        self._lock = threading.Lock()
        self._held: dict[tuple[str, str], _Held] = {}
        self._run_slots: dict[str, _RunSlot] = {}  # one-shot follow-up grants (FU9), keyed by run id
        self.clock = clock

    def put(self, args: dict[str, Any]) -> dict[str, Any]:
        """``grant.put``: validated, then it replaces this (workspace, person)'s grant. Answers ``{grant, until,
        replaced}`` (never the secret)."""
        deposit, wid, person = args.get("deposit_id"), args.get("workspace_id"), args.get("person")
        grant, secret, until = args.get("grant"), args.get("secret"), args.get("until")
        if not (isinstance(deposit, str) and _ULID.fullmatch(deposit)):
            raise GrantError("invalid.input", "grant.put: invalid deposit_id")
        if not (isinstance(wid, str) and _WS.fullmatch(wid) and isinstance(person, str) and _PERSON.fullmatch(person)):
            raise GrantError("invalid.input", "grant.put: invalid workspace or person")
        if not (isinstance(grant, str) and _GRANT_ID.fullmatch(grant)):
            raise GrantError("invalid.input", "grant.put: invalid grant id")
        if not (isinstance(secret, str) and GRANT_SHAPE.fullmatch(secret) and secret.startswith(grant + ".")):
            raise GrantError("invalid.input", "grant.put: the secret is not this grant's")
        if not (isinstance(until, str) and _TIME.fullmatch(until)):
            raise GrantError("invalid.input", "grant.put: invalid until")
        until_s = _epoch(until)
        if until_s <= self.clock():
            raise GrantError("launch.no_grant", "grant.put: the grant has expired")
        with self._lock:
            key = (wid, person)
            old = self._held.get(key)
            if old is None and len(self._held) >= MAX_HELD:
                raise GrantError("invalid.input", f"grant.put: at most {MAX_HELD} grants are held")
            self._held[key] = _Held(grant, secret, until, until_s, deposit)
        return {
            "grant": grant,
            "until": until,
            "replaced": old.grant if old is not None and old.grant != grant else None,
        }

    def put_run(self, args: dict[str, Any]) -> dict[str, Any]:
        """``grant.put_run {run, workspace_id, person, grant, secret, until}``: a one-shot follow-up grant (``fgr_``)
        for exactly one spawn of ``run``. It lives in its own table: it never replaces, shadows or reads the person's
        slot. A run id that already holds a slot is refused (never overwritten). Answers ``{run, grant, until}``."""
        run, wid, person = args.get("run"), args.get("workspace_id"), args.get("person")
        grant, secret, until = args.get("grant"), args.get("secret"), args.get("until")
        if not (isinstance(run, str) and _RUN.fullmatch(run)):
            raise GrantError("invalid.input", "grant.put_run: invalid run")
        if not (isinstance(wid, str) and _WS.fullmatch(wid) and isinstance(person, str) and _PERSON.fullmatch(person)):
            raise GrantError("invalid.input", "grant.put_run: invalid workspace or person")
        if not (isinstance(grant, str) and _FGRANT_ID.fullmatch(grant)):
            raise GrantError("invalid.input", "grant.put_run: invalid grant id")
        if not (isinstance(secret, str) and GRANT_SHAPE.fullmatch(secret) and secret.startswith(grant + ".")):
            raise GrantError("invalid.input", "grant.put_run: the secret is not this grant's")
        if not (isinstance(until, str) and _TIME.fullmatch(until)):
            raise GrantError("invalid.input", "grant.put_run: invalid until")
        until_s = _epoch(until)
        now = self.clock()
        if until_s <= now:
            raise GrantError("launch.no_grant", "grant.put_run: the grant has expired")
        with self._lock:
            self._prune_runs(now)
            if run in self._run_slots:
                raise GrantError("invalid.input", "grant.put_run: this run already holds a slot")
            if len(self._run_slots) >= MAX_RUN_SLOTS:
                raise GrantError("invalid.input", f"grant.put_run: at most {MAX_RUN_SLOTS} run slots are held")
            self._run_slots[run] = _RunSlot(wid, person, grant, secret, until, until_s)
        return {"run": run, "grant": grant, "until": until}

    def _prune_runs(self, now: float) -> None:  # caller holds the lock
        for run, slot in list(self._run_slots.items()):
            if slot.until_s <= now:
                del self._run_slots[run]

    def consume_run(self, run: Any, workspace_id: str, person: str, grant: Any) -> str:
        """The secret of ``run``'s one-shot slot, removing it (exactly once). Anything but an unexpired slot of this
        very run whose workspace, person and grant all match is ``launch.no_grant``; a mismatch leaves the slot."""
        with self._lock:
            slot = self._run_slots.get(run) if isinstance(run, str) else None
            try:
                if slot is not None and slot.until_s <= self.clock():
                    del self._run_slots[run]
                    slot = None
            except Exception:  # noqa: BLE001 - unreadable expiry: treat as unusable and remove
                self._run_slots.pop(run, None)
                slot = None
            try:  # corrupt slot state (wrong type, missing field) fails closed, and the broken slot is removed
                ok = slot is not None and (slot.workspace_id, slot.person, slot.grant) == (workspace_id, person, grant)
                secret = slot.secret if ok and slot is not None and isinstance(slot.secret, str) else None
            except Exception:  # noqa: BLE001
                ok, secret = False, None
                self._run_slots.pop(run, None)
            if not ok or secret is None:
                raise GrantError("launch.no_grant", "the runner holds no such one-shot grant for this run")
            del self._run_slots[run]
            return secret

    def drop(self, grant: Any) -> bool:
        with self._lock:
            dropped = False
            for run, slot in list(self._run_slots.items()):
                if slot.grant == grant:
                    del self._run_slots[run]
                    dropped = True
            for key, held in list(self._held.items()):
                if held.grant == grant:
                    del self._held[key]
                    return True
        return dropped

    def secret_for(self, workspace_id: str, person: str, grant: Any, run: Any = None) -> str:
        """The secret of ``grant`` if it is this (workspace, person)'s current, unexpired grant; else
        ``launch.no_grant``. A follow-up grant (``fgr_``) comes only from ``run``'s one-shot slot (consumed); the
        person slot is never consulted for it, and a person grant never touches a run slot."""
        if isinstance(grant, str) and grant.startswith("fgr_"):
            return self.consume_run(run, workspace_id, person, grant)
        with self._lock:
            held = self._held.get((workspace_id, person))
            if held is not None and held.until_s <= self.clock():
                del self._held[(workspace_id, person)]
                held = None
        if held is None or held.grant != grant:
            raise GrantError("launch.no_grant", "the runner holds no such runner grant for this person")
        return held.secret

    def report(self) -> list[dict[str, Any]]:
        """``runner.hello``'s ``grants``: ids and times only, expired ones dropped first."""
        now = self.clock()
        with self._lock:
            for key, held in list(self._held.items()):
                if held.until_s <= now:
                    del self._held[key]
            return [{"workspace_id": k[0], "person": k[1], "grant": h.grant, "until": h.until}
                    for k, h in sorted(self._held.items())]  # fmt: skip

    def secrets(self) -> list[str]:
        with self._lock:
            return [h.secret for h in self._held.values()] + [
                v for v in (getattr(s, "secret", None) for s in self._run_slots.values()) if isinstance(v, str)
            ]


class RedactFilter(logging.Filter):
    """Replaces every held secret and any grant-shaped string in a record (installed on the runner's handlers)."""

    def __init__(self, store: GrantStore) -> None:
        super().__init__()
        self.store = store

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken record is passed on unchanged
            return True
        clean = GRANT_SHAPE.sub("[redacted]", text)
        for secret in self.store.secrets():
            clean = clean.replace(secret, "[redacted]")
            tail = secret.partition(".")[2]
            if tail:
                clean = clean.replace(tail, "[redacted]")
        if clean != text:
            record.msg, record.args = clean, None
        return True
