"""``Store``: the only write path (core doc §2 "only store/ writes files"; ticket-format §5.5, §5.8, §5.10).

One workspace directory, one lock, one function that writes events: :meth:`Store.append`.

**Write order** (§5.5), under the workspace lock, for every event:

1. stamp ``seq``, ``ws_seq``, ``at``, ``prev``; validate with ``orch.schema``; ``orch.model.admit`` it against the
   current state with the real verifier; sign ``host_sig`` (file-tier workspace key); compute the next state with
   ``orch.model.advance`` and the projection files it implies (``ticket.json``, ``body.md``, ``keys.jsonl``,
   ``config.json``, artifact files);
2. write those files to ``.state/pending/<event id>/`` and a ``manifest.json`` (the event line included), fsync;
3. append the event line to its log and fsync (**this is the commit**);
4. rename the pending files into place, fsync the directories, copy the installed ``body.md`` to
   ``.state/body/<uid>.md``, write ``.state/applied``, delete the pending directory;
5. (derived, after the commit) checkpoints and ``.state/index.sqlite``.

**Crash recovery** runs at the next lock (``_recover_pending``): a pending directory without a complete manifest is
deleted; with a manifest, the log decides: the line is in the log, so step 4 is finished (a file that matches
neither the pending copy nor the manifest hash is reported as ``store.torn_write`` and the ordinary external-edit
check then rebuilds ``ticket.json`` and records ``edit.external`` for the ``body.md`` on disk); the line is absent or
a torn prefix of it, so the prefix is cut off the log and the pending files are deleted.

**Readers** never need the lock: every file is replaced by rename and a log only grows, so they see the state before
or after an event. **External edits** (§5.8) are looked for when the store opens, in :meth:`Store.scan`, and for the
ticket an event is about, right before it is appended.

Decisions where F1 is silent are listed in the module docstrings (``render``, ``checkpoints``) and the PR.
"""

from __future__ import annotations

import calendar
import contextlib
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISLNK as _S_ISLNK
from typing import Any, Protocol

from orch import canon, crypto, schema
from orch.identity import CryptoVerifier, new_ulid
from orch.model import (
    WORKSPACE,
    Code,
    Refusal,
    State,
    Verifier,
    admit,
    advance,
    at,
    external_edit_voids,
    replay,
)

from . import render
from .checkpoints import WORKSPACE_EVERY, Checkpoints, Divergence, find_divergence
from .errors import StoreError
from .fsio import append_durable, fsync_dir, loads, open_nofollow, read_or_none, replace, tail_bytes, write_atomic
from .index import Index
from .lock import FileLock
from .logs import MAX_LINE, LogInfo, first_line, info_sig, last_line, read_new_lines, stat_sig
from .paths import check_artifact, check_state_dirs, check_uid, safe_join, target_ok
from .pins import HostPins

__all__ = ["Appended", "BackendSigner", "HostSigner", "Report", "Store"]

_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_KEY_NUM = re.compile(r"[A-Z][A-Z0-9]{0,15}-([0-9]+)")
_HOST_FIELDS = ("seq", "at", "prev", "ws_seq", "host_sig")
_EMPTY = canon.section_hash("")
_PLACEHOLDER_SIG = "A" * 85 + "A"
_TICKET_HOST_EVENTS = frozenset({"edit.external", "projection.repaired"})


class HostSigner(Protocol):
    """The workspace key as the store uses it: the public key and a signature over bytes that start with a host label.

    :class:`BackendSigner` adapts a ``file``-tier custody backend; tests and later the host process can bring their own.
    """

    @property
    def public_key(self) -> bytes: ...

    def sign(self, payload: bytes) -> bytes: ...


class BackendSigner:
    """A custody backend (``orch.custody.FileBackend``) and a key id as a :class:`HostSigner`."""

    def __init__(self, backend: Any, key_id: str) -> None:
        self._backend, self._key_id = backend, key_id

    @property
    def public_key(self) -> bytes:
        return self._backend.public_key(self._key_id)

    def sign(self, payload: bytes) -> bytes:
        return self._backend.sign(self._key_id, payload, action="host")


@dataclass(frozen=True)
class Report:
    """Something the store found and dealt with or could not (``store.torn_write`` and friends)."""

    code: str
    detail: str
    log: str | None = None


@dataclass(frozen=True)
class Appended:
    """The result of :meth:`Store.append`: the event as written, the state after it, and whether it was already there.

    ``healed`` lists host events (``edit.external``, ``projection.repaired``) the store appended first because the
    files of the ticket had been edited outside the store."""

    event: dict[str, Any]
    state: State
    duplicate: bool = False
    healed: tuple[dict[str, Any], ...] = ()


@dataclass
class _Write:
    rel: str
    data: bytes
    body_copy: str | None = None  # uid whose .state/body copy follows this body.md


def _epoch(stamp: str) -> int:
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))


