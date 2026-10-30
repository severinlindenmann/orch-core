"""Bookkeeping for the host's polling of add-on events (addon-api 1.2, #718). Pure: no clock, no I/O, no process.

The host asks an add-on whether an event happened ONLY for the refs that a pending follow-up waits on. This class
decides which of them are due now: each ref at its own interval (the add-on's, between 60 s and 1 h, doubling after a
failure), at most :data:`PER_TICK` refs per add-on per tick, at most :data:`ADDON_HOUR` calls per add-on and
:data:`HOST_HOUR` per host in any hour. A due ref that does not fit the budget keeps its place (the oldest next-poll
goes first), so no ref starves. A source whose polls fail :data:`FAILS` times in a row is marked failed with a closed
code; the follow-up's deadline then wakes it as usual. Nothing an add-on says is kept except a declared status.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

__all__ = ["ADDON_HOUR", "HostBudget", "CODES", "FAILS", "HOST_HOUR", "MAX_S", "MIN_S", "PER_TICK", "Poller", "code_of"]

MIN_S, MAX_S = 60, 3600
PER_TICK = 5
ADDON_HOUR = 60
HOST_HOUR = 200
FAILS = 3
CODES = ("crashed", "timeout", "bad_response", "refused", "reported", "other")  # the closed reasons of a failed source
_ERRORS = {
    "addon.crashed": "crashed",
    "addon.timeout": "timeout",
    "addon.bad_response": "bad_response",
    "addon.output_too_large": "bad_response",
    "addon.error": "refused",
}

Item = tuple[str, str, str, int]  # (add-on name, full kind "<addon>.<kind>", ref, declared interval_s)


class HostBudget:
    """The host-wide hourly call count, shared by every workspace of this host (one file under the state dir, a file
    lock around read-modify-write). It survives a restart; an unreadable file counts as full (fail closed)."""

    def __init__(self, path: Path, host_uid: int, limit: int = HOST_HOUR) -> None:
        self.path, self.host_uid, self.limit = Path(path), host_uid, limit

    def _locked(self, wall: float, take: int, give: int = 0) -> bool:
        from . import custody

        custody.ensure_private_dir(self.path.parent, self.host_uid)
        fd = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                saved = custody.read_json(self.path, self.host_uid)
            except Exception:  # noqa: BLE001 - fail closed: unreadable counts as full
                return False
            calls = [
                float(c) for c in (saved or {}).get("calls", []) if isinstance(c, (int, float)) and wall - c < 3600
            ]
            if give:
                calls = sorted(calls)[: max(len(calls) - give, 0)]
            if take:
                if len(calls) + take > self.limit:
                    return False
                calls += [wall] * take
            custody.write_private(self.path, {"calls": calls})
            return True
        finally:
            os.close(fd)

    def take(self, wall: float) -> bool:
        return self._locked(wall, 1)

    def give_back(self, wall: float) -> None:
        self._locked(wall, 0, 1)


def code_of(runner_code: str) -> str:
    """The closed source-failure code of a runner error code."""
    return _ERRORS.get(runner_code, "other")


def key_of(item: Item) -> str:
    return f"{item[1]}|{item[2]}"


class Poller:
    def __init__(
        self,
        *,
        addon_hour: int = ADDON_HOUR,
        host_hour: int = HOST_HOUR,
        per_tick: int = PER_TICK,
        saved: dict[str, Any] | None = None,
        save: Callable[[dict[str, Any]], None] | None = None,
        host: HostBudget | None = None,
    ) -> None:
        self.host = host
        self.addon_hour, self.host_hour, self.per_tick = addon_hour, host_hour, per_tick
        self._save = save
        saved = saved if isinstance(saved, dict) else {}
        self.calls: list[list[Any]] = [
            [float(c[0]), str(c[1])]
            for c in saved.get("calls", [])
            if isinstance(c, list) and len(c) == 2 and isinstance(c[0], (int, float))
        ]
        self.sources: dict[str, dict[str, Any]] = {
            k: v
            for k, v in saved.get("sources", {}).items()
            if isinstance(v, dict) and v.get("code") in CODES and isinstance(v.get("since"), (int, float))
        }
        self._next: dict[str, float] = {}  # key -> monotonic time of the next poll; absent: poll at once
        self._fails: dict[str, int] = {}

    # -- planning
    def plan(self, items: Iterable[Item], mono: float, wall: float) -> list[Item]:
        """The items to poll now, oldest next-poll first, within the budgets. Counts the planned calls."""
        self.calls = [c for c in self.calls if wall - c[0] < 3600]
        seen: dict[str, Item] = {}
        for it in items:  # one call per (kind, ref) however many follow-ups wait on it
            seen.setdefault(key_of(it), it)
        due = sorted(
            (it for k, it in seen.items() if self._next.get(k, 0.0) <= mono),
            key=lambda it: self._next.get(key_of(it), 0.0),
        )
        per_addon: dict[str, int] = {}
        host = len(self.calls)  # only without a shared budget
        used = {}
        for _, name in self.calls:
            used[name] = used.get(name, 0) + 1
        out: list[Item] = []
        for it in due:
            name = it[0]
            if (
                per_addon.get(name, 0) >= self.per_tick
                or used.get(name, 0) >= self.addon_hour
                or (self.host is None and host >= self.host_hour)
            ):
                continue  # over budget: it keeps its place and goes first next tick
            if self.host is not None and not self.host.take(wall):
                break  # the host-wide hour is used up: nothing else this tick
            per_addon[name] = per_addon.get(name, 0) + 1
            used[name] = used.get(name, 0) + 1
            host += 1
            self.calls.append([wall, name])
            out.append(it)
        if out:
            self._persist()
        return out

    def refund(self, item: Item, wall: float) -> None:
        """Give back the call planned for an item that was not asked (the tick ran out of time)."""
        for i in range(len(self.calls) - 1, -1, -1):
            if self.calls[i][1] == item[0]:
                del self.calls[i]
                break
        if self.host is not None:
            self.host.give_back(wall)
        self._persist()

    # -- outcomes
    def done(self, item: Item) -> None:
        self._clear(key_of(item))

    def pending(self, item: Item, mono: float) -> None:
        key = key_of(item)
        self._clear(key)
        self._next[key] = mono + min(max(item[3], MIN_S), MAX_S)

    def failed(self, item: Item, code: str, mono: float, wall: float) -> None:
        """A failed poll (an error of the call, or the add-on's own ``failed``): back off; the third in a row marks the
        source failed."""
        key = key_of(item)
        n = self._fails.get(key, 0) + 1
        self._fails[key] = n
        self._next[key] = mono + min(min(max(item[3], MIN_S), MAX_S) * 2**n, MAX_S)
        if n >= FAILS and key not in self.sources:
            self.sources[key] = {"code": code if code in CODES else "other", "since": wall}
            self._persist()

    def _clear(self, key: str) -> None:
        self._fails.pop(key, None)
        if self.sources.pop(key, None) is not None:
            self._persist()

    def source(self, kind: str, ref: str) -> dict[str, Any] | None:
        got = self.sources.get(f"{kind}|{ref}")
        return None if got is None else {"state": "failed", **got}

    def forget(self, wanted: Iterable[Item]) -> None:
        """Drop everything about refs no pending follow-up waits on."""
        keep = {key_of(it) for it in wanted}
        for table in (self._next, self._fails):
            for k in [k for k in table if k not in keep]:
                del table[k]
        gone = [k for k in self.sources if k not in keep]
        for k in gone:
            del self.sources[k]
        if gone:
            self._persist()

    def _persist(self) -> None:
        if self._save is not None:
            self._save({"calls": self.calls, "sources": self.sources})
