"""The host's marketplace records (V3 G2; spec 15.1): ``<state>/hosts/<workspace id>/market/``.

* ``markets.json``: ``{"v": 1, "markets": {<canonical url>: record}}``, strictly validated on every read. A file that is
  malformed is ``market.state_invalid``: it is **never reset** (a reset would drop the rollback floor, E6).
* ``<sha256(url)>.index.json``: the exact bytes of the last accepted index. Every reader re-parses and re-verifies
  them; the record keeps their sha256 and the commit they came from. A cache that fails is shown "invalid" and is
  never trusted.
* ``<sha256(url)>.refresh.lock`` (non-blocking ``flock`` held across the network work: one refresh per market) and
  ``<sha256(url)>.stamp`` (when the last fetch started, for the agent's minimum interval, E5).
* ``markets.lock``: held only for compare-and-write (E3), never across a network call.
* ``installs.json`` (V3 G3): what the person installed: ``{"v": 1, "addons": {name: record}, "seen": {"<url>|<id>":
  version}, "keys": {"<url>|<id>": publisher key}}``. Written only after the signed grant landed
  (:mod:`orch.host.marketinstall`), under ``markets.lock`` (lock order: store, then markets.lock), strictly validated
  on every read like ``markets.json``. ``seen`` and ``keys`` outlive a removal (rollback floor and TOFU).
* ``staged/<stage>.json`` (V3 G3): one reviewed-but-not-yet-signed package (0600, written atomically, read without
  following links and only if it is the host's own file); see :func:`MarketFiles.write_stage`.

Files are 0600 in 0700 directories of the host UID; a same-user agent can write them (Tier A, spec 15.1): forging the
cache fails re-verification, lowering ``last_version`` can only replay an old *validly signed* index, and nothing here
reaches running code without the person's signed grant.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from orch import oslock
from orch.market import ADDONS, MarketError
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
        rec["class"] in ("official", "dev", "company", "other")
        and _COMMIT.fullmatch(rec["index_commit"]) is not None
        and _HEX64.fullmatch(rec["index_sha256"]) is not None
        and (rec["payload_sha256"] is None or _HEX64.fullmatch(rec["payload_sha256"]) is not None)
        and (rec["last_version"] is None or rec["last_version"] >= 1)
    )


_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}(?![\s\S])")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?![\s\S])")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}(?![\s\S])")
_STAGE = re.compile(r"st_[0-9a-f]{32}(?![\s\S])")
_INSTALL = {
    "origin": (str,),
    "version": (str,),
    "package_sha256": (str,),
    "commit": (str,),
    "installed_at": (int, float),
    "publisher_key": (str, type(None)),
}
MAX_STAGE_BYTES = 40 << 20


def _valid_install(name: str, rec: Any) -> bool:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None or not isinstance(rec, dict):
        return False
    if set(rec) != set(_INSTALL):
        return False
    for k, types in _INSTALL.items():
        if not isinstance(rec[k], types) or (isinstance(rec[k], bool) and bool not in types):
            return False
    return (
        _VERSION.fullmatch(rec["version"]) is not None
        and _DIGEST.fullmatch(rec["package_sha256"]) is not None
        and _COMMIT.fullmatch(rec["commit"]) is not None
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
                oslock.flock(fd, oslock.LOCK_EX)

                def write(records: dict[str, dict[str, Any]], caches: dict[str, bytes]) -> None:
                    for url, data in caches.items():  # the cache first: a record never points at a missing file
                        write_atomic(directory / f"{url_hash(url)}.index.json", data, mode=0o600)
                    body = json.dumps({"v": 1, "markets": records}, sort_keys=True).encode()
                    write_atomic(directory / "markets.json", body, mode=0o600)

                yield write
            finally:
                os.close(fd)

    # ---------------------------------------------------------------- installs (G3)
    def installs(self) -> dict[str, Any]:
        """``{"addons", "seen", "keys"}``, strictly. Missing file: empty. Anything else wrong: ``market.state_invalid``
        (never reset: a reset would drop the rollback floors and the remembered keys)."""
        try:
            doc = read_json(self.dir / "installs.json", self.host_uid, limit=1 << 20)
        except Exception:  # noqa: BLE001 - refuse, never reset
            raise MarketError("market.state_invalid") from None
        if doc is None:
            return {ADDONS: {}, "seen": {}, "keys": {}}
        if not isinstance(doc, dict) or set(doc) != {"v", ADDONS, "seen", "keys"} or doc["v"] != 1:
            raise MarketError("market.state_invalid")
        addons, seen, keys = doc[ADDONS], doc["seen"], doc["keys"]
        if (
            not isinstance(addons, dict)
            or not all(_valid_install(n, r) for n, r in addons.items())
            or not isinstance(seen, dict)
            or not all(isinstance(k, str) and isinstance(v, str) and _VERSION.fullmatch(v) for k, v in seen.items())
            or not isinstance(keys, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in keys.items())
        ):
            raise MarketError("market.state_invalid")
        return {ADDONS: addons, "seen": seen, "keys": keys}

    @contextlib.contextmanager
    def commit_installs(self) -> Iterator[Callable[[dict[str, Any]], None]]:
        """Compare-and-write for ``installs.json``: the same lock as :meth:`commit` (never held across the network);
        the caller re-reads with :meth:`installs` inside, then calls the yielded function once."""
        directory = self._ensure()
        with _MU:
            fd = os.open(directory / "markets.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                oslock.flock(fd, oslock.LOCK_EX)

                def write(doc: dict[str, Any]) -> None:
                    body = json.dumps({"v": 1, **doc}, sort_keys=True).encode()
                    write_atomic(directory / "installs.json", body, mode=0o600)

                yield write
            finally:
                os.close(fd)

    # ---------------------------------------------------------------- staged packages (G3)
    def _stage_dir(self) -> Path:
        directory = self._ensure() / "staged"
        ensure_private_dir(directory, self.host_uid)
        return directory

    def write_stage(self, doc: dict[str, Any]) -> None:
        """Write one stage file (0600, atomic) and delete every other stage of the same add-on name: one live stage per
        workspace and name. ``doc`` carries ``stage``, ``name`` and the base64 ``files``."""
        if _STAGE.fullmatch(doc["stage"]) is None or _NAME.fullmatch(doc["name"]) is None:
            raise MarketError("market.state_invalid")
        directory = self._stage_dir()
        body = json.dumps(doc, sort_keys=True).encode()
        if len(body) > MAX_STAGE_BYTES:
            raise MarketError("market.package_invalid")
        for other in self.stages():
            if other["name"] == doc["name"]:
                self.drop_stage(other["stage"])
        write_atomic(directory / f"{doc['stage']}.json", body, mode=0o600)

    def read_stage(self, stage: str) -> dict[str, Any] | None:
        """The stage file, or ``None`` when it is missing, not the host's own, readable by others, too big or not a
        stage document (read without following links)."""
        if not isinstance(stage, str) or _STAGE.fullmatch(stage) is None:
            return None
        path = self.dir / "staged" / f"{stage}.json"
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except OSError:
            return None
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != self.host_uid or st.st_mode & 0o077:
                return None
            if st.st_size > MAX_STAGE_BYTES:
                return None
            raw = f.read(MAX_STAGE_BYTES + 1)
        try:
            doc = json.loads(raw)
        except ValueError:
            return None
        ok = isinstance(doc, dict) and doc.get("v") == 1 and doc.get("stage") == stage
        return doc if ok and isinstance(doc.get("name"), str) and isinstance(doc.get("files"), dict) else None

    def stages(self) -> list[dict[str, Any]]:
        try:
            names = sorted(os.listdir(self.dir / "staged"))
        except OSError:
            return []
        out = []
        for n in names:
            doc = self.read_stage(n.removesuffix(".json")) if n.endswith(".json") else None
            if doc is not None:
                out.append(doc)
        return out

    def drop_stage(self, stage: str) -> bool:
        if _STAGE.fullmatch(stage) is None:
            return False
        try:
            os.unlink(self.dir / "staged" / f"{stage}.json")
        except OSError:
            return False
        return True

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
                oslock.flock(fd, oslock.LOCK_EX | oslock.LOCK_NB)
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