def _fmt(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verb_events() -> dict[str, frozenset[str]]:
    """What each operation emits, from the registry: the table that turns a grant's operation names into event types."""
    from orch.ops import verb_events

    return verb_events()


_default_verifier: Callable[[], Verifier] = CryptoVerifier  # test seam (a module attribute, never set from src/)
LOCK_TIMEOUT = 10.0  # seconds a call waits for the workspace lock before it fails with ``store.busy``
FUTURE_SLACK = (
    120  # seconds: an `at` further ahead of the host clock is not believed (honest same-second bumps stay below)
)


@dataclass
class _Hint:
    """What the store knows about a ticket log without reading it: its first and last line, **unverified**. Used to find
    the ticket of a key, to keep the merged order (`at`, `ws_seq`) and keys.jsonl right; never for a decision."""

    sig: tuple[int, int, int, int]  # (size, inode, mtime_ns, ctime_ns) when read
    ino: int
    key: str | None
    created_at: str | None


class Store:
    """A workspace directory. Use :meth:`open`; ``append`` is the only way to add an event.

    **Lazy replay** (ticket-format §2.1): the workspace log is always replayed and verified in full; a ticket log is
    replayed and verified when a call touches it (``ticket``, ``append`` and the helpers that need a ticket), together
    with the tickets it refers to. ``state`` is the state of what is loaded; ``load_all`` (and ``scan``,
    ``rebuild_index``) load everything. Everything that decides something is checked under the lock, against the
    files, never against ``.state`` records."""

    def __init__(
        self,
        root: Path,
        *,
        workspace_id: str,
        expected_genesis: str | None,
        host: HostSigner | None,
        host_state_dir: Path | None,
        verifier: Verifier,
        clock: Callable[[], float],
        workspace_name: str | None,
        lock_timeout: float | None,
    ) -> None:
        self.root = root
        self.workspace_id = workspace_id
        self._expected_genesis = expected_genesis
        self._host = host
        self._verifier = verifier
        self._clock = clock
        self._name = workspace_name
        self.state_dir = root / ".state"
        self._pins = HostPins(host_state_dir, workspace_id) if host_state_dir is not None else None
        self._lock = FileLock(self.state_dir / "lock", timeout=lock_timeout)
        self._checkpoints = Checkpoints(self.state_dir / "checkpoints", workspace_id)
        self._index = Index(self.state_dir / "index.sqlite")
        self.reports: list[Report] = []
        self._logs: dict[str, LogInfo] = {}
        self._loaded: set[str] = set()
        self._state: State | None = None
        self._read_errors: dict[str, str] = {}
        self._diverged: dict[str, Divergence] = {}
        self._genesis_event: dict[str, Any] | None = None
        self._hints: dict[str, _Hint] = {}
        self._key_uid: dict[str, str] = {}
        self._sizes: dict[str, int] = {}
        self._pin: str | None = expected_genesis
        self._json_cache: dict[str, tuple[Any, bytes]] = {}
        self._applied_n = 0
        self._ws_last_at = 0
        self._loaded_at = 0
        self._loaded_pos: tuple[int, int, str, int] | None = None
        self._last_ok: dict[str, tuple[tuple[int, int], tuple[int, int, str, int] | None]] = {}
        self._own_pos: tuple[int, int, str, int] | None = None
        self._own_at = 0
        self._needs_reload = False
        self._torn: set[str] = set()
        self._bad_projection: dict[str, str] = {}
        self._ticket_appends_since_cp = 0
        self._healing = False
        self._grant_hashes: dict[str, str] = {}
        self._index_ok = False
        self._exists: set[str] = set()
        self._pending_revs: list[dict[str, Any]] = []
        self._ws_restore_at = ""
        self._created_ok: dict[
            str, tuple[tuple[int, int, int, int], str]
        ] = {}  # uid -> (log signature, key) of a verified first line
        self._unverified: set[str] = set()  # ticket logs without a verified creation line
        self._suspect = False  # duplicate or missing keys among the hints
        self._hint_mismatch = False  # a verified ticket whose key its hint did not say

    # ------------------------------------------------------------------ opening

    @classmethod
    def open(
        cls,
        path: str | os.PathLike[str],
        *,
        expected_workspace_id: str,
        expected_genesis: str | None = None,
        host: HostSigner | None = None,
        host_state_dir: str | os.PathLike[str] | None = None,
        clock: Callable[[], float] = time.time,
        workspace_name: str | None = None,
        load: str = "lazy",
        lock_timeout: float | None = LOCK_TIMEOUT,
    ) -> Store:
        """Open (and, for a new directory, prepare) the workspace at ``path``.

        ``expected_workspace_id`` and ``expected_genesis`` are the pins: the id from ``config.json`` and the genesis
        head from ``<host state dir>/hosts/<workspace_id>/genesis`` (§5.11). ``expected_genesis=None`` means "use the
        pin file in ``host_state_dir`` if there is one, otherwise trust the genesis the logs hold and pin it" (trust on
        first use). A store that can write (``host`` given) **needs** ``host_state_dir``: without it there would be no
        pin and nowhere to keep revocations (``validation.host_state``). Replay uses both pins for every read.
        ``host`` is the workspace key signer; without it the store is read-only (``append`` raises
        ``store.read_only``). ``load="lazy"`` (default) replays the workspace log now and tickets when touched;
        ``load="all"`` replays every ticket and looks for external edits on the way in (see :meth:`scan`); what the
        store did is in ``reports``. A call that waits longer than ``lock_timeout`` seconds for the workspace lock
        raises ``store.busy``.
        """
        if host is not None and host_state_dir is None:
            raise StoreError("validation.host_state", "a store that can write needs host_state_dir (pin, revocations)")
        if load not in ("lazy", "all"):
            raise StoreError("validation.load", "load is 'lazy' or 'all'")
        root = Path(path)
        store = cls(
            root,
            workspace_id=expected_workspace_id,
            expected_genesis=expected_genesis,
            host=host,
            host_state_dir=Path(host_state_dir) if host_state_dir is not None else None,
            verifier=_default_verifier(),
            clock=clock,
            workspace_name=workspace_name,
            lock_timeout=lock_timeout,
        )
        store._open(load == "all")
        return store

    def _open(self, eager: bool) -> None:
        for d in ("events", "tickets", ".state"):
            if os.path.islink(self.root / d):
                raise StoreError("validation.path", f"{d} is a symlink")
            (self.root / d).mkdir(parents=True, exist_ok=True)
        cfg = read_or_none(self.root / "config.json")
        if cfg is not None:
            try:
                have = loads(cfg)["workspace"]["id"]
            except (ValueError, KeyError, TypeError):
                have = None
            if have is not None and have != self.workspace_id:
                raise StoreError("trust.genesis_mismatch", "config.json belongs to another workspace")
        with self._lock:
            check_state_dirs(self.root)
            pend = self._recover_pending()
            self._scan_dirs()
            if eager:
                self._loaded = set(self._exists)
            self._loaded |= {m["log"] for _, m in pend if m["log"] != WORKSPACE}
            self._load()
            self._finish_pending(pend)
            self._enforce_revocations()
            if self._host is not None:
                self._after_open_heal(eager)
            self._maybe_index()

    def close(self) -> None:
        self._index.close()

    def refresh(self) -> None:
        """Take the lock and re-read what changed on disk since the last call: what a long-lived caller does before it
        decides anything from :attr:`state`."""
        with self._locked():
            pass

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the workspace lock (re-entrant) with the state freshly read: for a caller that judges and then appends
        several events and must not let another writer in between (an atomic batch)."""
        with self._locked():
            yield

    def events(self, ref: str, *, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        """The events of the ticket ``ref`` with ``seq`` above ``after`` (at most ``limit``), parsed from the log that
        was replayed and verified. ``[]`` if there is no such ticket. Read-only; the lines are not re-verified here."""
        with self._locked():
            uid = self._uid_of(ref)
            if uid is None:
                return []
            self._ensure({uid})
            info = self._logs.get(uid)
            if info is None or uid in self._read_errors:
                return []
            last = info.seq if limit is None else min(info.seq, after + limit)
            return [self._read_event(uid, n) for n in range(max(after, 0) + 1, last + 1)]

    @property
    def can_write(self) -> bool:
        """Does this store hold the workspace key (it can append), or only read?"""
        return self._host is not None

    def host_append(self, typ: str, ref: str, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Append the host's observation of a repository (``branch.pushed``) to the ticket ``ref`` and, **under the same
        lock**, the ``gate.invalidated`` records it makes owed (see :meth:`flush_invalidations`). Returns the events."""
        if typ != "branch.pushed":
            raise StoreError("validation.event", f"{typ} is not an event a caller appends in the host's name")
        with self._locked():
            uid = self._uid_of(ref)
            if uid is None:
                raise StoreError("ticket.unknown", ref)
            self._ensure({uid})
            out = [self._host_append(typ, uid, dict(payload))]
            return out + self._flush(uid)

    def flush_invalidations(self, ref: str) -> list[dict[str, Any]]:
        """Append a host ``gate.invalidated`` for every gate that still has approvals the model voided without a record
        (after a ``branch.pushed``, a crash between the two appends, a changed section...). ``voided`` is exactly what
        the model derived (``pending_void``, read from the state after the last append), never a caller's guess."""
        with self._locked():
            uid = self._uid_of(ref)
            if uid is None:
                return []
            self._ensure({uid})
            return self._flush(uid)

    def _flush(self, uid: str) -> list[dict[str, Any]]:
        assert self._state is not None
        out = []
        for g in ("requirements", "plan", "verify", "code"):
            t = self._state._core.tickets.get(uid)
            ids = sorted(t.gates[g].pending_void) if t is not None else []
            if ids:
                cause = "new_commits" if g in ("verify", "code") else "content_changed"
                out.append(self._host_append("gate.invalidated", uid, {"gate": g, "cause": cause, "voided": ids}))
        return out

    def attempt_logged(self, ref: str, idem: str) -> bool:
        """Did an append with this idempotency key reach the log of ticket ``ref``? (The log confirms the record.)"""
        with self._locked():
            uid = self._uid_of(ref)
            rec = self._read_intent(idem)
            if uid is None or rec is None or rec["log"] != uid:
                return False
            self._ensure({uid})
            s = rec.get("seq")
            info = self._logs.get(uid)
            if isinstance(s, int) and info is not None and 1 <= s <= info.seq and info.heads[s - 1] == rec.get("head"):
                return True
            return self._seq_of_id(uid, rec["id"]) is not None

    def peek_stamp(self, log: str) -> dict[str, Any]:
        """The ``seq``, ``prev``, ``at`` and ``ws_seq`` the next event of ``log`` would be stamped with, without
        appending anything: what a caller needs to plan a chain of events with ``orch.model.preview``."""
        with self._locked():
            ev: dict[str, Any] = {"type": "log.added"}
            self._stamp(ev, log)
            return {k: ev[k] for k in ("seq", "prev", "at") + (() if log == WORKSPACE else ("ws_seq",))}

    def raw_matches(self, needle: str) -> list[str]:
        """The uids whose ``ticket.json`` or ``body.md`` contains ``needle`` (case-insensitive), read as bytes from the
        files without replaying anything: **candidates only**. A caller verifies (loads) a ticket before it prints
        anything from it."""
        with self._locked():
            self._scan_dirs()
            want = needle.lower()
            out = []
            for uid in sorted(self._exists):
                for name in ("ticket.json", "body.md"):
                    raw = read_or_none(safe_join(self.root, f"tickets/{check_uid(uid)}/{name}"))
                    if raw and want in raw.decode("utf-8", "ignore").lower():
                        out.append(uid)
                        break
            return out

    def peek_key(self) -> str:
        """The key the next ``create_ticket`` would allocate (a dry run shows it; another writer may take it first)."""
        with self._locked():
            self._scan_dirs()
            self._verified_keys()
            return self._next_key()

    # ------------------------------------------------------------------ small helpers

    def _now(self) -> str:
        return _fmt(int(self._clock()))

    @property
    def state(self) -> State:
        """The derived state now (claims, leases and grants judged by the clock): the workspace and the **loaded**
        tickets, as of the last call that took the lock. Use :meth:`ticket` or :meth:`load_all` to get a ticket."""
        assert self._state is not None
        return at(self._state, self._now())

    @property
    def genesis(self) -> str | None:
        assert self._state is not None
        return self._state.workspace.genesis

    @property
    def diverged(self) -> Mapping[str, Divergence]:
        return dict(self._diverged)

    def chain_errors(self) -> list[tuple[str, int, str, str]]:
        """Every broken log that was read, as ``(log, seq, code, detail)``: unreadable lines, and the model's report.
        A ticket log that is not loaded has not been checked (``load_all`` checks them all)."""
        out = [(log, self._logs[log].error_seq, "chain.broken", msg) for log, msg in sorted(self._read_errors.items())]
        out += [(c.log, c.seq, c.code, c.detail) for c in self.state.chain_errors if c.log not in self._read_errors]
        return out

    def head_seq(self, ref: str) -> int:
        """The seq of the last event of the ticket ``ref`` (a key, a number or a uid), 0 if there is none: the ticket
        head in the dedup key. Takes the lock and loads the ticket (it is checked, not trusted from a hint)."""
        with self._locked():
            if ref == WORKSPACE:
                return self._logs[WORKSPACE].seq
            uid = self._uid_of(ref)
            if uid is None:
                return 0
            self._ensure({uid})
            info = self._logs.get(uid)
            return info.seq if info else 0

    def log_head(self, log: str) -> str | None:
        info = self._logs.get(log)
        return info.head if info else None

    def log_path(self, log: str) -> Path:
        return self._path_of(log)

    def _path_of(self, log: str) -> Path:
        if log == WORKSPACE:
            return safe_join(self.root, "events/workspace.jsonl")
        return safe_join(self.root, f"tickets/{check_uid(log)}/events.jsonl")

    def _report(self, code: str, detail: str, log: str | None = None) -> None:
        r = Report(code, detail, log)
        if r not in self.reports:
            self.reports.append(r)

    # ------------------------------------------------------------------ the directory scan (hints)

    def _scan_dirs(self) -> None:
        """List the ticket logs and refresh the hints of those whose size or inode changed (about a stat per ticket)."""
        tdir = self.root / "tickets"
        seen: dict[str, _Hint] = {}
        sizes: dict[str, int] = {}
        try:
            entries = list(os.scandir(tdir))
        except FileNotFoundError:
            entries = []
        for ent in entries:
            uid = ent.name
            if not _ULID.fullmatch(uid) or ent.is_symlink():
                continue
            try:  # one lstat per ticket
                st = os.lstat(ent.path + "/events.jsonl")
            except OSError:
                continue
            if _S_ISLNK(st.st_mode):
                continue
            sig = (st.st_size, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)
            sizes[uid] = sig[0]
            old = self._hints.get(uid)
            if old is not None and old.sig == sig:
                seen[uid] = old
                continue
            path = Path(ent.path) / "events.jsonl"
            key = created = None
            if old is not None and old.ino == sig[1]:
                key, created = old.key, old.created_at
            else:
                first = first_line(path)
                try:
                    f = loads(first) if first else {}
                    if isinstance(f, dict) and f.get("type") == "ticket.created":
                        key, created = f.get("key"), f.get("at")
                except ValueError:
                    pass
            seen[uid] = _Hint(sig, sig[1], key if isinstance(key, str) else None, created)
        self._hints, self._sizes = seen, sizes
        self._exists = set(seen)
        self._key_uid = {h.key: u for u, h in seen.items() if h.key}
        self._suspect = len(self._key_uid) != len(seen)  # a log without a readable first line, or two with one key

    def _order(self) -> tuple[int, tuple[int, int, str, int] | None]:
        """The latest `at` and merged position of what was **verified** (the workspace log, the loaded tickets, our own
        appends). Unloaded tickets do not count here; see :meth:`_global_floor` for the one case that asks them."""
        pos = max([p for p in (self._loaded_pos, self._own_pos) if p], default=None)
        return max(self._ws_last_at, self._loaded_at, self._own_at), pos

    def _global_floor(self, wsh: int) -> tuple[int, str, int] | None:
        """After ``admit`` refused an order: the latest position of **every** ticket log, taken from last lines whose
        ``host_sig`` verifies (cached by size and inode), ignoring a ``ws_seq`` above the verified workspace head and an
        ``at`` later than the host clock (that log is reported, nothing is adopted)."""
        self._scan_dirs()
        wsk = self._wsk_pub()
        limit = int(self._clock()) + FUTURE_SLACK
        best: tuple[int, int, str, int] | None = None
        for uid, h in self._hints.items():
            hit = self._last_ok.get(uid)
            if hit is None or hit[0] != h.sig:
                pos = None
                line = last_line(safe_join(self.root, f"tickets/{uid}/events.jsonl"))
                try:
                    e = canon.parse_event_line(line) if line else None
                    if (
                        e is not None
                        and wsk is not None
                        and self._verifier.verify_host(e, log=uid, wsk_pub=wsk, workspace_id=self.workspace_id)
                    ):
                        pos = (int(e["ws_seq"]), _epoch(e["at"]), uid, int(e["seq"]))
                except (canon.HashError, KeyError, TypeError, ValueError):
                    pos = None
                hit = self._last_ok[uid] = (h.sig, pos)
            pos = hit[1]
            if pos is None or pos[0] > wsh:
                continue
            if pos[1] > limit:
                self._report("store.torn_write", f"the last event of {uid} is dated after the host clock", uid)
                continue
            best = pos if best is None or pos > best else best
        return best

    # ------------------------------------------------------------------ loading

    def _read_refs(self, events: list[dict[str, Any]]) -> set[str]:
        """Ticket keys the events of one ticket refer to (parent, blocked_by, duplicate_of): the tickets that must be
        replayed with it, because the model checks them against the creation positions."""
        keys: set[str] = set()
        for e in events:
            if e["type"] == "ticket.updated":
                s = e.get("set", {})
                if isinstance(s.get("ticket.parent"), str):
                    keys.add(s["ticket.parent"])
                keys.update(k for k in s.get("ticket.blocked_by", []) if isinstance(k, str))
            elif e["type"] == "ticket.closed" and isinstance(e.get("duplicate_of"), str):
                keys.add(e["duplicate_of"])
        return keys

    def _load(self) -> None:
        """Read the workspace log and the loaded ticket logs (and the tickets they refer to) from disk, replay with the
        real verifier and both pins, and rebuild every cache."""
        pin = self._pins.genesis() if self._pins is not None else None
        if self._expected_genesis is not None and pin is not None and pin != self._expected_genesis:
            raise StoreError("trust.genesis_mismatch", "the genesis pin file differs from the genesis given")
        self._pin = self._expected_genesis or pin
        self._scan_dirs()  # who exists now (a stat per ticket); a reload is not the hot path
        self._logs = {WORKSPACE: LogInfo(WORKSPACE, self._path_of(WORKSPACE))}
        self._read_errors = {}
        ws_events = read_new_lines(self._logs[WORKSPACE])
        tickets: dict[str, list[dict[str, Any]]] = {}
        todo = sorted(u for u in self._loaded if u in self._exists)
        while todo:
            uid = todo.pop()
            if uid in tickets:
                continue
            info = LogInfo(uid, safe_join(self.root, f"tickets/{uid}/events.jsonl"))
            tickets[uid] = read_new_lines(info)
            self._logs[uid] = info
            refs = self._read_refs(tickets[uid])
            if not refs:
                continue
            owners = self._verified_keys()  # key -> uid by verified creation lines, not by hints
            if self._unsure() or any(k not in owners for k in refs):
                todo.extend(u for u in self._exists if u not in tickets)  # doubt: every ticket, then the replay decides
                continue
            todo.extend(owners[k] for k in refs if owners[k] not in tickets)
        self._loaded = set(tickets)
        for name, info in self._logs.items():
            if info.error:
                self._read_errors[name] = info.error
        self._genesis_event = ws_events[0] if ws_events and ws_events[0]["type"] == "workspace.created" else None
        self._state = replay(
            ws_events,
            tickets,
            verifier=self._verifier,
            now=self._now(),
            expected_workspace_id=self.workspace_id,
            expected_genesis=self._pin,
            verb_events=_verb_events(),
        )
        ws = self._state.workspace
        if ws_events and ws.genesis is None:
            detail = next((c.detail for c in self._state.chain_errors if c.log == WORKSPACE), "untrusted genesis")
            raise StoreError("trust.genesis_mismatch", detail)
        if ws.genesis is not None and self._pin is None and self._pins is not None:
            self._pins.pin_genesis(ws.genesis)
        if ws.genesis is not None:
            self._pin = self._pin or ws.genesis
        for uid in tickets:
            v, h = self._state.tickets.get(uid), self._hints.get(uid)
            if v is not None and h is not None and h.key != v.key and h.sig != (0, 0, 0, 0):
                self._hint_mismatch = True  # a first line said another key than the verified ticket has
        self._ws_last_at = max((_epoch(e["at"]) for e in ws_events), default=0)
        lasts = [(e["ws_seq"], _epoch(e["at"]), u, e["seq"]) for u, evs in tickets.items() for e in evs[-1:]]
        self._loaded_pos = max(lasts, default=None)
        self._loaded_at = max((p[1] for p in lasts), default=0)
        # a grant's secret hash counts only from a grant.issued the model accepted (not invalid, and the grant exists)
        invalid = {i.id for i in ws.invalid}
        self._grant_hashes = {}
        for e in ws_events:
            if e["type"] == "grant.issued" and e["id"] not in invalid and e["grant"] in ws.grants:
                self._grant_hashes.setdefault(e["grant"], e["secret_hash"])
        self._json_cache = {}
        try:
            applied = loads(read_or_none(self.state_dir / "applied") or b"{}")
            self._applied_n = int(applied.get("n", 0))
        except (ValueError, TypeError, AttributeError):
            self._applied_n = 0
        self._needs_reload = False
        self._diverged = {}
        wsk = self._wsk_pub()
        if wsk is not None:
            for d in find_divergence(self._checkpoints, wsk, ws.genesis, self._logs, self._exists):
                if d.code == "trust.genesis_mismatch":
                    raise StoreError(d.code, d.detail)
                self._diverged[d.log] = d
        self._index_ok = ws.genesis is not None and self._index.is_current(
            self._sizes_now(), self.workspace_id, ws.genesis
        )
        self._ws_restore_at = max(
            (e["at"] for e in ws_events if e["type"] == "restore" and e["id"] not in invalid), default=""
        )
        self._pending_revs = self._revocation_plan()

    def _sizes_now(self) -> dict[str, int]:
        sizes = dict(self._sizes)
        ws = stat_sig(self._path_of(WORKSPACE))
        sizes[WORKSPACE] = ws[0] if ws else 0
        return sizes

    def _wsk_pub(self) -> bytes | None:
        if self._genesis_event is None:
            return None
        return crypto.unb64u(self._genesis_event["wsk_pub"], crypto.PUB_LEN)

    def _fresh(self) -> bool:
        """Do the files still say what was read? Compared under the lock: size and inode of the workspace log and of
        every loaded ticket log. ``.state/applied`` plays no part (it is a record, not a signal)."""
        for info in self._logs.values():
            if stat_sig(info.path) != info_sig(info):
                return False
        return True

    def _ensure(self, uids: set[str]) -> None:
        """Make sure these tickets (and the tickets they refer to) are replayed and checked."""
        if any(u not in self._exists for u in uids):
            self._scan_dirs()  # a ticket another process created since the last scan
        need = {u for u in uids if u in self._exists and u not in self._loaded}
        if need:
            self._loaded |= need
            self._load()
            self._enforce_revocations()

    def load(self, uids: Iterable[str]) -> None:
        """Replay and check these tickets in one go (one at a time each load replays everything loaded again)."""
        with self._locked():
            self._ensure({u for u in uids if isinstance(u, str)})

    def load_all(self) -> None:
        """Replay and check every ticket log (cold cost: every event is verified)."""
        with self._locked():
            self._ensure(set(self._exists))

    def ticket(self, ref: str, *, heal: bool = True) -> Any | None:
        """The view of the ticket ``ref`` (a key, ``43``, a uid), loading and checking it first; ``None`` if there is no
        such ticket. With ``heal`` (and a writable store) edits made outside the store are answered first (§5.8)."""
        with self._locked():
            uid = self._uid_of(ref)
            if uid is None:
                return None
            self._ensure({uid})
            if heal and self._host is not None and uid not in self._read_errors and uid not in self._diverged:
                try:
                    self._heal_ticket(uid)
                except StoreError as e:
                    self._report(e.code, e.detail, uid)
            return self.state.tickets.get(uid)  # at the current clock: claims, leases and grants lapse by it

    def _index_fresh(self) -> Index:
        """The index, current: one that matches the logs' sizes is taken as it is (a hint, never a decision); if it is
        not, every ticket is loaded and it is rebuilt."""
        with self._locked():
            ws = self._state.workspace if self._state else None
            if ws is not None and ws.genesis is not None and not self._index_ok:
                if self._index.is_current(self._sizes_now(), self.workspace_id, ws.genesis):
                    self._index_ok = True
                else:
                    self._ensure(set(self._exists))
                    assert self._state is not None
                    self._index.rebuild(self._state, self._logs, self.workspace_id, ws.genesis, self._sizes_now())
                    self._index_ok = True
        return self._index

    def _maybe_index(self, state: State | None = None) -> None:
        """Build the index now if it is missing or stale and everything is loaded anyway (new or ``load="all"``)."""
        state = state or self._state
        if state is None or state.workspace.genesis is None or self._index_ok or not self._loaded >= self._exists:
            return
        try:
            self._index.rebuild(state, self._logs, self.workspace_id, state.workspace.genesis, self._sizes_now())
            self._index_ok = True
        except Exception:  # noqa: BLE001 - derived data
            self._index.drop()

    def rebuild_index(self) -> None:
        """Drop and rebuild ``.state/index.sqlite`` from the logs (it is derived data)."""
        with self._locked():
            self._index_ok = False
            self._index_fresh()

    @property
    def index(self) -> Index:
        """The derived index. First use after it went stale loads every ticket to rebuild it; it is never read for a
        decision."""
        return self._index_fresh()

    # ------------------------------------------------------------------ locking, sync, recovery

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            check_state_dirs(self.root)
            pend = self._recover_pending()
            self._loaded |= {m["log"] for _, m in pend if m["log"] != WORKSPACE}
            reloaded = bool(pend) or self._needs_reload or not self._fresh()
            if reloaded:
                self._load()
            self._finish_pending(pend)
            if reloaded:
                self._enforce_revocations()
            yield

    def _recover_pending(self) -> list[tuple[Path, dict[str, Any]]]:
        """Phase 1 of crash recovery (no state needed): a pending directory without a manifest that describes its own
        event is deleted; a torn prefix of its line is cut off the log; a manifest whose line is the last line of its
        log (and that ``.state/applied`` does not name yet) is returned, to be finished once the log is replayed."""
        pdir = self.state_dir / "pending"
        if not pdir.is_dir():
            return []
        found = sorted(pdir.iterdir())
        if not found:
            return []  # the common case costs one directory listing
        out: list[tuple[Path, dict[str, Any]]] = []
        for d in found:
            m = self._recover_one(d)
            if m is not None:
                out.append((d, m))
        tdir = self.root / "tickets"
        for p in tdir.iterdir() if tdir.is_dir() else []:  # a ticket directory whose event never landed
            with contextlib.suppress(OSError):
                if p.is_dir() and not p.is_symlink() and not (p / "events.jsonl").exists() and not any(p.iterdir()):
                    p.rmdir()
        return out

    def _recover_one(self, d: Path) -> dict[str, Any] | None:
        try:
            m = loads(read_or_none(d / "manifest.json", 2_000_000) or b"")
            line = m["line"].encode("utf-8")
            log = m["log"]
            path = self._path_of(log)
            check_uid(m["id"])
            e = canon.parse_event_line(line)
            if e.get("id") != m["id"] or e.get("seq") != m["seq"] or not isinstance(m["files"], list):
                raise ValueError("the manifest does not describe its own event")
        except (OSError, ValueError, KeyError, StoreError, TypeError, canon.HashError):
            shutil.rmtree(d, ignore_errors=True)  # the manifest is written last: the event was never appended
            return None
        size, tail = tail_bytes(path, len(line) + 1)
        if tail.endswith(line):
            applied = self._read_applied()
            if applied.get("id") == m["id"]:
                shutil.rmtree(d, ignore_errors=True)  # already installed (§5.5: recovery runs only for an event that
                return None  # `.state/applied` does not name)
            self._needs_reload = True
            return m
        for k in range(min(len(line) - 1, size), 0, -1):  # a torn prefix of our own line: cut it off
            if tail[-k:] == line[:k] and (size == k or tail[-k - 1] == 0x0A):
                with open(path, "r+b") as f:
                    f.truncate(size - k)
                    os.fsync(f.fileno())
                self._needs_reload = True
                break
        shutil.rmtree(d, ignore_errors=True)
        return None

    def _read_applied(self) -> dict[str, Any]:
        try:
            a = loads(read_or_none(self.state_dir / "applied") or b"{}")
            return a if isinstance(a, dict) else {}
        except ValueError:
            return {}

    def _finish_pending(self, pend: list[tuple[Path, dict[str, Any]]]) -> None:
        """Phase 2 of crash recovery: install the pending files of an event that is in the (replayed) log. A file is
        installed only if it is exactly what the log says it should be (``ticket.json`` is rebuilt from the state, a
        body must have the log's section hashes, an artifact the log's digest); otherwise it is ``store.torn_write``
        and the ordinary external-edit check answers what is on disk. Nothing the manifest says is believed."""
        for d, m in pend:
            log = m["log"]
            info = self._logs.get(log)
            line = m["line"].encode("utf-8")
            ok = (
                info is not None
                and 1 <= m["seq"] <= info.seq
                and info.heads[m["seq"] - 1] == canon.event_head(canon.parse_event_line(line))
            )
            if not ok:  # the log up to that line did not verify: nothing to finish
                self._report("store.torn_write", f"event {m['id']} is not in the verified part of {log}", log)
                shutil.rmtree(d, ignore_errors=True)
                continue
            torn = self._install(m, d, check=self._expected_bytes)
            for rel in torn:
                self._torn.add(rel)
                self._report("store.torn_write", f"{rel} did not survive the crash of event {m['id']}", log)
            shutil.rmtree(d, ignore_errors=True)

    def _expected_bytes(self, rel: str, data: bytes) -> bool:
        """Is ``data`` what the replayed log says the file ``rel`` holds (recovery only)?"""
        assert self._state is not None
        m = re.fullmatch(r"tickets/([0-7][0-9A-HJKMNP-TV-Z]{25})/(ticket\.json|body\.md|artifacts/.+)", rel)
        if m is None:
            if rel == "config.json":
                return data == self._config_bytes(self._state)
            if rel == "keys.jsonl":
                return data == self._keys_bytes()
            return False
        uid, what = m.groups()
        view = self._state.tickets.get(uid)
        if view is None:
            return False
        if what == "ticket.json":
            return data == self._ticket_bytes(uid)
        if what == "body.md":
            try:
                texts = render.parse_body(data, view.type)
            except render.BodyError:
                return False
            return self._hashes(texts) == self._log_hashes(view) and data == render.render_body(
                view.type, texts
            ).encode("utf-8")
        art = view.artifacts and next((a for a in view.artifacts if a.name == what[len("artifacts/") :]), None)
        return bool(art and art.digest == canon.artifact_digest(data))

    def _install(self, m: dict[str, Any], d: Path, check: Callable[[str, bytes], bool] | None = None) -> list[str]:
        """Step 4 of the write order (idempotent). Returns the relative paths that could not be installed. With
        ``check`` (recovery) a pending file is installed only if ``check(rel, data)``."""
        torn: list[str] = []
        for f in m["files"]:
            dest, src = safe_join(self.root, target_ok(f["to"])), d / str(int(f["n"]))
            data = read_or_none(src, 256 * 1024 * 1024)
            if data is not None and _sha(data) == f["sha256"] and (check is None or check(f["to"], data)):
                dest.parent.mkdir(parents=True, exist_ok=True)
                replace(src, dest)
                fsync_dir(dest.parent)
            else:
                now = read_or_none(dest)
                if now is None or _sha(now) != f["sha256"] or (check is not None and not check(f["to"], now)):
                    torn.append(f["to"])
        uid = m.get("body_copy")
        if uid:
            check_uid(uid)
            body = read_or_none(safe_join(self.root, f"tickets/{uid}/body.md"))
            if body is not None and f"tickets/{uid}/body.md" not in torn:
                write_atomic(self.state_dir / "body" / f"{uid}.md", body)
        self._write_applied(m["id"], m["log"], m["seq"])
        return torn

    def _write_applied(self, event_id: str, log: str, seq: int) -> None:
        self._applied_n += 1
        raw = canon.cj_checked({"id": event_id, "log": log, "n": self._applied_n, "seq": seq}) + b"\n"
        write_atomic(self.state_dir / "applied", raw)

    # ------------------------------------------------------------------ reading projections

    def _ticket_bytes(self, uid: str, view: Any = None) -> bytes:
        assert self._state is not None
        view = view or self._state.tickets[uid]
        hit = self._json_cache.get(uid)
        if hit is not None and hit[0] is view:
            return hit[1]
        data = render.render_ticket(uid, view.key, view.fields)
        self._json_cache[uid] = (view, data)
        return data

    def ticket_json(self, uid: str) -> bytes | None:
        """The ``ticket.json`` on disk (None if there is none)."""
        return read_or_none(safe_join(self.root, f"tickets/{check_uid(uid)}/ticket.json"))

    def body_sections(self, uid: str) -> dict[str, str]:
        """The section texts of the ticket's ``body.md`` as on disk, **checked against the log**: every section's hash
        must be the one the log holds, otherwise ``store.torn_write`` is raised (nothing forged is served). Call
        :meth:`ticket` or :meth:`scan` first to have edits made outside the store answered with host events."""
        with self._locked():
            self._ensure({check_uid(uid)})
            return self._body_sections(uid)

    def _body_sections(self, uid: str) -> dict[str, str]:
        assert self._state is not None
        view = self._state.tickets[uid]
        raw = read_or_none(safe_join(self.root, f"tickets/{check_uid(uid)}/body.md"))
        if raw is None:
            texts: dict[str, str] = {}
        else:
            try:
                texts = render.parse_body(raw, view.type)
            except render.BodyError as e:
                raise StoreError("validation.body", str(e)) from None
        if self._hashes(texts) != self._log_hashes(view):
            raise StoreError("store.torn_write", f"body.md of {uid} does not match the log (sections differ)")
        return texts

    def _next_key(self) -> str:
        """The next ticket key: one above the highest number in ``keys.jsonl`` or in any ``ticket.created`` (§2). Only
        called under the lock (``create_ticket``), after the directory scan."""
        assert self._state is not None
        # a key is taken if a verified creation, a loaded ticket, or (additively) a hint or keys.jsonl says so; a hint
        # or a file can only make fewer keys free, never one more
        best = 0
        for key in (*self._verified_keys(), *self._key_uid, *(t.key for t in self._state.tickets.values())):
            m = _KEY_NUM.fullmatch(key)
            best = max(best, int(m.group(1)) if m else 0)
        for line in (read_or_none(self.root / "keys.jsonl") or b"").splitlines():
            with contextlib.suppress(ValueError, KeyError, TypeError):
                m = _KEY_NUM.fullmatch(loads(line)["key"])
                best = max(best, int(m.group(1)) if m else 0)
        return f"{self._state.workspace.prefix}-{best + 1:04d}"

    def _verified_keys(self) -> dict[str, str]:
        """key -> uid for every ticket whose creation line (the first line of its log) is a well-formed
        ``ticket.created`` with a valid ``host_sig``, checked here without replaying the ticket (about one signature
        check per ticket, cached by inode). Key ownership is decided from this, never from a hint, ``keys.jsonl`` or the
        index."""
        wsk = self._wsk_pub()
        out: dict[str, str] = {}
        dup: set[str] = set()
        if wsk is None:
            self._unverified = set(self._hints)
            return out
        for uid, h in self._hints.items():
            hit = self._created_ok.get(uid)
            if hit is None or hit[0] != h.sig:
                hit = None
                line = first_line(safe_join(self.root, f"tickets/{uid}/events.jsonl"))
                try:
                    e = canon.parse_event_line(line) if line else None
                    if (
                        e is not None
                        and e.get("type") == "ticket.created"
                        and e.get("seq") == 1
                        and e.get("prev") is None
                        and isinstance(e.get("key"), str)
                        and self._verifier.verify_host(e, log=uid, wsk_pub=wsk, workspace_id=self.workspace_id)
                    ):
                        hit = (h.sig, e["key"])
                except (canon.HashError, KeyError, TypeError):
                    hit = None
                if hit is None:
                    self._created_ok.pop(uid, None)
                    continue
                self._created_ok[uid] = hit
            if hit[1] in out:
                dup.add(uid)  # two verified creations of one key: the replay (merged order) must decide
            else:
                out[hit[1]] = uid
        self._unverified = (set(self._hints) - set(out.values())) | dup
        return out

    def _unsure(self) -> bool:
        """Can the key hints (first lines of unverified logs) not be believed? Duplicates, missing keys, a verified
        ticket that contradicted its hint, or a log whose creation line did not verify (after ``_verified_keys``)."""
        return self._suspect or self._hint_mismatch or bool(self._unverified)

    def _resolve(self, ref: str) -> str | None:
        """The uid of the ticket ``ref``, from **verified** state only: a hint names a candidate, the candidate is
        replayed and checked (its verified key must be ``ref``); anything doubtful loads every ticket and looks there.
        Under the lock."""
        r = self._text_ref(ref)
        assert self._state is not None
        if _ULID.fullmatch(r):
            if r not in self._exists:
                return None
            self._ensure({r})
            return r if r in self._state.tickets else None
        owners = self._verified_keys()  # key -> uid by host_sig-checked creation lines (hints only ever say less)
        if not self._unsure():
            cand = owners.get(r)
            if cand is None:
                return None  # every log has a verified creation and none carries this key
            self._ensure({cand})
            v = self._state.tickets.get(cand)
            if v is not None and v.key == r:
                return cand
            self._hint_mismatch = True
        self._ensure(set(self._exists))  # doubt: load everything and let the replay say
        return next((u for u, v in self._state.tickets.items() if v.key == r), None)

    def _text_ref(self, ref: str) -> str:
        """``43``, ``#43``, ``demo-43`` and ``DEMO-0043`` as ``DEMO-0043`` (the workspace prefix is verified state)."""
        assert self._state is not None
        r = ref.strip().lstrip("#")
        prefix = self._state.workspace.prefix
        m = (
            re.fullmatch(r"(?:(?i:" + re.escape(prefix) + r")-)?([0-9]+)", r)
            if prefix and not _ULID.fullmatch(r)
            else None
        )
        if m and int(m.group(1)) >= 1:
            return f"{prefix}-{int(m.group(1)):04d}"
        return r if _ULID.fullmatch(r) else ref

    def normalise_ref(self, ref: str) -> str:
        """``43``, ``#43``, ``demo-43`` and ``DEMO-0043`` are the ticket key ``DEMO-0043`` (text, from the verified
        workspace prefix); a uid becomes the key of the ticket it names, after that ticket was replayed and checked.
        Anything else comes back unchanged. Feeds the stop rule and the dedup key, so it believes nothing unverified."""
        r = self._text_ref(ref)
        if _ULID.fullmatch(r):
            with self._locked():
                uid = self._resolve(r)
                assert self._state is not None
                return self._state.tickets[uid].key if uid else ref
        return r

    def grant_secret_hash(self, grant_id: str) -> str | None:
        """The ``secret_hash`` of an **accepted** ``grant.issued`` for ``grant_id`` (the secret itself is never stored).
        Takes the lock and re-reads what changed, so a grant revoked elsewhere is seen."""
        with self._locked():
            return self._grant_hashes.get(grant_id)

    def _uid_of(self, ref: str) -> str | None:
        return self._resolve(ref)

    def uid_of(self, ref: str) -> str | None:
        """The uid of the ticket ``ref`` (from the directory scan under the lock), None if there is none."""
        with self._locked():
            return self._uid_of(ref)

    # ------------------------------------------------------------------ the one write path

    def append(
        self,
        event: Mapping[str, Any],
        *,
        log: str,
        body: Mapping[str, str | None] | None = None,
        artifacts: Mapping[str, bytes] | None = None,
        idem: str | None = None,
        precommit: Callable[[], None] | None = None,
    ) -> Appended:
        """Append ``event`` to ``log`` (``"workspace"`` or a ticket uid) and return the new state.

        ``event`` is the event without ``seq``, ``at``, ``prev``, ``ws_seq`` and ``host_sig`` (the store stamps them);
        ``v``, ``hash_v`` and, for events nobody signs, ``id`` and ``based_on`` default to the current values. A
        person event arrives complete and signed. ``body`` carries the new text of every section a ``ticket.updated``
        names (the event holds only hashes). ``artifacts`` carries the bytes of a file artifact. ``idem`` makes the
        append idempotent: a second call with the same key returns the first result instead of appending again, even
        after a crash between the append and the caller's own record.

        Raises :class:`StoreError`: the model's refusal ``code``, ``validation.*``, ``chain.broken``,
        ``chain.diverged``, ``store.read_only``.
        """
        with self._locked():
            if precommit is not None:  # runs with the lock held, right before the append: it may raise to refuse
                precommit()
            return self._append(dict(event), log, body, artifacts, idem)

    def create_ticket(
        self,
        *,
        actor: Mapping[str, Any],
        ticket_type: str,
        title: str,
        owner: str,
        uid: str | None = None,
        idem: str | None = None,
    ) -> Appended:
        """Append a ``ticket.created`` for an actor nobody signs (an agent with a grant): allocates the key and the
        uid under the lock, so two processes never take the same number."""
        with self._locked():
            self._scan_dirs()
            self._verified_keys()
            bad = sorted(self._unverified | {u for u in self._diverged if u != WORKSPACE})
            if bad:  # a log with no verified creation line (or a diverged one) might own a key we cannot see
                raise StoreError("chain.broken", f"no new ticket while {bad[0]} has no verified creation or diverged")
            new_uid = uid or self._uid_for_idem(idem) or new_ulid()
            ev = {
                "type": "ticket.created",
                "actor": dict(actor),
                "key": self._next_key(),
                "ticket_type": ticket_type,
                "title": title,
                "owner": owner,
            }
            return self._append(ev, new_uid, None, None, idem)

    # -- steps

    def _check_writable(self, log: str, typ: str) -> None:
        if log != WORKSPACE:
            check_uid(log)
        if self._host is None:
            raise StoreError("store.read_only", "no workspace key signer: this store can only read")
        if log in self._read_errors:
            raise StoreError("chain.broken", f"{log}: {self._read_errors[log]}")
        if WORKSPACE in self._read_errors:
            raise StoreError("chain.broken", f"workspace: {self._read_errors[WORKSPACE]}")
        d = self._diverged.get(log) or (self._diverged.get(WORKSPACE) if log != WORKSPACE else None)
        if d is not None and not (typ == "restore" and d.log == log):
            raise StoreError("chain.diverged", f"{d.log}: {d.detail} (an owner-signed restore is needed)")

    def _envelope(self, event: dict[str, Any], log: str) -> dict[str, Any]:
        bad = [k for k in _HOST_FIELDS if k in event]
        if bad:
            raise StoreError(
                "validation.host_field", f"the store stamps {', '.join(bad)}; the caller must not set them"
            )
        e = event
        if "type" not in e or "actor" not in e:
            raise StoreError("validation.event", "an event needs type and actor")
        person = isinstance(e["actor"], dict) and e["actor"].get("kind") == "person"
        e.setdefault("v", 2)
        e.setdefault("hash_v", 1)
        if "id" not in e:
            if person:
                raise StoreError("validation.event", "a person event comes with its id (the signature covers it)")
            e["id"] = new_ulid()
        info = self._logs.get(log)
        head = info.head if info else None
        if "based_on" not in e:
            if person:
                raise StoreError("validation.event", "a person event comes with based_on (the signature covers it)")
            e["based_on"] = head
        return e

    def _append(
        self,
        e: dict[str, Any],
        log: str,
        body: Mapping[str, str | None] | None,
        artifacts: Mapping[str, bytes] | None,
        idem: str | None,
        *,
        texts: dict[str, str] | None = None,
        force: tuple[str, ...] = (),
    ) -> Appended:
        typ = e.get("type", "")
        self._check_writable(log, typ)
        actor = e.get("actor")
        host_event = isinstance(actor, dict) and actor.get("kind") == "host"
        if not self._healing:
            self._ensure_for(e, log)
        if idem is not None:
            dup = self._idem_hit(idem, log, e)
            if dup is not None:
                return dup
        healed = [] if self._healing else self._heal_for(log, typ, host_event)
        if log in self._bad_projection and not (host_event and typ in _TICKET_HOST_EVENTS):
            raise StoreError(
                "store.torn_write", f"{log}: {self._bad_projection[log]} (restore the files or ask an owner)"
            )
        e = self._envelope(e, log)
        if typ == "restore":
            self._check_restore(log, e)
        new_texts = self._section_texts(e, log, body, texts)
        files = self._file_artifacts(e, artifacts)
        if idem is not None:
            self._idem_intent(idem, log, e["id"])
        result = self._commit(e, log, new_texts, files, force)
        if idem is not None:
            self._idem_done(idem, log, result.event)
        assert self._state is not None
        return Appended(result.event, self._state, False, tuple(healed))

    def _ensure_for(self, e: dict[str, Any], log: str) -> None:
        """Load what the model needs to judge ``e``: its ticket, the tickets it refers to (parent, blocked_by,
        duplicate_of), the ticket that already holds the key of a ``ticket.created`` (so the model refuses a repeat),
        and, for an unattended event (a quota counted over the whole workspace), every ticket."""
        need: set[str] = set() if log == WORKSPACE else {log}
        actor = e.get("actor")
        if (isinstance(actor, dict) and actor.get("unattended") is True) or (
            e.get("type") == "ticket.created" and self._unsure()
        ):
            need |= self._exists
        verified = self._verified_keys() if e.get("type") == "ticket.created" else {}
        for key in self._read_refs([e]) | ({e["key"]} if e.get("type") == "ticket.created" else set()):
            if isinstance(key, str) and key in self._key_uid:
                need.add(self._key_uid[key])
            if key in verified:
                need.add(verified[key])  # the ticket whose verified creation holds the key: the model refuses a repeat
        self._ensure(need)
        self._enforce_revocations()

    def _check_restore(self, log: str, e: dict[str, Any]) -> None:
        want = self.restore_facts(log)["abandoned"]
        if e.get("abandoned") != want:
            raise StoreError("validation.restore", f"abandoned must name the highest checkpoint above from_seq: {want}")

    def _commit(
        self,
        e: dict[str, Any],
        log: str,
        new_texts: dict[str, str] | None,
        files: dict[str, bytes],
        force: tuple[str, ...],
    ) -> Appended:
        assert self._state is not None and self._host is not None
        kind = "workspace" if log == WORKSPACE else "ticket"
        old_state = self._state
        genesis = e["type"] == "workspace.created"
        if genesis and self._host.public_key != crypto.unb64u(e.get("wsk_pub", ""), crypto.PUB_LEN):
            raise StoreError("validation.host_key", "the genesis names a workspace key other than this host's")
        ev: dict[str, Any] = {}
        floor = None
        for attempt in (0, 1):
            ev = dict(e)
            self._stamp(ev, log, floor=floor)
            try:
                schema.validate("event", {**ev, "host_sig": _PLACEHOLDER_SIG}, log=kind)
            except schema.SchemaError as err:
                raise StoreError("validation.event", str(err)) from None
            if genesis:  # the genesis checks its own host_sig (§5.11 step 4), so it is signed before it is admitted
                self._host_sign(ev, log)
            r = admit(old_state, ev, log=log)
            if isinstance(r, Refusal):
                if r.code == Code.CHAIN_BAD_WS_SEQ and attempt == 0 and log != WORKSPACE:
                    floor = self._global_floor(self._logs[WORKSPACE].seq)  # verified last lines of every ticket
                    continue  # the store's own ordering: stamp again, once
                raise StoreError.from_refusal(r)
            break
        if not genesis:
            self._host_sign(ev, log)
        wsk = None if genesis else self._wsk_pub()
        if not self._verifier.verify_host(ev, log=log, wsk_pub=wsk, workspace_id=self.workspace_id):
            raise StoreError("validation.host_key", "host_sig does not verify under the workspace key")
        if genesis and self._pin is not None and canon.event_head(ev) != self._pin:
            raise StoreError("trust.genesis_mismatch", "the genesis differs from the pinned one")
        line = canon.event_line(ev)
        if len(line) > schema.MAX_EVENT_LINE_BYTES:
            raise StoreError("validation.event", "the event line is too large")
        new_state = advance(old_state, ev, log=log, now=max(self._now(), ev["at"]))
        writes = self._projection(old_state, new_state, ev, log, new_texts, files, force)
        info = self._logs.get(log)
        path = self._path_of(log)
        wsi = self._logs[WORKSPACE]  # last look, right before the write: the logs this event rests on are unchanged
        if stat_sig(wsi.path) != info_sig(wsi):
            self._needs_reload = True
            raise StoreError("chain.broken", f"{log}: the workspace log changed on disk while the store held the lock")
        if stat_sig(path) != info_sig(info):
            self._needs_reload = True
            raise StoreError("chain.broken", f"{log}: the log changed on disk while the store held the lock")
        try:
            manifest = self._write_pending(ev, log, line, writes)
            append_durable(path, line, commit=True)  # the commit
            self._finish(manifest, ev, log, line)
        except BaseException:
            self._needs_reload = True  # whatever got half done, the next lock recovers from disk
            raise
        self._state = new_state
        self._after_commit(old_state, new_state, ev, log)
        return Appended(ev, new_state)

    def _host_sign(self, ev: dict[str, Any], log: str) -> None:
        assert self._host is not None
        ev.pop("host_sig", None)
        ev["host_sig"] = crypto.b64u(self._host.sign(canon.host_signing_bytes(self.workspace_id, log, ev)))

    def _stamp(self, ev: dict[str, Any], log: str, *, floor: tuple[int, int, str, int] | None = None) -> None:
        """Stamp ``seq``, ``prev``, ``at`` and ``ws_seq``. ``at`` is the clock or the latest verified `at`, one second
        past the latest verified position only when the merged order would not grow: computed, no loop."""
        info = self._logs.get(log)
        seq = (info.seq if info else 0) + 1
        ev["seq"] = seq
        ev["prev"] = info.head if info else None
        last_at, last_pos = self._order()
        if floor is not None:
            last_pos = floor if last_pos is None or floor > last_pos else last_pos
            last_at = max(last_at, floor[1])
        t = max(int(self._clock()), last_at)
        if (
            ev["type"] == "restore" and log == WORKSPACE and self._pins is not None
        ):  # after every revocation it re-appends
            t = max([t, *(_epoch(r["at"]) for r in self._pins.revocations() if r.get("at"))])
        if log != WORKSPACE:
            wsh = self._logs[WORKSPACE].seq if WORKSPACE in self._logs else 0
            ev["ws_seq"] = wsh
            if last_pos is not None and last_pos[0] == wsh:  # an earlier workspace head leaves any `at` free
                if t < last_pos[1]:
                    t = last_pos[1]
                if t == last_pos[1] and (log, seq) <= (last_pos[2], last_pos[3]):
                    t += 1
        ev["at"] = _fmt(t)

    # -- text, artifacts

    def _section_texts(
        self, e: dict[str, Any], log: str, body: Mapping[str, str | None] | None, texts: dict[str, str] | None
    ) -> dict[str, str] | None:
        """The full section texts the ticket's ``body.md`` gets with this event, or None when it does not change."""
        typ = e["type"]
        if texts is not None:
            return texts
        if log == WORKSPACE:
            if body:
                raise StoreError("validation.body", "the workspace log has no body")
            return None
        if typ == "ticket.created":
            if body:
                raise StoreError("validation.body", "a ticket starts with an empty body")
            return {}
        if typ == "handoff.written":
            text = e.get("text")
            if not isinstance(text, str):
                raise StoreError("validation.event", "handoff.written needs text")
            return self._merge_body(log, e, {"current_state": text})
        if typ == "ticket.updated":
            secs = e.get("sections", {})
            given = dict(body or {})
            if set(given) != set(secs):
                missing, extra = sorted(set(secs) - set(given)), sorted(set(given) - set(secs))
                raise StoreError(
                    "validation.body", f"body must hold exactly the sections of the event ({missing=}, {extra=})"
                )
            if not secs:
                return None
            delta: dict[str, str | None] = {}
            for sid, entry in secs.items():
                text = given[sid]
                if entry is None:
                    if text is not None:
                        raise StoreError("validation.body", f"{sid}: the event removes the section but text was given")
                    delta[sid] = None
                    continue
                if text is None:
                    raise StoreError("validation.body", f"{sid}: the event sets the section but no text was given")
                if canon.section_hash(text) != entry["hash"]:
                    raise StoreError("validation.body", f"{sid}: the text does not match the hash in the event")
                if render.refs_of(text) != entry["refs"]:
                    raise StoreError("body.bad_refs", f"{sid}: refs differ from the artifact references in the text")
                delta[sid] = text
            return self._merge_body(log, e, delta)
        if body:
            raise StoreError("validation.body", f"{typ} carries no body text")
        return None

    def _merge_body(self, uid: str, e: dict[str, Any], delta: Mapping[str, str | None]) -> dict[str, str]:
        assert self._state is not None
        cur = self._body_sections(uid)  # checked against the log: a body the log does not account for is not merged
        new = dict(cur)
        for sid, text in delta.items():
            if text is None:
                new.pop(sid, None)
            else:
                new[sid] = text
        ticket_type = e.get("set", {}).get("ticket.type", self._state.tickets[uid].type)
        try:
            schema.validate("body", {"type": ticket_type, "sections": new})
            render.render_body(ticket_type, new)
        except (schema.SchemaError, render.BodyError) as err:
            raise StoreError("validation.body", str(err)) from None
        return new

    def _file_artifacts(self, e: dict[str, Any], artifacts: Mapping[str, bytes] | None) -> dict[str, bytes]:
        if e["type"] not in ("artifact.added", "artifact.replaced"):
            if artifacts:
                raise StoreError("validation.artifact", f"{e['type']} carries no artifact bytes")
            return {}
        if "sha256" not in e:  # an addon artifact has no file
            if artifacts:
                raise StoreError("validation.artifact", "an addon artifact has no file")
            return {}
        check_artifact(e.get("name"))
        data = (artifacts or {}).get(e["name"])
        if data is None or set(artifacts or {}) != {e["name"]}:
            raise StoreError("validation.artifact", "a file artifact comes with exactly its own bytes")
        if canon.artifact_digest(data) != e["sha256"] or len(data) != e["bytes"]:
            raise StoreError("validation.artifact", "the bytes do not match sha256 and bytes in the event")
        return {e["name"]: data}

    # -- projection

    def _projection(
        self,
        old: State,
        new: State,
        ev: dict[str, Any],
        log: str,
        texts: dict[str, str] | None,
        files: dict[str, bytes],
        force: tuple[str, ...],
    ) -> list[_Write]:
        out: list[_Write] = []
        typ = ev["type"]
        if log == WORKSPACE:
            if new.workspace.genesis is not None:
                g = ev if typ == "workspace.created" else None
                cfg = self._config_bytes(new, g)
                if typ == "workspace.created" or "config.json" in force or cfg != self._config_bytes(old):
                    out.append(_Write("config.json", cfg))
            if "keys.jsonl" in force or typ == "workspace.created":
                out.append(_Write("keys.jsonl", self._keys_bytes()))
            return out
        uid = log
        view = new.tickets[uid]
        base = f"tickets/{uid}/"
        data = self._ticket_for(view, uid)
        prev = old.tickets.get(uid)
        if typ == "ticket.created" or prev is None or "ticket.json" in force or data != self._ticket_for(prev, uid):
            out.append(_Write(base + "ticket.json", data))
        if texts is not None:
            out.append(_Write(base + "body.md", render.render_body(view.type, texts).encode("utf-8"), body_copy=uid))
        for name, blob in files.items():
            out.append(_Write(base + "artifacts/" + name, blob))
        if typ == "ticket.created":
            out.append(_Write("keys.jsonl", self._keys_bytes(extra=(view.key, uid, ev["at"]))))
        return out

    def _ticket_for(self, view: Any, uid: str) -> bytes:
        data = self._ticket_bytes(uid, view)
        try:
            schema.validate("ticket", loads(data))
        except (schema.SchemaError, ValueError) as err:
            raise StoreError("validation.ticket", str(err)) from None
        return data

    def _config_bytes(self, state: State, genesis: dict[str, Any] | None = None) -> bytes:
        g = genesis or self._genesis_event
        if g is None or state.workspace.genesis is None:
            return b""
        name, run_for = self._name or state.workspace.prefix, ["owner", "maintainer", "member"]
        cur = read_or_none(self.root / "config.json")
        if cur is not None:  # `name` and `agents.run_for` are in no event: whatever the file says (if valid) stays
            with contextlib.suppress(ValueError, KeyError, TypeError, schema.SchemaError):
                doc = loads(cur)
                schema.validate("workspace", doc)
                name, run_for = doc["workspace"]["name"], doc["agents"]["run_for"]
        return render.render_config(
            state.workspace, host_id=g["host_id"], wsk_pub=g["wsk_pub"], name=name, run_for=run_for
        )

    def _keys_bytes(self, extra: tuple[str, str, str] | None = None) -> bytes:
        """``keys.jsonl`` for every ticket log there is (key and creation time from its first line, a hint that the
        ticket's own replay checks when it is loaded), plus ``extra`` (the ticket being created)."""
        rows = {u: (h.key, h.created_at or "") for u, h in self._hints.items() if h.key and _KEY_NUM.fullmatch(h.key)}
        if extra is not None:
            rows[extra[1]] = (extra[0], extra[2])
        ordered = sorted(rows.items(), key=lambda kv: (int(_KEY_NUM.fullmatch(kv[1][0]).group(1)), kv[0]))  # type: ignore[union-attr]
        return b"".join(render.keys_line(k, u, a) for u, (k, a) in ordered)

    # -- pending, install

    def _write_pending(self, ev: dict[str, Any], log: str, line: bytes, writes: list[_Write]) -> dict[str, Any]:
        pdir = self.state_dir / "pending" / ev["id"]
        pdir.mkdir(parents=True, exist_ok=True)
        files = []
        for n, w in enumerate(writes):
            fd = os.open(pdir / str(n), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(w.data)
                f.flush()
                os.fsync(f.fileno())
            files.append({"to": w.rel, "n": n, "sha256": _sha(w.data)})
        manifest = {
            "v": 1,
            "id": ev["id"],
            "log": log,
            "seq": ev["seq"],
            "line": line.decode("utf-8"),
            "files": files,
            "body_copy": next((w.body_copy for w in writes if w.body_copy), None),
        }
        write_atomic(pdir / "manifest.json", json.dumps(manifest, separators=(",", ":")).encode("utf-8"), mode=0o600)
        fsync_dir(pdir.parent)
        return manifest

    def _finish(self, manifest: dict[str, Any], ev: dict[str, Any], log: str, line: bytes) -> None:
        pdir = self.state_dir / "pending" / ev["id"]
        torn = self._install(manifest, pdir)
        if torn:  # we just wrote them: this is a disk fault, not a crash
            raise StoreError("store.torn_write", f"could not install {', '.join(torn)}")
        shutil.rmtree(pdir, ignore_errors=True)
        info = self._logs.setdefault(log, LogInfo(log, self._path_of(log)))
        info.starts.append(info.offset)
        info.offset += len(line)
        info.seen_size = info.offset
        info.seq = ev["seq"]
        info.heads.append(canon.event_head(ev))
        sig = stat_sig(info.path)
        info.ino = sig[1] if sig else None
        info.seen_times = (sig[2], sig[3]) if sig else (0, 0)
        if log != WORKSPACE:
            self._loaded.add(log)

    # -- after the commit

    def _after_commit(self, old: State, new: State, ev: dict[str, Any], log: str) -> None:
        assert self._host is not None
        self._own_at = max(self._own_at, _epoch(ev["at"]))
        if log == WORKSPACE:
            self._ws_last_at = max(self._ws_last_at, _epoch(ev["at"]))
        else:
            pos = (ev["ws_seq"], _epoch(ev["at"]), log, ev["seq"])
            self._own_pos = pos if self._own_pos is None or pos > self._own_pos else self._own_pos
            self._exists.add(log)
            self._sizes[log] = self._logs[log].offset
            if ev["type"] == "ticket.created" and log not in self._hints:
                self._hints[log] = _Hint((0, 0, 0, 0), 0, ev["key"], ev["at"])
                self._key_uid[ev["key"]] = log
        typ = ev["type"]
        if typ == "workspace.created":
            self._genesis_event = ev
            self._pin = self._expected_genesis = new.workspace.genesis
            self._load()  # replay from here on carries the pin (the State built so far has none)
            if self._pins is not None and new.workspace.genesis:
                self._pins.pin_genesis(new.workspace.genesis)
        if typ == "grant.issued":
            self._grant_hashes.setdefault(ev["grant"], ev["secret_hash"])
        if typ == "device.revoked" and self._pins is not None:
            self._pins.note_revocation(ev["device"], ev["reason"], ev["revocation"], ev["at"])
        if typ == "restore":
            self._checkpoints.abandon_above(log, ev["from_seq"])
            self._diverged.pop(log, None)
        self._write_checkpoints(log, ev, new)
        changed = {u for u, v in new.tickets.items() if old.tickets.get(u) is not v}
        self._maybe_index(new)
        if new.workspace.genesis is not None and self._index_ok:
            try:
                self._index.update(new, self._logs, [log], changed, self._sizes_now() | {log: self._logs[log].offset})
            except Exception:  # noqa: BLE001 - derived data: never fail an append for it
                self._index.drop()
                self._index_ok = False
        if typ == "restore" and log == WORKSPACE:
            self._ws_restore_at = max(self._ws_restore_at, ev["at"])
            self._pending_revs = self._revocation_plan()
            self._enforce_revocations()

    def _write_checkpoints(self, log: str, ev: dict[str, Any], new: State) -> None:
        assert self._host is not None
        sign = self._host.sign
        info = self._logs[log]
        if log != WORKSPACE:
            self._ticket_appends_since_cp += 1
            if self._checkpoints.write_ticket(sign, log, info.seq, info.head or "", ev["at"]) == "diverged":
                self._diverged[log] = Divergence(log, info.seq, "a checkpoint at this height has another head")
        if log == WORKSPACE or self._ticket_appends_since_cp >= WORKSPACE_EVERY:
            self._write_workspace_checkpoint(ev["at"], new)

    def _write_workspace_checkpoint(self, at_: str, state: State) -> None:
        assert self._host is not None
        if state.workspace.genesis is None or self._logs[WORKSPACE].seq == 0:
            return
        if self._checkpoints.write_workspace(self._host.sign, state.workspace.genesis, at_, self._logs) == "diverged":
            self._diverged[WORKSPACE] = Divergence(
                WORKSPACE, self._logs[WORKSPACE].seq, "a checkpoint at this height has another head"
            )
        self._ticket_appends_since_cp = 0

    def checkpoint(self) -> None:
        """Write the workspace checkpoint now (and a ticket checkpoint for every log that has none yet)."""
        with self._locked():
            self._checkpoint_all()

    def _checkpoint_all(self) -> None:
        if self._host is None or self._state is None or self._state.workspace.genesis is None or self._diverged:
            return
        at_ = self._now()
        for uid, info in self._logs.items():
            if uid == WORKSPACE or not info.seq or info.error:
                continue
            cp = self._checkpoints.ticket(uid)
            if cp is None or "o" not in cp or cp["o"]["seq"] != info.seq:
                self._checkpoints.write_ticket(self._host.sign, uid, info.seq, info.head or "", at_)
        self._write_workspace_checkpoint(at_, self._state)

    # ------------------------------------------------------------------ idempotent appends

    def _intent_path(self, idem: str) -> Path:
        return self.state_dir / "intents" / (_sha(idem.encode("utf-8"))[:40] + ".json")

    def _read_intent(self, idem: str) -> dict[str, Any] | None:
        try:
            rec = loads(read_or_none(self._intent_path(idem)) or b"null")
        except ValueError:
            return None
        if isinstance(rec, dict) and isinstance(rec.get("log"), str) and isinstance(rec.get("id"), str):
            return rec
        return None

    def _uid_for_idem(self, idem: str | None) -> str | None:
        """The uid a retried ``create_ticket`` must use: the one its first attempt recorded (a name, nothing more; the
        request is matched against the logged event before it counts as a duplicate)."""
        rec = self._read_intent(idem) if idem else None
        return rec["log"] if rec and _ULID.fullmatch(rec["log"]) else None

    @staticmethod
    def _same_request(logged: dict[str, Any], e: dict[str, Any]) -> bool:
        """Is ``logged`` the event ``e`` asks for: same type, actor and payload (everything the host does not stamp)?
        ``id`` and ``based_on`` are compared only for a signed event (they are inside its signature); a
        ``ticket.created`` ignores ``key`` (allocated per attempt)."""
        skip = {*_HOST_FIELDS, "id", "based_on"}
        if e.get("type") == "ticket.created":
            skip.add("key")
        want = {k: v for k, v in e.items() if k not in skip}
        want.setdefault("v", 2)
        want.setdefault("hash_v", 1)
        have = {k: v for k, v in logged.items() if k not in skip}
        if have != want:
            return False
        if isinstance(e.get("actor"), dict) and e["actor"].get("kind") == "person":
            return logged.get("id") == e.get("id") and logged.get("based_on") == e.get("based_on")
        return True

    def _idem_hit(self, idem: str, log: str, e: dict[str, Any]) -> Appended | None:
        """A retry of an append that already happened returns its result. The record is believed only as far as the log
        confirms it: same log, and the logged event is the one requested (type, actor, payload; id and base for a signed
        event). A first attempt that never reached the log hands its event id to the retry, for an event nobody signs
        (a signed event keeps its own id, which is already its idempotency key)."""
        rec = self._read_intent(idem)
        signed = isinstance(e.get("actor"), dict) and e["actor"].get("kind") == "person"
        seq = None
        if signed and isinstance(e.get("id"), str):
            seq = self._seq_of_id(log, e["id"])  # a signed event is its own idempotency key
        elif rec is not None and rec["log"] == log:
            info = self._logs.get(log)
            s = rec.get("seq")
            if isinstance(s, int) and info is not None and 1 <= s <= info.seq and info.heads[s - 1] == rec.get("head"):
                seq = s
            else:
                seq = self._seq_of_id(log, rec["id"])
        if seq is not None:
            logged = self._read_event(log, seq)
            if self._same_request(logged, e):
                return Appended(logged, self.state, True)
            return None  # a record that names some other event is ignored, never trusted
        if rec is not None and rec["log"] == log and not signed and "id" not in e:
            e["id"] = rec["id"]  # the first attempt never reached the log: same id, so it cannot land twice
        return None

    def _seq_of_id(self, log: str, event_id: str) -> int | None:
        info = self._logs.get(log)
        if info is None or not info.seq:
            return None
        needle = b'"id":"' + event_id.encode() + b'"'
        try:
            fd = open_nofollow(self._path_of(log), os.O_RDONLY)
        except OSError:
            return None
        with os.fdopen(fd, "rb") as f:  # line by line, at most one line's limit at a time
            for i in range(1, info.seq + 1):
                line = f.readline(MAX_LINE + 1)
                if needle in line:
                    with contextlib.suppress(canon.HashError, KeyError):
                        if canon.parse_event_line(line)["id"] == event_id:
                            return i
        return None

    def _read_event(self, log: str, seq: int) -> dict[str, Any]:
        info = self._logs[log]
        fd = open_nofollow(self._path_of(log), os.O_RDONLY)
        with os.fdopen(fd, "rb") as f:  # the line offsets come from the verified read: seek, do not scan the log
            f.seek(info.starts[seq - 1])
            return canon.parse_event_line(f.readline(MAX_LINE + 1))

    def _idem_intent(self, idem: str, log: str, event_id: str) -> None:
        write_atomic(self._intent_path(idem), json.dumps({"log": log, "id": event_id}).encode("utf-8"), mode=0o600)

    def _idem_done(self, idem: str, log: str, ev: dict[str, Any]) -> None:
        rec = {"log": log, "id": ev["id"], "seq": ev["seq"], "head": canon.event_head(ev)}
        write_atomic(self._intent_path(idem), json.dumps(rec).encode("utf-8"), mode=0o600, durable=False)

    # ------------------------------------------------------------------ external edits (§5.8)

    def scan(self) -> list[dict[str, Any]]:
        """Load every ticket, look at every ticket's files and ``config.json``/``keys.jsonl``; answer each external
        change with the host events §5.8 names (``edit.external`` for ``body.md``; revert and ``projection.repaired``
        for the rest). Returns the events appended. A read-only store reports instead (``reports``)."""
        with self._locked():
            self._ensure(set(self._exists))
            return self._scan_all()

    def _scan_all(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        assert self._state is not None
        if self._host is None:
            return out
        bad = {c.log for c in self._state.chain_errors} | set(self._read_errors) | set(self._diverged)
        if WORKSPACE in bad:
            return out
        try:
            out += self._heal_workspace()
        except StoreError as e:
            self._report(e.code, e.detail, WORKSPACE)
        for uid in sorted(self._state.tickets):
            if uid in bad:
                continue
            try:
                out += self._heal_ticket(uid)
            except StoreError as e:  # one ticket that cannot be healed must not stop the others (or opening)
                self._report(e.code, e.detail, uid)
        return out

    def _after_open_heal(self, eager: bool) -> None:
        """On open: answer a changed ``config.json``/``keys.jsonl``; with ``load="all"`` also every ticket's files."""
        assert self._state is not None
        if self._state.workspace.genesis is None or WORKSPACE in self._read_errors or WORKSPACE in self._diverged:
            return
        if eager:
            if not self._read_errors and not self._diverged:
                self._scan_all()
                self._checkpoint_all()
            return
        try:
            self._heal_workspace()
        except StoreError as e:
            self._report(e.code, e.detail, WORKSPACE)

    def _heal_for(self, log: str, typ: str, host_event: bool) -> list[dict[str, Any]]:
        if typ in ("workspace.created", "ticket.created"):
            return []
        if log == WORKSPACE:
            return [] if host_event else self._heal_workspace()
        return [] if host_event and typ in _TICKET_HOST_EVENTS else self._heal_ticket(log)

    def _host_append(self, typ: str, log: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        if not self._loaded >= self._exists:  # a host write comes only from state equal to the full replay
            self._verified_keys()
            if self._unsure() or (log != WORKSPACE and log not in self._loaded):
                self._ensure(set(self._exists))
        was, self._healing = self._healing, True
        try:
            info = self._logs.get(log)
            ev = {"type": typ, "actor": {"kind": "host"}, **payload, "based_on": info.head if info else None}
            return self._append(ev, log, None, None, None, **kw).event
        finally:
            self._healing = was

    def _heal_workspace(self) -> list[dict[str, Any]]:
        assert self._state is not None
        out: list[dict[str, Any]] = []
        if self._state.workspace.genesis is None or self._host is None:
            return out
        for path, cause, produce in (
            ("config.json", "projection_mismatch", lambda: self._config_bytes(self._state)),
            ("keys.jsonl", "keys_mismatch", lambda: self._keys_bytes()),
        ):
            want = produce()
            if read_or_none(self.root / path) == want:
                continue
            self._torn.discard(path)
            out.append(
                self._host_append("projection.repaired", WORKSPACE, {"path": path, "cause": cause}, force=(path,))
            )
        return out

    def _heal_ticket(self, uid: str) -> list[dict[str, Any]]:
        assert self._state is not None and self._host is not None
        if uid not in self._state.tickets:
            return []
        view = self._state.tickets[uid]
        out: list[dict[str, Any]] = []
        self._bad_projection.pop(uid, None)
        tpath = f"tickets/{uid}/ticket.json"
        want = self._ticket_bytes(uid)
        have = read_or_none(self.root / tpath)
        if have != want:
            cause = "projection_mismatch" if tpath in self._torn else "external_edit"
            self._torn.discard(tpath)
            payload: dict[str, Any] = {"path": "ticket.json", "cause": cause}
            fields = self._changed_fields(have, uid)
            if fields:
                payload["fields"] = fields
            out.append(self._host_append("projection.repaired", uid, payload, force=("ticket.json",)))
        out += self._heal_body(uid, view)
        return out

    def _changed_fields(self, have: bytes | None, uid: str) -> list[str]:
        assert self._state is not None
        if have is None:
            return []
        try:
            doc = loads(have)
            want = loads(self._ticket_bytes(uid))
        except ValueError:
            return []
        if not isinstance(doc, dict):
            return []
        keys = sorted(k for k in set(doc) | set(want) if doc.get(k, object()) != want.get(k, object()))
        return [f"ticket.{k}" for k in keys if k in render.TICKET_KEYS or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(k))]

    def _copy_texts(self, uid: str, view: Any) -> dict[str, str] | None:
        """The host's copy of the installed body.md, only if its section hashes are the log's (§5.5)."""
        raw = read_or_none(self.state_dir / "body" / f"{uid}.md")
        if raw is None:
            return None
        try:
            texts = render.parse_body(raw, view.type)
        except render.BodyError:
            return None
        return texts if self._hashes(texts) == self._log_hashes(view) else None

    @staticmethod
    def _hashes(texts: Mapping[str, str]) -> dict[str, str]:
        return {s: h for s, t in texts.items() if (h := canon.section_hash(t)) != _EMPTY}

    @staticmethod
    def _log_hashes(view: Any) -> dict[str, str]:
        return {s: v["hash"] for s, v in view.sections.items() if v["hash"] != _EMPTY}

    def _heal_body(self, uid: str, view: Any) -> list[dict[str, Any]]:
        assert self._state is not None
        bpath = f"tickets/{uid}/body.md"
        raw = read_or_none(self.root / bpath)
        torn = bpath in self._torn
        if raw is None:
            texts: dict[str, str] | None = {}
            normalised = False
        else:
            texts, normalised = self._read_external(raw, view.type)
        copy = self._copy_texts(uid, view)
        if texts is None:  # not a body at all (unknown heading, open fence, text outside the rules)
            if copy is None:
                self._unhealable(uid, f"{bpath} is not a body and no valid copy to restore")
                return []
            self._torn.discard(bpath)
            self._keep_rejected(uid, raw)
            return [self._repair_body(uid, view, copy, [], "external_edit" if not torn else "projection_mismatch")]
        have_h = {s: canon.section_hash(t) for s, t in texts.items()}
        log_h = {s: v["hash"] for s, v in view.sections.items()}
        changed: dict[str, dict[str, Any] | None] = {}
        for sid in sorted(set(have_h) | set(log_h)):
            h_new = have_h.get(sid)
            h_old = log_h.get(sid)
            if (h_new or _EMPTY) == (h_old or _EMPTY):
                continue  # a missing section and an empty one are the same text
            changed[sid] = render.section_entry(texts[sid]) if sid in texts else None
        if not changed:
            return []
        self._torn.discard(bpath)
        bound = [s for s, entry in changed.items() if self._is_bound(uid, s, entry)]
        if bound and copy is None:
            self._unhealable(uid, f"bound sections {bound} of {bpath} changed and no valid copy to revert")
            return []
        installs = {s: v for s, v in changed.items() if s not in bound}
        final = {s: t for s, t in texts.items() if s not in bound}
        for s in bound:
            if copy is not None and s in copy:
                final[s] = copy[s]
        out: list[dict[str, Any]] = []
        if installs:
            voids = external_edit_voids(self._state, uid, installs)
            if isinstance(voids, Refusal):
                raise StoreError.from_refusal(voids)
            out.append(
                self._host_append(
                    "edit.external",
                    uid,
                    {"sections": installs, "voided_gates": voids, "normalised": normalised},
                    texts=final,
                )
            )
        if bound:
            out.append(
                self._repair_body(
                    uid, self._state.tickets[uid], final, bound, "projection_mismatch" if torn else "external_edit"
                )
            )
        return out

    def _unhealable(self, uid: str, detail: str) -> None:
        """An edit outside the store that cannot be answered (nothing trustworthy to revert to): reported, and the
        ticket takes no new event until the files match the log again."""
        self._bad_projection[uid] = detail
        self._report("store.torn_write", detail, uid)

    def _is_bound(self, uid: str, sid: str, entry: dict[str, Any] | None) -> bool:
        assert self._state is not None
        r = external_edit_voids(self._state, uid, {sid: entry})
        return isinstance(r, Refusal) and r.code == Code.TICKET_FROZEN

    def _keep_rejected(self, uid: str, raw: bytes | None) -> None:
        """A body.md that is not a body is saved to ``.state/rejected/<uid>-<at>.md`` before it is reverted, so prose a
        person typed is not lost; the report says where."""
        if raw is None:
            return
        d = self.state_dir / "rejected"
        stem = f"{uid}-{self._now().replace(':', '')}"
        p, n = d / f"{stem}.md", 0
        while os.path.lexists(p):
            n += 1
            p = d / f"{stem}.{n}.md"
        write_atomic(p, raw, mode=0o600)
        self._report(
            "store.rejected_body", f"body.md was not a body; the file is kept as .state/rejected/{p.name}", uid
        )

    def _repair_body(self, uid: str, view: Any, texts: dict[str, str], bound: list[str], cause: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"path": "body.md", "cause": cause}
        if bound:
            payload["fields"] = [f"body.{s}" for s in sorted(bound)]
        return self._host_append("projection.repaired", uid, payload, texts=texts)

    @staticmethod
    def _read_external(raw: bytes, ticket_type: str) -> tuple[dict[str, str] | None, bool]:
        """Section texts of a body.md someone else wrote, normalised (§11.3), and whether that changed anything. None
        when the file is not a body §4 accepts."""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, False
        unified = text.replace("\r\n", "\n").replace("\r", "\n")
        try:
            parsed = render.parse_body(canon.nfc(unified), ticket_type)
            texts = {s: canon.normalize_text(t) for s, t in parsed.items()}
            texts = {s: t.strip("\n") for s, t in texts.items()}
            schema.validate("body", {"type": ticket_type, "sections": texts})
        except (render.BodyError, canon.TextError, schema.SchemaError):
            return None, False
        return texts, canon.nfc(unified) != text

    # ------------------------------------------------------------------ restore support (§5.10)

    def restore_facts(self, log: str) -> dict[str, Any]:
        """What an owner needs to sign ``restore`` for ``log``: the last event on disk (``from_seq``, ``head``), the
        highest checkpoint above it (``abandoned``, or None) and the signed decisions the host still knows of in the
        part it archived with :meth:`abandon_tail` (``abandoned_decisions``)."""
        info = self._logs.get(log)
        if info is None:
            raise StoreError("validation.log", f"unknown log {log}")
        cp = self._checkpoints.workspace() if log == WORKSPACE else self._checkpoints.ticket(log)
        abandoned = None
        if cp and "o" in cp:
            seq, head = (
                (cp["o"]["workspace_log"]["seq"], cp["o"]["workspace_log"]["head"])
                if log == WORKSPACE
                else (cp["o"]["seq"], cp["o"]["head"])
            )
            if seq > info.seq:
                abandoned = {"seq": seq, "head": head}
        decisions: list[str] = []
        for p in (
            sorted((self.state_dir / "abandoned").glob(f"{log}-*.jsonl"))
            if (self.state_dir / "abandoned").is_dir()
            else []
        ):
            for line in p.read_bytes().splitlines():
                with contextlib.suppress(canon.HashError, KeyError):
                    e = canon.parse_event_line(line + b"\n")
                    if e["type"] in ("gate.approved", "gate.changes_requested", "verdict.given", "question.answered"):
                        decisions.append(e["id"])
        return {
            "from_seq": info.seq,
            "head": info.head,
            "abandoned": abandoned,
            "abandoned_decisions": sorted(decisions),
        }

    def abandon_tail(self, log: str, from_seq: int) -> int:
        """Cut ``log`` back to ``from_seq`` on disk, keeping the cut lines in ``.state/abandoned/`` (never deleted).
        For a log that has events after the last valid one (a fork, a rewritten tail); a rolled-back log needs none.
        Returns the number of lines moved. The next step is an owner-signed ``restore``."""
        with self._locked():
            self._ensure({log} if log != WORKSPACE else set())
            path = self._path_of(log)
            raw = read_or_none(path, None) or b""  # an owner's explicit repair: the whole file
            lines = raw.splitlines(keepends=True)
            if from_seq >= len(lines):
                return 0
            keep, cut = b"".join(lines[:from_seq]), b"".join(lines[from_seq:])
            ab = self.state_dir / "abandoned"
            ab.mkdir(parents=True, exist_ok=True)
            target, n = ab / f"{log}-{from_seq + 1}.jsonl", 0
            while os.path.lexists(target):  # an earlier archive is never overwritten
                n += 1
                target = ab / f"{log}-{from_seq + 1}.{n}.jsonl"
            write_atomic(target, cut, mode=0o600)
            write_atomic(path, keep, mode=0o644)
            self._needs_reload = True
            self._load()
            return len(lines) - from_seq

    def _revocation_plan(self) -> list[dict[str, Any]]:
        """Revocations the host noted (PK-signed, §5.10) that the workspace log does not show: those a workspace
        ``restore`` post-dates are to be re-appended (a restore without them is incomplete, so this runs on every load
        and finishes a re-append that a crash interrupted); any other is a rollback of a revocation, and the workspace
        log is marked diverged (appends refused until an owner ``restore``)."""
        assert self._state is not None
        ws = self._state.workspace
        out: list[dict[str, Any]] = []
        if self._pins is None or ws.genesis is None or WORKSPACE not in self._logs:
            return out
        for rec in self._pins.revocations():
            dev = ws.devices.get(rec.get("device"))
            if dev is None or dev.revoked:
                continue
            if self._ws_restore_at and rec.get("at", "") <= self._ws_restore_at:
                out.append(rec)
            else:
                self._diverged.setdefault(
                    WORKSPACE,
                    Divergence(
                        WORKSPACE,
                        self._logs[WORKSPACE].seq,
                        f"the host noted a revocation of {rec.get('device')} that the log lacks (rolled back?)",
                    ),
                )
        return out

    def _enforce_revocations(self) -> None:
        """Re-append the revocations of :meth:`_revocation_plan` (host actor, allowed by the embedded revocation, §5.3).
        A read-only store cannot: it marks the workspace diverged instead."""
        todo, self._pending_revs = self._pending_revs, []
        if not todo or self._healing:
            return
        if self._host is None:
            self._diverged.setdefault(
                WORKSPACE, Divergence(WORKSPACE, self._logs[WORKSPACE].seq, "a restore lacks noted revocations")
            )
            return
        for rec in todo:
            dev = self._state.workspace.devices.get(rec["device"]) if self._state else None
            if dev is None or dev.revoked:
                continue
            self._host_append(
                "device.revoked",
                WORKSPACE,
                {"device": rec["device"], "reason": rec["reason"], "revocation": rec["revocation"]},
            )
