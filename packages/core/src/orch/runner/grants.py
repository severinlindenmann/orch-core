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

from orch.host.mcpshared import CAPABILITY_ANY
from orch.tokenshape import GITHUB_TOKEN_SHAPE

__all__ = ["GrantError", "GrantStore", "RedactFilter"]

GRANT_SHAPE = re.compile(r"gr_[0-7][0-9A-HJKMNP-TV-Z]{25}\.[A-Za-z0-9_-]{43}")
_GRANT_ID = re.compile(r"gr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_WS = re.compile(r"[0-9a-f]{32}")
_PERSON = re.compile(r"p_[0-9a-f]{32}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z")
MAX_HELD = 64
MAX_RUN_HELD = 16
_RUN = re.compile(r"r_[0-7][0-9A-HJKMNP-TV-Z]{25}")


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


class GrantStore:
    def __init__(self, *, clock: Any = time.time) -> None:
        self._lock = threading.Lock()
        self._held: dict[tuple[str, str], _Held] = {}
        self._runs: dict[str, _Held] = {}
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
        """Hold a secret for one spawn, separately from the person's runner grant."""
        run, wid, person = args.get("run"), args.get("workspace_id"), args.get("person")
        grant, secret, until = args.get("grant"), args.get("secret"), args.get("until")
        if not (isinstance(run, str) and _RUN.fullmatch(run)):
            raise GrantError("invalid.input", "grant.put_run: invalid run")
        if not (isinstance(wid, str) and _WS.fullmatch(wid) and isinstance(person, str) and _PERSON.fullmatch(person)):
            raise GrantError("invalid.input", "grant.put_run: invalid workspace or person")
        if not (isinstance(grant, str) and _GRANT_ID.fullmatch(grant)):
            raise GrantError("invalid.input", "grant.put_run: invalid grant id")
        if not (isinstance(secret, str) and GRANT_SHAPE.fullmatch(secret) and secret.startswith(grant + ".")):
            raise GrantError("invalid.input", "grant.put_run: the secret is not this grant's")
        if not (isinstance(until, str) and _TIME.fullmatch(until)):
            raise GrantError("invalid.input", "grant.put_run: invalid until")
        try:
            until_s = _epoch(until)
        except ValueError:
            raise GrantError("invalid.input", "grant.put_run: invalid until") from None
        with self._lock:
            now = self.clock()
            for key, held in list(self._runs.items()):
                if held.until_s <= now:
                    del self._runs[key]
            if until_s <= now:
                raise GrantError("launch.no_grant", "grant.put_run: the grant has expired")
            old = self._runs.get(run)
            if old is None and len(self._runs) >= MAX_RUN_HELD:
                raise GrantError("invalid.input", f"grant.put_run: at most {MAX_RUN_HELD} grants are held")
            self._runs[run] = _Held(grant, secret, until, until_s, "")
        return {"grant": grant, "until": until, "replaced": old.grant if old and old.grant != grant else None}

    def secret_for_run(self, run: str, grant: Any) -> str:
        """Consume the matching, unexpired secret of this run exactly once."""
        with self._lock:
            held = self._runs.get(run) if isinstance(run, str) else None
            if held is not None and held.until_s <= self.clock():
                del self._runs[run]
                held = None
            if held is None or held.grant != grant:
                raise GrantError("launch.no_grant", "the runner holds no such grant for this run")
            del self._runs[run]
            return held.secret

    def drop(self, grant: Any) -> bool:
        dropped = False
        with self._lock:
            for entries in (self._held, self._runs):
                for key, held in list(entries.items()):
                    if held.grant == grant:
                        del entries[key]
                        dropped = True
        return dropped

    def secret_for(self, workspace_id: str, person: str, grant: Any) -> str:
        """The secret of ``grant`` if it is this (workspace, person)'s current, unexpired grant; else
        ``launch.no_grant``."""
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
            return [h.secret for entries in (self._held, self._runs) for h in entries.values()]


class RedactFilter(logging.Filter):
    """Replaces every held secret and any grant-shaped string in a record (installed on the runner's handlers)."""

    def __init__(self, store: GrantStore) -> None:
        super().__init__()
        self.store = store

    def _clean(self, text: str) -> str:
        clean = GRANT_SHAPE.sub("[redacted]", text)
        clean = GITHUB_TOKEN_SHAPE.sub("[redacted]", clean)
        clean = CAPABILITY_ANY.sub("[redacted]", clean)  # a company MCP proxy capability is never logged
        for secret in self.store.secrets():
            clean = clean.replace(secret, "[redacted]")
            tail = secret.partition(".")[2]
            if tail:
                clean = clean.replace(tail, "[redacted]")
        return clean

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - a record that cannot be formatted may carry a secret in its args
            record.msg, record.args = "[log message withheld: format error]", ()
            text = record.msg
        clean = self._clean(text)
        if clean != text:
            record.msg, record.args = clean, None
        # Exception text is formatted later by the handler from exc_info (and cached in exc_text): format it here,
        # redact it, and drop exc_info so the handler prints only the redacted text.
        try:
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = self._clean(record.exc_text)
                record.exc_info = None
            if record.stack_info:
                record.stack_info = self._clean(record.stack_info)
        except Exception:  # noqa: BLE001 - never let a formatting failure leak the raw traceback
            record.exc_info, record.exc_text, record.stack_info = None, "[exception text withheld]", None
        return True
