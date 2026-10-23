"""The host's marketplace records (V3 G2; spec 15.1): ``<state>/hosts/<workspace id>/market/``.

* ``markets.json``: ``{"v": 1, "markets": {<canonical url>: record}}``, strictly validated on every read. A file that is
  malformed is ``market.state_invalid``: it is **never reset** (a reset would drop the rollback floor, E6).
* ``<sha256(url)>.index.json``: the exact bytes of the last accepted index. Every reader re-parses and re-verifies
  them; the record keeps their sha256 and the commit they came from. A cache that fails is shown "invalid" and is
  never trusted.
* ``<sha256(url)>.refresh.lock`` (non-blocking ``flock`` held across the network work: one refresh per market) and
  ``<sha256(url)>.stamp`` (when the last fetch started, for the agent's minimum interval, E5).
* ``markets.lock``: held only for compare-and-write (E3), never across a network call.

Files are 0600 in 0700 directories of the host UID; a same-user agent can write them (Tier A, spec 15.1): forging the
cache fails re-verification, lowering ``last_version`` can only replay an old *validly signed* index, and nothing here
reaches running code without the person's signed grant.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from orch.market import MarketError
from orch.store.fsio import write_atomic

from .custody import ensure_private_dir, read_json

__all__ = ["MarketFiles"]

_MU = threading.Lock()
_HEX64 = re.compile(r"[0-9a-f]{64}(?![\s\S])")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})(?![\s\S])")
_RECORD = {
    "class": (str,),
    "added_at": (int, float),
    "index_commit": (str,),
    "index_sha256": (str,),
    "pinned": (bool,),
    "last_version": (int, type(None)),
    "payload_sha256": (str, type(None)),
    "sig_key": (str, type(None)),
}


def _valid_record(url: str, rec: Any) -> bool:
    if not isinstance(url, str) or not isinstance(rec, dict) or set(rec) != set(_RECORD):
        return False
    for k, types in _RECORD.items():
        v = rec[k]
        if not isinstance(v, types) or (isinstance(v, bool) and bool not in types):
            return False
    return (
        rec["class"] in ("official", "other")
        and _COMMIT.fullmatch(rec["index_commit"]) is not None
        and _HEX64.fullmatch(rec["index_sha256"]) is not None
        and (rec["payload_sha256"] is None or _HEX64.fullmatch(rec["payload_sha256"]) is not None)
        and (rec["last_version"] is None or rec["last_version"] >= 1)
    )


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


class MarketFiles:
    def __init__(self, state_dir: Path, workspace_id: str, host_uid: int) -> None:
        self.dir = Path(os.path.realpath(state_dir)) / "hosts" / workspace_id / "market"
        self.host_uid = host_uid

    def _ensure(self) -> Path:
        for level in (self.dir.parent, self.dir):
            ensure_private_dir(level, self.host_uid)
        return self.dir

    # ---------------------------------------------------------------- the records
    def load(self) -> dict[str, dict[str, Any]]:
        """The records, strictly. Missing file: none. Anything else wrong: ``market.state_invalid`` (no reset)."""
        try:
            doc = read_json(self.dir / "markets.json", self.host_uid, limit=1 << 20)
        except Exception:  # noqa: BLE001 - HostError(custody.unsafe_mode) and parse errors alike: refuse, never reset
            raise MarketError("market.state_invalid") from None
        if doc is None:
            return {}
        if (
            not isinstance(doc, dict)
            or set(doc) != {"v", "markets"}
            or doc["v"] != 1
            or not isinstance(doc["markets"], dict)
        ):
            raise MarketError("market.state_invalid")
        if not all(_valid_record(u, r) for u, r in doc["markets"].items()):
            raise MarketError("market.state_invalid")
        return doc["markets"]

    @contextlib.contextmanager
    def commit(self) -> Iterator[Callable[[dict[str, dict[str, Any]], dict[str, bytes]], None]]:
        """Compare-and-write: under the lock the caller re-reads (``load``), re-checks, and calls the yielded function
        with the new records and the cache bytes to write (``{url: bytes}``). Held for milliseconds, never across the
        network."""
        directory = self._ensure()
        with _MU:
            fd = os.open(directory / "markets.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)

                def write(records: dict[str, dict[str, Any]], caches: dict[str, bytes]) -> None:
                    for url, data in caches.items():  # the cache first: a record never points at a missing file
                        write_atomic(directory / f"{url_hash(url)}.index.json", data, mode=0o600)
                    body = json.dumps({"v": 1, "markets": records}, sort_keys=True).encode()
                    write_atomic(directory / "markets.json", body, mode=0o600)

                yield write
            finally:
                os.close(fd)

    # ---------------------------------------------------------------- cache, lock, stamp
    def read_cache(self, url: str) -> bytes | None:
        path = self.dir / f"{url_hash(url)}.index.json"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except OSError:
            return None
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if st.st_uid != self.host_uid or st.st_mode & 0o077 or st.st_size > (1 << 20):
                return None
            return f.read(1 << 20)

    @contextlib.contextmanager
    def refresh_lock(self, url: str) -> Iterator[None]:
        """One refresh per market at a time: a second caller is refused at once, never waits."""
        directory = self._ensure()
        fd = os.open(
            directory / f"{url_hash(url)}.refresh.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise MarketError("market.refresh_running") from None
            yield
        finally:
            os.close(fd)

    def last_fetch(self, url: str) -> float | None:
        try:
            doc = read_json(self.dir / f"{url_hash(url)}.stamp", self.host_uid, limit=1024)
        except Exception:  # noqa: BLE001 - an unreadable stamp counts as "never": the fetch then rewrites it
            return None
        t = doc.get("t") if isinstance(doc, dict) else None
        return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None

    def stamp(self, url: str, now: float) -> None:
        directory = self._ensure()
        write_atomic(directory / f"{url_hash(url)}.stamp", json.dumps({"t": now}).encode(), mode=0o600)
