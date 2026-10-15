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
import logging
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISLNK as _S_ISLNK
from stat import S_ISREG as _S_ISREG
from typing import Any, Literal, NamedTuple, Protocol

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
from .outbox import Outbox, OutboxEntry, RelayPosition, Segment, make_entry, parse_entry
from .paths import check_artifact, check_state_dirs, check_uid, safe_join, target_ok
from .pins import HostPins, is_forwarded, pair

__all__ = ["Appended", "BackendSigner", "HostSigner", "Report", "Store"]

_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_KEY_NUM = re.compile(r"[A-Z][A-Z0-9]{0,15}-([0-9]+)")
_HOST_FIELDS = ("seq", "at", "prev", "ws_seq", "host_sig")
_EMPTY = canon.section_hash("")
_PLACEHOLDER_SIG = "A" * 85 + "A"
_TICKET_HOST_EVENTS = frozenset({"edit.external", "projection.repaired"})
_ZERO_ACC = "sha256:" + "0" * 64
RELAY_BRANCH_MAX = (64 << 20) // 74  # heads: the relay's largest staging set (64 MiB at 74 bytes a head, P3-D20)
_NOPLAN = object()  # no outbox plan: the append writes its workspace checkpoint by the P1 rule (every 50)
_log = logging.getLogger("orch.store")
MAX_FORWARDED_ITEM_BYTES = 4096  # a forwarded revocation item (cj) above this is dropped (H12b review, Opus F2)
_MAX_LABEL_SEALED = 1024  # b64u characters of a forwarded certificate's sealed label
_ITEM_KEYS = frozenset(
    {"device_id", "person_id", "reason", "revocation", "device_cert", "pk_pub", "provenance", "chain_reason"}
)


class _Cut(NamedTuple):
    """The point an append is about to give its log: what ``Checkpoints.build_workspace`` reads of a log."""

    seq: int
    head: str
    acc: str


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


# §5.10: the recovery for a host whose state shows checkpoints existed but none is held (doctor says the same).
MISSING_CHECKPOINT_RECOVERY = (
    "P1 has no in-place repair (a restore cannot succeed without its source checkpoint): restore .state/checkpoints "
    "and the host pin from a backup, or wait for the P3 owner-authenticated baseline. Do not delete the host state "
    "dir (hosts/<workspace id>/): it holds the genesis pin and the durable revocation evidence"
)


def _drop(path: Path) -> None:
    """Remove a stale pending transition durably (§5.10: crash recovery keeps only live ones)."""
    path.unlink(missing_ok=True)
    fsync_dir(path.parent)


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


class LoadBudgetExceeded(Exception):
    """A best-effort read used its dependency budget."""


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
        guard: Callable[[], contextlib.AbstractContextManager[Any]] | None = None,
    ) -> None:
        self.root = root
        self._guard = guard or contextlib.nullcontext
        #: Host mode (H4a1): called as ``append_check(log, event)`` right before a caller's event (any actor but the
        #: host) is committed, after its id is final; it raises :class:`StoreError` to refuse. The host uses it to make
        #: sure no event lands that its outcome record does not list (§0.7). ``None``: no check (direct mode).
        self.append_check: Callable[[str, dict[str, Any]], None] | None = None
        #: Host mode (H6a): ``before_append()`` runs under the lock before every append (the host's foreign-append
        #: check, F11), and ``on_commit(log, event)`` under the lock right after an event is committed (the host's
        #: subscription journal). ``None``: no hook (direct mode).
        self.before_append: Callable[[], None] | None = None
        self.on_commit: Callable[[str, dict[str, Any]], None] | None = None
        #: how often a lock found a log changed on disk that this store did not write (or a crashed append to
        #: finish): the host's cheap foreign-append signal (F11)
        self.outside_changes = 0
        self.workspace_id = workspace_id
        self._expected_genesis = expected_genesis
        self._host = host
        self._verifier = verifier
        self._clock = clock
        self._name = workspace_name
        self.state_dir = root / ".state"
        self._pins = HostPins(host_state_dir, workspace_id) if host_state_dir is not None else None
        #: the relay outbox (§11 P3-D21), active while ``<state>/hosts/<ws>/relay/link.json`` exists
        self._outbox = Outbox(self._pins.dir / "outbox") if self._pins is not None else None
        self._relay_branch: tuple[tuple[Any, ...], tuple[str, ...], tuple[str, ...]] | None = None  # R3 paging
        self._lock = FileLock(self.state_dir / "lock", timeout=lock_timeout)
        self._checkpoints = Checkpoints(self.state_dir / "checkpoints", workspace_id)
        self._index = Index(self.state_dir / "index.sqlite")
        self.reports: list[Report] = []
        self._logs: dict[str, LogInfo] = {}
        self._loaded: set[str] = set()
        self._load_budget: int | None = None
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
        #: while opening: the ``config.json`` bytes the caller read (discovery), used instead of a read by path until
        #: the store installs a new ``config.json`` itself
        self._known_config: bytes | None = None
        self._bad_projection: dict[str, str] = {}
        self._ticket_appends_since_cp = 0
        self._healing = False
        self._grant_hashes: dict[str, str] = {}
        self._index_ok = False
        self._exists: set[str] = set()
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
        guard: Callable[[], contextlib.AbstractContextManager[Any]] | None = None,
        config: bytes | None = None,
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
        raises ``store.busy``. ``guard`` wraps every outermost transaction, outside the workspace lock: the direct-mode
        CLI passes :class:`orch.host.ownership.DirectGuard` (D-P2-18), which holds the ownership lock shared and
        answers ``host.required`` for a workspace the host has adopted. ``config``: the ``config.json`` bytes the
        caller already read (direct-mode discovery, :mod:`orch.discovery`); the open-time workspace id check uses
        them instead of reading the file by path again.
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
            guard=guard,
        )
        store._open(load == "all", config)
        return store

    def _open(self, eager: bool, config: bytes | None = None) -> None:
        for d in ("events", "tickets", ".state"):
            if os.path.islink(self.root / d):
                raise StoreError("validation.path", f"{d} is a symlink")
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self._known_config = config
        try:
            self._open_checked(eager)
        finally:
            self._known_config = None

    def _config_json(self) -> bytes | None:
        """``config.json`` as it is now: the caller's bytes while opening (until the store installs a new one), else
        read below the root."""
        return self._known_config if self._known_config is not None else read_or_none(self.root / "config.json")

    def _open_checked(self, eager: bool) -> None:
        cfg = self._config_json()
        if cfg is not None:
            try:
                have = loads(cfg)["workspace"]["id"]
            except (ValueError, KeyError, TypeError):
                have = None
            if have is not None and have != self.workspace_id:
                raise StoreError("trust.genesis_mismatch", "config.json belongs to another workspace")
        with self._guard(), self._lock:
            check_state_dirs(self.root)
            self._load_transition_logs()
            pend = self._recover_pending()
            self._scan_dirs()
            if eager:
                self._loaded = set(self._exists)
            self._loaded |= {m["log"] for _, m in pend if m["log"] != WORKSPACE}
            self._load()
            self._finish_pending(pend)
            self._recover_catchup()
            self._finish_applied_checkpoint()
            self._enforce_revocations()
            if self._host is not None:
                self._after_open_heal(eager)
            self._maybe_index()

    def close(self) -> None:
        self._index.close()
        if self._pins is not None:
            self._pins.close()

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
            return self._read_events(uid, max(after, 0) + 1, last)

    def revision(self) -> tuple[Any, ...]:
        """A value that stays equal exactly while no log changed (H12d-3, #555 review F1): the read heads ``(log, seq,
        offset)`` of every log this store holds, after the lock re-read what changed on disk, and the stat signature
        ``(size, inode, mtime_ns, ctime_ns)`` of every ticket log file (``_scan_dirs``: one stat per ticket), so an
        append by this store or another writer, a new ticket log and a cut tail each change it. For a caller that
        caches a judgement over the whole history (``Store.state`` is a new object at every read)."""
        with self._locked():
            self._scan_dirs()
            heads = tuple(sorted((log, info.seq, info.offset) for log, info in self._logs.items()))
            files = tuple(sorted((uid, hint.sig) for uid, hint in self._hints.items()))
            return heads, files

    def replay_context(self) -> Any:
        """The model context this store replays with (its verifier and both pins), for a caller that replays the same
        events itself (the host's subscription journal)."""
        return self.state._ctx

    def log_size(self, log: str) -> int | None:
        """The bytes of ``log`` this store has read or written (``None``: a log it does not know)."""
        info = self._logs.get(log)
        return info.offset if info is not None else None

    def merged_events(self) -> list[tuple[str, dict[str, Any]]]:
        """Every event of every log, as read and chain-checked, in the merged order of §5.5: ``(log, event)`` pairs,
        the workspace log first within one ``ws_seq``, then ticket events by ``(at, uid, seq)``. Loads every ticket.
        The host rebuilds its subscription journal (H6a) from it; call it with the lock held to pair it with a check of
        the files."""
        import heapq

        with self._locked():
            self._scan_dirs()  # a ticket log another writer created since the last scan is in it too
            self._ensure(set(self._exists))
            streams = []
            for log, info in sorted(self._logs.items()):
                events = self._read_events(log, 1, info.seq)
                if log == WORKSPACE:
                    streams.append([((e["seq"], 0, 0, "", e["seq"]), log, e) for e in events])
                else:
                    streams.append([((e["ws_seq"], 1, _epoch(e["at"]), log, e["seq"]), log, e) for e in events])
            return [(log, e) for _, log, e in heapq.merge(*streams, key=lambda x: x[0])]

    def event_fields_before(self, uid: str, seq: int, names: set[str]) -> dict[str, dict[str, Any]]:
        """Find the last criteria and tasks before a tail, without replaying the log."""
        state: dict[str, dict[str, Any]] = {}
        with self._locked():
            self._ensure({uid})
            seq = min(seq, self._logs[uid].seq)
            while seq > 0 and names - state.keys():
                first = max(1, seq - 31)
                for event in reversed(self._read_events(uid, first, seq)):
                    if event["type"] == "ticket.updated":
                        for name in names:
                            if name not in state and f"ticket.{name}" in event.get("set", {}):
                                state[name] = {x["id"]: x for x in event["set"][f"ticket.{name}"]}
                seq = first - 1
        return {"acceptance": {}, "tasks": {}, **state}

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
            out = self._flush(uid)
            out.append(self._host_append(typ, uid, dict(payload)))
            return out + self._flush(uid, cause="new_commits")

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

    def replay_invalidations(self, uid: str, note: Callable[[str, str], None]) -> None:
        """Observe invalidation transitions from the verified logs through the model's replay."""
        with self._locked():
            self._ensure({uid})
            replay(
                self._read_events(WORKSPACE, 1, self._logs[WORKSPACE].seq),
                {u: self._read_events(u, 1, self._logs[u].seq) for u in sorted(self._loaded)},
                verifier=self._verifier,
                now=self.state.now,
                expected_workspace_id=self.workspace_id,
                expected_genesis=self._pin,
                verb_events=_verb_events(),
                invalidations=(uid, note),
            )

    def _flush(self, uid: str, *, cause: str | None = None) -> list[dict[str, Any]]:
        assert self._state is not None
        from .invalidations import pending_causes

        t = self._state._core.tickets.get(uid)
        if t is None or not any(g.pending_void for g in t.gates.values()):
            return []
        causes = pending_causes(self, uid) if cause is None else dict.fromkeys(t.gates, cause)
        out = []
        for g in ("requirements", "plan", "verify", "code"):
            t = self._state._core.tickets.get(uid)
            ids = sorted(t.gates[g].pending_void) if t is not None else []
            if ids:
                reason = causes.get(g)
                if reason is None:
                    raise StoreError("store.torn_write", f"cannot recover the invalidation cause for {uid} {g}")
                out.append(self._host_append("gate.invalidated", uid, {"gate": g, "cause": reason, "voided": ids}))
        return out

    def logged_event(self, log: str, event_id: str) -> dict[str, Any] | None:
        """The event of ``log`` (``"workspace"`` or a ticket uid) with this id, read from the verified log under the
        lock, or ``None``. The host's reconciliation of an outcome record asks this for each planned event id."""
        with self._locked():
            if log != WORKSPACE:
                check_uid(log)
                self._ensure({log})
            seq = self._seq_of_id(log, event_id)
            return None if seq is None else self._read_event(log, seq)

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
            self._spend_load()
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
            self._spend_load()
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
            for d in find_divergence(
                self._checkpoints, wsk, ws.genesis, self._logs, self._exists, self._held_checkpoint()
            ):
                if d.code == "trust.genesis_mismatch":
                    raise StoreError(d.code, d.detail)
                self._diverged[d.log] = d
        self._index_ok = ws.genesis is not None and self._index.is_current(
            self._sizes_now(), self.workspace_id, ws.genesis
        )
        if self._pins is not None and self._host is not None:
            for (dev, _person), evidence in self._state.revocation_evidence().items():
                rev = evidence["revocation"]
                self._pins.note_revocation(
                    dev, rev["o"]["reason"], rev, self._now(), evidence["device_cert"], evidence["pk_pub"]
                )
        self._recover_transitions()
        self._enforce_revocations()
        if self._pins is not None and self._host is not None:
            for log, info in self._logs.items():
                verified = self._state._core.logs.get(log)
                self._pins.note_heads(info.heads, verified.seq if verified else 0)

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

    @contextlib.contextmanager
    def load_budget(self, limit: int) -> Iterator[None]:
        """Bound ticket reads and creation checks for an optional hint; restore state on exhaustion."""
        saved = {**self.__dict__, "_loaded": set(self._loaded)}
        self._load_budget = limit
        try:
            yield
        except LoadBudgetExceeded:
            self.__dict__.update(saved)
            raise
        finally:
            self._load_budget = saved["_load_budget"]

    def _spend_load(self) -> None:
        if self._load_budget is not None:
            if self._load_budget <= 0:
                raise LoadBudgetExceeded
            self._load_budget -= 1

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
        with self._guard(), self._lock:
            check_state_dirs(self.root)
            self._load_transition_logs()
            pend = self._recover_pending()
            self._loaded |= {m["log"] for _, m in pend if m["log"] != WORKSPACE}
            outside = bool(pend) or not self._fresh()
            if outside:
                self.outside_changes += 1  # a log changed on disk since this store last read or wrote it
            reloaded = outside or self._needs_reload
            if reloaded:
                self._load()
            self._finish_pending(pend)
            self._recover_catchup()
            if reloaded:
                self._enforce_revocations()
            yield

    def _load_transition_logs(self) -> None:
        """A committed restore must finish its checkpoint before any other append, including on a lazy open."""
        if self._pins is None:
            return
        for path in (self._pins.dir / "pending-transitions").glob("*.json"):
            transition = loads(read_or_none(path, None))
            log = transition["log"]
            if log != WORKSPACE and log not in self._loaded:
                self._loaded.add(check_uid(log))
                self._needs_reload = True

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
            self._finish_applied_checkpoint()
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
            if f["to"] == "config.json":
                self._known_config = None  # from here on the file is the store's own
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
        if m.get("outbox") is not None:
            self._install_outbox(m, d, recovering=check is not None)
        self._write_applied(m["id"], m["log"], m["seq"])
        return torn

    def _install_outbox(self, m: dict[str, Any], d: Path, *, recovering: bool) -> None:
        """Install the append's outbox entry (idempotent), only if it verifies and names exactly this event: a WSK
        checkpoint of this workspace and genesis whose entry for the event's log is the event's ``seq`` and head.
        A plain entry's checkpoint is also adopted as the newest workspace checkpoint (as the append itself does
        after the commit), so a crash in between never leaves the outbox ahead of ``workspace.json``. In recovery a
        bad entry is reported (``store.torn_write``) and left out; on the append's own path it raises."""
        if self._outbox is None or self._pins is None:
            # a store without the host state dir cannot install it: keep the pending directory for one that can
            raise StoreError(
                "validation.host_state",
                f"the pending append of event {m['id']} to {m['log']} carries a relay outbox entry; "
                "the next host open finishes it (open with the host state dir)",
            )
        pending = d / "outbox"
        data = None if pending.is_symlink() else read_or_none(pending, 16 * 1024 * 1024)
        try:
            if data is None or _sha(data) != m["outbox"]:
                raise StoreError("store.torn_write", "the pending outbox entry is missing or changed")
            entry = parse_entry(data, wsk_pub=self._wsk_pub(), workspace_id=self.workspace_id, genesis=self._pin)
            o = entry.checkpoint["o"]
            mine = o["workspace_log"] if m["log"] == WORKSPACE else o["tickets"].get(m["log"])
            head = canon.event_head(canon.parse_event_line(m["line"].encode("utf-8")))
            if not mine or mine["seq"] != m["seq"] or mine["head"] != head:
                raise StoreError("store.torn_write", "the pending outbox entry does not name its event")
        except StoreError as e:
            if not recovering:
                raise
            self._report("store.torn_write", f"outbox entry of event {m['id']} not installed: {e.detail}", m["log"])
            return
        self._pins.reserve_n(entry.n)  # idempotent: the append reserved it before signing
        self._outbox.write_raw(entry.n, data)
        if entry.kind == "plain":
            self._coalesce(entry.n)
            self._adopt(entry.checkpoint)

    def _coalesce(self, n: int) -> None:
        """Outbox coalescing (H12b review, Opus F3), right after plain entry ``n`` is written: the entry just below it
        is deleted when it is a plain, non-catch-up entry and not the lowest entry. The relay accepts any higher
        extension of its held checkpoint (``n`` need not be consecutive), so a middle plain entry is superseded by
        the next one. Kept always: the lowest entry (R3 publishes oldest first, so it may be staging), every restore,
        the plain entry just before a restore (its source: its successor is the restore, never a plain entry), every
        catch-up entry and the newest entry. The queue therefore stays at about 2 + 2 x (queued restores) entries.
        Idempotent: recovery may repeat it."""
        assert self._outbox is not None
        names = self._outbox.names()
        ns = [k for k, _ in names]
        if n not in ns:
            return
        i = ns.index(n)
        if i < 2:
            return  # the entry below is the lowest, or there is none
        below, path = names[i - 1]
        raw = None if path.is_symlink() else read_or_none(path, None)
        if raw is None:
            return
        try:
            entry = parse_entry(raw, wsk_pub=self._wsk_pub(), workspace_id=self.workspace_id, genesis=self._pin)
        except StoreError:
            return  # left in place: the reader stops on it
        if entry.kind == "plain" and not entry.catchup and entry.n == below:
            self._outbox.delete(below)

    def _adopt(self, cp: dict[str, Any]) -> None:
        """Make ``cp`` the newest checkpoint: the host pin and ``workspace.json``, each repaired on its own (a crash
        between the two writes is finished by the next recovery, Codex P1), never moved backwards."""
        assert self._pins is not None
        n = cp["o"]["n"]
        held = self._pins.held()
        if held is None or "o" not in held or held["o"]["n"] < n:
            self._pins.accept_checkpoint(cp)
        cur = self._checkpoints.workspace()
        if cur is None or "o" not in cur or cur["o"]["n"] < n:
            self._checkpoints.write_workspace_object(cp)

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
            self._spend_load()
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
        event_id: str | None = None,
    ) -> Appended:
        """Append a ``ticket.created`` for an actor nobody signs (an agent with a grant): allocates the key and the
        uid under the lock, so two processes never take the same number. ``event_id`` plans the event's id (the host
        writes it into its outcome record first, so a crash after the append can be reconciled from the log)."""
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
            if event_id is not None:
                ev["id"] = event_id
            return self._append(ev, new_uid, None, None, idem)

    # -- steps

    def _check_writable(self, log: str, typ: str) -> None:
        if log != WORKSPACE:
            check_uid(log)
        if self._host is None:
            raise StoreError("store.read_only", "no workspace key signer: this store can only read")
        self._finish_applied_checkpoint()
        if log in self._read_errors:
            raise StoreError("chain.broken", f"{log}: {self._read_errors[log]}")
        if WORKSPACE in self._read_errors:
            raise StoreError("chain.broken", f"workspace: {self._read_errors[WORKSPACE]}")
        if typ != "restore" and self._missing_checkpoint(log):
            raise StoreError("checkpoint.missing", f"{log}: missing checkpoint is not explained by the last append")
        if self._workspace_checkpoint_missing():
            raise StoreError(
                "checkpoint.missing", "workspace: no workspace checkpoint is held although the workspace log has grown"
            )
        for failure in self._state.chain_errors if self._state else ():
            if failure.log in (WORKSPACE, log) and failure.code == "chain.broken":
                raise StoreError(failure.code, failure.detail)
        d = self._diverged.get(log) or (self._diverged.get(WORKSPACE) if log != WORKSPACE else None)
        if d is not None and not (typ == "restore" and d.log == log):
            raise StoreError("chain.diverged", f"{d.log}: {d.detail} (an owner-signed restore is needed)")

    def _workspace_checkpoint_missing(self) -> bool:
        """No workspace checkpoint (host pin nor ``.state``) although the log grew past the genesis, on a host whose
        state dir shows checkpoints existed. Every append writes one and a crash keeps the previous one, so this is
        never a crash state: no append and no new baseline; an owner restores (§5.10). A host without such evidence (a
        fresh clone, a lost state dir) starts a baseline instead: F1 "What P1 can't detect".

        ``seq < 2``: the genesis append itself writes the first checkpoint after it is committed, so a log of only the
        genesis may lack one after a crash. (``restore_facts`` uses ``seq >= 1``: any restore needs a source once a
        genesis exists, because a restore always comes after it.)"""
        ws = self._logs.get(WORKSPACE)
        if ws is None or ws.seq < 2 or self._pins is None or not self._pins.checkpoint_evidence():
            return False
        return self._pins.held() is None and self._checkpoints.workspace() is None

    def _missing_checkpoint(self, log: str) -> bool:
        info = self._logs.get(log)
        return bool(
            log != WORKSPACE
            and info is not None
            and info.seq
            and self._checkpoints.ticket(log) is None
            and not self.repairable_checkpoint(log)
        )

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
        if self.before_append is not None:
            self.before_append()
        typ = e.get("type", "")
        if typ == "restore" and self._pins and e.get("id"):
            path = self._pins.dir / "transitions" / (check_uid(e["id"]) + ".json")
            raw = read_or_none(path, None)
            if raw:
                prior = loads(raw)
                previous = prior["event"]
                if (
                    prior.get("committed")
                    and prior["log"] == log
                    and previous["sig"] == e.get("sig")
                    and canon.person_signing_bytes(self.workspace_id, log, previous)
                    == canon.person_signing_bytes(self.workspace_id, log, e)
                ):
                    info = self._logs.get(log)
                    if (
                        info
                        and info.seq >= previous["seq"]
                        and info.heads[previous["seq"] - 1] == canon.event_head(previous)
                    ):
                        return Appended(previous, self._state, True)
        self._check_writable(log, typ)
        actor = e.get("actor")
        host_event = isinstance(actor, dict) and actor.get("kind") == "host"
        if not self._healing:
            self._ensure_for(e, log)
        if typ != "restore" and self._missing_checkpoint(log):
            raise StoreError("checkpoint.missing", f"{log}: missing checkpoint is not explained by the last append")
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
        new_texts = self._section_texts(e, log, body, texts)
        files = self._file_artifacts(e, artifacts)
        if self.append_check is not None and not host_event:
            self.append_check(log, e)
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
        facts = self.restore_facts(log)
        if e.get("source") != facts["source"]:
            raise StoreError("chain.diverged", "restore source is not the held workspace checkpoint")
        signer = e["actor"]["device"]
        signer_pair = (signer, e["actor"]["id"])  # the restore is person-signed: actor id is the signer's person
        if self._pins and any(pair(r) == signer_pair for r in self._pins.revocations()):
            # Replay authorizes against the kept chain, which lacks a revocation from the given-up part; the host's
            # durable evidence still has it, and every receiver refuses a revoked signer (§5.10, C3).
            raise StoreError("device.invalid", f"{signer} is revoked in the host's durable revocation evidence")
        if any("d_" + r["revocation"]["o"]["device_id"] == signer for r in e.get("revocations", [])):
            # §5.10: receivers refuse a bundle that revokes its own signer, so the host never appends one (replay
            # keeps pre-event authorization). Sign from an eligible replacement device, or recover by person key.
            raise StoreError("validation.restore", f"the restore revokes its own signer {signer}")
        if log == WORKSPACE:
            # Completeness per (device, person) pair: another person's entry for the same device never satisfies it.
            supplied = {
                (r["revocation"]["o"]["device_id"], r["revocation"]["o"]["person_id"]): r
                for r in e.get("revocations", [])
            }
            for entry in facts["revocations"]:
                rev = entry["revocation"]
                got = supplied.get((rev["o"]["device_id"], rev["o"]["person_id"]))
                if got is None or (
                    got["revocation"] != rev
                    and (rev["o"]["reason"] == "compromised" or got["revocation"]["o"]["reason"] != "compromised")
                ):
                    raise StoreError("validation.restore", "restore omits or weakens held revocation evidence")
        want = facts["abandoned"]
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
        if ev["type"] == "restore":
            self._check_restore(log, ev)
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
        linked = not genesis and self.outbox_active
        planned: Any = _NOPLAN
        outbox: bytes | None = None
        if linked and ev["type"] != "restore":
            # §11 P3-D21: the exact checkpoint of this append, planned now so that it rides in the commit
            planned = self._plan_workspace_checkpoint(log, ev, new_state)
            if isinstance(planned, dict):
                outbox = make_entry(planned).to_bytes()
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
            if ev["type"] == "restore":
                transition = self._stage_transition(ev, log, old_state)
                if linked:
                    outbox = make_entry(transition["T"], restore=_restore_evidence(transition)).to_bytes()
            manifest = self._write_pending(ev, log, line, writes, outbox=outbox)
            append_durable(path, line, commit=True)  # the commit
            self._finish(manifest, ev, log, line)
        except BaseException:
            self._needs_reload = True  # whatever got half done, the next lock recovers from disk
            raise
        self._state = new_state
        try:
            self._after_commit(old_state, new_state, ev, log, planned)
        except BaseException:
            self._needs_reload = True  # finish a committed transition before the next call, even without reopening
            raise
        finally:
            if self.on_commit is not None:  # the event is in the log whatever the bookkeeping above did
                self.on_commit(log, ev)
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
        now = int(self._clock())  # one sampled clock for the stamp and the ceiling
        t = max(now, last_at)
        if ev["type"] == "restore":
            cp = self._held_checkpoint()
            if cp:
                t = max(t, _epoch(cp["o"]["at"]))
        if log != WORKSPACE:
            wsh = self._logs[WORKSPACE].seq if WORKSPACE in self._logs else 0
            ev["ws_seq"] = wsh
            if last_pos is not None and last_pos[0] == wsh:  # an earlier workspace head leaves any `at` free
                if t < last_pos[1]:
                    t = last_pos[1]
                if t == last_pos[1] and (log, seq) <= (last_pos[2], last_pos[3]):
                    t += 1
        if ev["type"] == "restore" and t > now + 300:
            # §5.10 C2: the computed restore `at` (source, last merged-order at) is checked, not only C's. Host
            # acceptance does not guarantee receiver acceptance: receivers sample their own clocks.
            raise StoreError(
                "validation.restore", "the restore's computed at is more than 300 seconds ahead of the clock"
            )
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
        cur = self._config_json()
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

    def _write_pending(
        self, ev: dict[str, Any], log: str, line: bytes, writes: list[_Write], outbox: bytes | None = None
    ) -> dict[str, Any]:
        """Step 2 of the write order. ``outbox`` is the relay outbox entry of this append (H12b): it rides in the
        pending directory, so the commit (the log line) decides it with the event: neither or both."""
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
        if outbox is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(pdir / "outbox")  # a retried event id: start clean, never write through a link
            fd = os.open(pdir / "outbox", os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(outbox)
                f.flush()
                os.fsync(f.fileno())
            manifest["outbox"] = _sha(outbox)
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
        info.accs.append(canon.log_acc(info.acc, canon.event_head(ev)))
        info.heads.append(canon.event_head(ev))
        sig = stat_sig(info.path)
        info.ino = sig[1] if sig else None
        info.seen_times = (sig[2], sig[3]) if sig else (0, 0)
        if log != WORKSPACE:
            self._loaded.add(log)

    # -- after the commit

    def _after_commit(self, old: State, new: State, ev: dict[str, Any], log: str, planned: Any = _NOPLAN) -> None:
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
        if self._pins is not None:
            info = self._logs[log]
            self._pins.note_head(info.seq, info.head, ev["prev"])
            for (device, _person), entry in (
                new.revocation_evidence().items() if typ in ("restore", "device.revoked") else []
            ):
                rev = entry["revocation"]
                self._pins.note_revocation(
                    device, rev["o"]["reason"], rev, ev["at"], entry["device_cert"], entry["pk_pub"]
                )
        if typ == "restore":
            self._recover_transitions()
        else:
            self._write_checkpoints(log, ev, new, planned)
        changed = {u for u, v in new.tickets.items() if old.tickets.get(u) is not v}
        self._maybe_index(new)
        if new.workspace.genesis is not None and self._index_ok:
            try:
                self._index.update(new, self._logs, [log], changed, self._sizes_now() | {log: self._logs[log].offset})
            except Exception:  # noqa: BLE001 - derived data: never fail an append for it
                self._index.drop()
                self._index_ok = False

    def _write_checkpoints(self, log: str, ev: dict[str, Any], new: State, planned: Any = _NOPLAN) -> None:
        """The checkpoints after a commit. ``planned`` is the workspace checkpoint planned before the commit in a
        relay-linked workspace (every append writes one, and it is the outbox entry's): written as is (the outbox
        install already wrote it, unless that was skipped), never re-signed."""
        assert self._host is not None
        sign = self._host.sign
        info = self._logs[log]
        if log != WORKSPACE:
            self._ticket_appends_since_cp += 1
            if self._checkpoints.write_ticket(sign, log, info.seq, info.head or "", ev["at"], info.acc) == "diverged":
                self._diverged[log] = Divergence(log, info.seq, "a checkpoint at this height has another head")
        if planned is _NOPLAN:
            if log == WORKSPACE or self._ticket_appends_since_cp >= WORKSPACE_EVERY:
                self._write_workspace_checkpoint(ev["at"], new)
        elif isinstance(planned, dict):
            if self._pins is not None:
                self._adopt(planned)
            self._ticket_appends_since_cp = 0
        elif planned == "diverged":
            self._diverged[WORKSPACE] = Divergence(
                WORKSPACE, self._logs[WORKSPACE].seq, "a checkpoint at this height has another head"
            )

    def _workspace_logs(self, state: State) -> dict[str, Any] | None:
        """The logs a workspace checkpoint lists now, or None when none may be written (§5.10)."""
        if state.workspace.genesis is None or self._logs[WORKSPACE].seq == 0:
            return None
        # Do not certify a rolled-back head just because its ticket checkpoint was deleted.
        broken = {failure.log for failure in state.chain_errors} | set(self._read_errors) | set(self._diverged)
        if WORKSPACE in broken:
            return None
        if self._workspace_checkpoint_missing():
            return None  # never start a new baseline after the checkpoint files were lost (§5.10); appends refuse
        return {
            uid: info for uid, info in self._logs.items() if uid not in broken and not self._missing_checkpoint(uid)
        }

    def _plan_workspace_checkpoint(self, log: str, ev: dict[str, Any], state: State) -> dict[str, Any] | str | None:
        """The workspace checkpoint this append will write, signed before the commit (H12b): the logs after the
        append, by the same rule as :meth:`_write_workspace_checkpoint`. ``"diverged"`` or None: no entry."""
        assert self._host is not None
        logs = self._workspace_logs(state)
        if logs is None or state.workspace.genesis is None:
            return None
        if log in self._logs and log not in logs:
            return None  # the post-commit rule leaves this log out (broken, diverged, missing checkpoint): no entry
        if log != WORKSPACE:
            old = self._checkpoints.ticket(log)
            if old is not None and ("o" not in old or old["o"]["seq"] >= ev["seq"]):
                return None  # write_ticket will call it diverged after the commit: certify nothing for it (Opus F5)
        info = self._logs.get(log)
        head = canon.event_head(ev)
        logs[log] = _Cut(ev["seq"], head, canon.log_acc(info.acc if info else _ZERO_ACC, head))
        baseline = self._held_checkpoint()
        return self._checkpoints.build_workspace(
            self._host.sign,
            state.workspace.genesis,
            ev["at"],
            logs,
            baseline,
            n=self._reserve_next(baseline),
        )

    def _number_floor(self) -> int:
        """The checkpoint numbering high-water mark (``HostPins.high_n``): every number ever reserved or accepted."""
        return self._pins.high_n() if self._pins is not None else 0

    def _reserve_next(self, baseline: Mapping[str, Any] | None) -> int | None:
        """Reserve and return the next workspace checkpoint number (H12b review, Opus F1): above every number the
        checkpoints, ``baseline`` and the high-water mark hold. **No ``n`` is signed unless reserved first; reserved
        ``n`` are never reused** (``HostPins.reserve_n``). No outbox scan: every outbox ``n`` was reserved."""
        if self._pins is None:
            return None  # a store without host state numbers by its checkpoint files alone (P1 direct reads)
        n = (
            max(
                self._checkpoints.highest_n(),
                baseline["o"]["n"] if baseline and "o" in baseline else 0,
                self._pins.high_n(),
            )
            + 1
        )
        self._pins.reserve_n(n)
        return n

    def _write_workspace_checkpoint(self, at_: str, state: State) -> None:
        assert self._host is not None
        logs = self._workspace_logs(state)
        if logs is None or state.workspace.genesis is None:
            return
        baseline = self._held_checkpoint()
        if self.outbox_active:
            # linked (H12b): every workspace checkpoint the store writes goes to the outbox too. Order: the number is
            # reserved, then the entry, the pin and workspace.json; a crash anywhere burns the number at worst.
            cp = self._checkpoints.build_workspace(
                self._host.sign,
                state.workspace.genesis,
                at_,
                logs,
                baseline,
                n=self._reserve_next(baseline),
            )
            if isinstance(cp, dict):
                assert self._outbox is not None and self._pins is not None
                self._outbox.write(make_entry(cp))
                self._coalesce(cp["o"]["n"])
                self._adopt(cp)
                self._ticket_appends_since_cp = 0
                return
            self._diverged[WORKSPACE] = Divergence(
                WORKSPACE, self._logs[WORKSPACE].seq, "a checkpoint at this height has another head"
            )
            return
        if (
            self._checkpoints.write_workspace(
                self._host.sign,
                state.workspace.genesis,
                at_,
                logs,
                baseline,
                n=self._reserve_next(baseline),
            )
            == "diverged"
        ):
            self._diverged[WORKSPACE] = Divergence(
                WORKSPACE, self._logs[WORKSPACE].seq, "a checkpoint at this height has another head"
            )
            return
        cp = self._checkpoints.workspace()
        if self._pins is not None and cp and "o" in cp:
            self._pins.accept_checkpoint(cp)
        self._ticket_appends_since_cp = 0

    def repairable_checkpoint(self, uid: str) -> bool:
        """Only the last recorded append can have crashed before its checkpoint write."""
        info = self._logs.get(uid)
        applied = self._read_applied()
        cp = self._checkpoints.workspace()
        if info is None or not info.seq or cp is None:
            return False
        floor = cp.get("o", {}).get("tickets", {}).get(uid, {}).get("seq", 0)
        return (
            applied.get("log") == uid
            and applied.get("seq") == info.seq
            and info.seq >= floor
            and applied.get("id") == self._read_event(uid, info.seq)["id"]
        )

    def repair_checkpoints(self) -> None:
        """Fill only a missing checkpoint explained by the last recorded append."""
        with self._locked():
            self._finish_applied_checkpoint()

    def _finish_applied_checkpoint(self) -> None:
        """Finish the last append's missing ticket checkpoint before its crash evidence moves on."""
        if self._host is None:
            return
        uid = self._read_applied().get("log")
        if not isinstance(uid, str) or not _ULID.fullmatch(uid) or self._checkpoints.ticket(uid) is not None:
            return
        if uid not in self._exists:
            self._scan_dirs()  # another writer may have created the ticket since this store opened
        if uid not in self._exists:
            return
        if uid not in self._loaded:
            self._loaded.add(uid)
            self._load()  # no healing or revocation appends until this checkpoint is finished
        if self._state is None or self._diverged or self.chain_errors() or self._state.workspace.invalid:
            return
        view = self._state.tickets.get(uid)
        if view is None or view.frozen or not self.repairable_checkpoint(uid):
            return
        info = self._logs[uid]
        self._checkpoints.write_ticket(self._host.sign, uid, info.seq, info.head or "", self._now(), info.acc)

    def checkpoint(self) -> None:
        """Write the workspace checkpoint and finish crash-repairable ticket checkpoints. Raises
        ``checkpoint.missing`` when the host must not start a new baseline (§5.10)."""
        with self._locked():
            if self._workspace_checkpoint_missing():
                raise StoreError(
                    "checkpoint.missing",
                    "workspace: this host's state shows checkpoints existed but none is held. "
                    + MISSING_CHECKPOINT_RECOVERY,
                )
            self._checkpoint_all()

    def _checkpoint_all(self) -> None:
        if self._host is None or self._state is None or self._state.workspace.genesis is None or self._diverged:
            return
        if self.chain_errors() or self._state.workspace.invalid:
            return
        at_ = self._now()
        for uid, info in self._logs.items():
            if uid == WORKSPACE or not info.seq or info.error:
                continue
            view = self._state.tickets.get(uid)
            if view is None or view.frozen:
                continue
            cp = self._checkpoints.ticket(uid)
            if (cp is None or "o" not in cp or cp["o"]["seq"] != info.seq) and self.repairable_checkpoint(uid):
                self._checkpoints.write_ticket(self._host.sign, uid, info.seq, info.head or "", at_, info.acc)
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
        return self._read_events(log, seq, seq)[0]

    def _read_events(self, log: str, first: int, last: int) -> list[dict[str, Any]]:
        """The events ``first`` to ``last`` of ``log``, with one open: each line is read at its verified offset (seek,
        do not scan the log)."""
        if first > last:
            return []
        info = self._logs[log]
        fd = open_nofollow(self._path_of(log), os.O_RDONLY)
        out = []
        with os.fdopen(fd, "rb") as f:
            for seq in range(first, last + 1):
                f.seek(info.starts[seq - 1])
                out.append(canon.parse_event_line(f.readline(MAX_LINE + 1)))
        return out

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
            if (self._config_json() if path == "config.json" else read_or_none(self.root / path)) == want:
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
        highest checkpoint above it (``abandoned``, or None) and the ids of every person-signed event (actor P) the host
        still knows of in the part it archived with :meth:`abandon_tail` (``abandoned_decisions``)."""
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
        held = self._held_checkpoint()
        if held is None and self._logs[WORKSPACE].seq >= 1:
            # seq >= 1 (not the append guard's >= 2): a restore always follows the genesis, whose append wrote the
            # first checkpoint. Every append writes a workspace checkpoint, so a workspace log is evidence: a null
            # source would start a new baseline after every checkpoint file was deleted (§5.10).
            raise StoreError("chain.diverged", "workspace checkpoint source is missing but the workspace log exists")
        if held:
            entry = held["o"]["workspace_log"] if log == WORKSPACE else held["o"]["tickets"].get(log)
            if entry and entry["seq"] > info.seq:
                candidate = {"seq": entry["seq"], "head": entry["head"]}
                if abandoned and candidate["seq"] == abandoned["seq"] and candidate != abandoned:
                    raise StoreError("chain.diverged", "equal-height conflicting abandoned checkpoints")
                if abandoned is None or candidate["seq"] > abandoned["seq"]:
                    abandoned = candidate
        decisions: list[str] = []
        for p in (
            sorted((self.state_dir / "abandoned").glob(f"{log}-*.jsonl"))
            if (self.state_dir / "abandoned").is_dir()
            else []
        ):
            for line in p.read_bytes().splitlines():
                with contextlib.suppress(canon.HashError, KeyError):
                    e = canon.parse_event_line(line + b"\n")
                    if e["actor"]["kind"] == "person":  # §5.10: every person-signed event of the given-up part
                        decisions.append(e["id"])
        return {
            "from_seq": info.seq,
            "head": info.head,
            "abandoned": abandoned,
            "abandoned_decisions": sorted(set(decisions)),
            "source": {"n": held["o"]["n"], "hash": canon.checkpoint_hash(held["o"])} if held else None,
            "revocations": [
                {k: r[k] for k in ("revocation", "device_cert", "pk_pub")}
                for r in self._pins.revocations()
                if self._carries(r)
            ]
            if log == WORKSPACE and self._pins
            else [],
        }

    def _carries(self, rec: dict[str, Any]) -> bool:
        """Whether a restore carries this held pair: all chain evidence; forwarded evidence only for a live device
        the workspace associates with the same person. Forwarded evidence for an unknown or tombstone-only device, or
        one associated with another person, stays pending (§5.10): it still refuses its signer pair."""
        if not is_forwarded(rec):
            return True
        if self._state is None:
            return False
        device, person = pair(rec)
        return self._state.device_person(device) == person

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
            self._mark_cut_convenience(cut)  # #441: before the events leave the merged view (fail closed)
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

    def _mark_cut_convenience(self, cut: bytes) -> None:
        """A cut tail holding a convenience event sets the workspace's durable ``convenience_seen`` mark first
        (:mod:`orch.ops.readers`; readers may have read those events). Only a canonical event line counts
        (``canon.parse_event_line``, Opus #454 L4); a torn or forged-looking line does not. A mark that cannot be
        written refuses the cut."""
        from orch.ops.readers import canonical_lines_have_convenience, ensure_convenience_mark

        if not canonical_lines_have_convenience(cut):
            return
        if self._pins is None:
            raise StoreError("validation.host_state", "cutting convenience events needs the host state (the mark)")
        try:
            ensure_convenience_mark(self._pins.dir.parent.parent, self.workspace_id, os.getuid())
        except Exception as exc:  # noqa: BLE001 - any doubt refuses the cut
            raise StoreError(
                "internal", f"the convenience_seen mark could not be written ({type(exc).__name__}); nothing was cut"
            ) from None

    def _enforce_revocations(self) -> None:
        if self._pins is None or self._state is None:
            return
        accepted = self._state.revocation_evidence()
        for rec in self._pins.revocations():
            if is_forwarded(rec):
                continue  # forwarded evidence: enforced as signer refusal, never claimed by accepted history
            entry = accepted.get(pair(rec))
            if entry is None or entry["revocation"] != rec["revocation"]:
                self._diverged.setdefault(
                    WORKSPACE,
                    Divergence(
                        WORKSPACE,
                        self._logs[WORKSPACE].seq,
                        f"the host noted a revocation of {rec['device']} that accepted history lacks",
                    ),
                )

    def _held_checkpoint(self) -> dict[str, Any] | None:
        cp = self._pins.held() if self._pins else None
        if cp is None:
            cp = self._checkpoints.workspace()
        if cp is None:
            if (
                self._checkpoints.ticket_uids()
                or any((self._checkpoints.dir / "abandoned").glob("workspace-*.json"))
                or (self._pins and any((self._pins.dir / "transitions").glob("*.json")))
            ):
                raise StoreError(
                    "chain.diverged", "workspace checkpoint source is missing but checkpoint evidence exists"
                )
            return None
        from .checkpoints import verify_object

        if (
            not verify_object(self._wsk_pub(), cp)
            or cp["o"]["kind"] != "workspace_checkpoint"
            or cp["o"]["workspace_id"] != self.workspace_id
            or cp["o"]["genesis"] != self._pin
        ):
            raise StoreError("chain.diverged", "invalid held workspace checkpoint")
        return cp

    def _stage_transition(self, ev: dict[str, Any], log: str, state: State) -> dict[str, Any]:
        from .checkpoints import _extension, sign_object

        assert self._pins is not None and self._host is not None
        cp = self._held_checkpoint()
        info = self._logs[log]
        entry = {"seq": ev["seq"], "head": canon.event_head(ev), "acc": canon.log_acc(info.acc, canon.event_head(ev))}
        tickets = dict(cp["o"]["tickets"]) if cp else {}
        broken = {failure.log for failure in state.chain_errors} | set(self._read_errors) | set(self._diverged)
        for uid, item in self._logs.items():
            if uid != WORKSPACE and item.seq and uid not in broken:
                tickets[uid] = {"seq": item.seq, "head": item.head, "acc": item.acc}
        ws = self._logs[WORKSPACE]
        workspace_entry = {"seq": ws.seq, "head": ws.head, "acc": ws.acc}
        if log == WORKSPACE:
            workspace_entry = entry
        else:
            tickets[log] = entry
        target = sign_object(
            self._host.sign,
            {
                "v": 2,
                "suite": 2,
                "kind": "workspace_checkpoint",
                "workspace_id": self.workspace_id,
                "genesis": self._pin,
                "n": self._reserve_next(cp),
                "at": ev["at"],
                "workspace_log": workspace_entry,
                "tickets": dict(sorted(tickets.items())),
            },
        )
        baseline = (cp["o"]["workspace_log"] if log == WORKSPACE else cp["o"]["tickets"].get(log)) if cp else None
        abandoned = self._pins.branch(baseline["head"], info.seq) if baseline and baseline["seq"] > info.seq else []
        if baseline and baseline["seq"] > info.seq and abandoned is not None:
            if not _extension({"seq": info.seq, "head": info.head, "acc": info.acc}, baseline, abandoned):
                abandoned = None
        forward = (
            info.heads[baseline["seq"] if baseline else 0 :] if not baseline or info.seq >= baseline["seq"] else []
        )
        if baseline and info.seq >= baseline["seq"]:
            if not _extension(baseline, {"seq": info.seq, "head": info.head, "acc": info.acc}, forward):
                abandoned = None  # a retained fork cannot be published as an extension of C
        evidence = state.device_evidence(ev["actor"]["device"])
        proofs = {log: [entry["head"]]}
        if cp:
            for uid, item in self._logs.items():
                if uid == log:
                    continue
                prior = cp["o"]["workspace_log"] if uid == WORKSPACE else cp["o"]["tickets"].get(uid)
                proofs[uid] = item.heads[prior["seq"] if prior else 0 :]
        transition = {
            "committed": False,
            "C": cp,
            "T": target,
            "log": log,
            "event": ev,
            **evidence,
            "delegation": self._genesis_event["delegation"],
            "proofs": proofs,
            "acc_from": info.acc,
            "acc_before": info.accs[-2] if info.seq > 1 else "sha256:" + "0" * 64,
            "forward_heads": forward,
            "abandoned_heads": abandoned,
            "publication_complete": abandoned is not None,
        }
        directory = self._pins.dir / "pending-transitions"
        directory.mkdir(parents=True, exist_ok=True)
        write_atomic(directory / (ev["id"] + ".json"), canon.cj_checked(transition) + b"\n", mode=0o600)
        return transition

    def _recover_transitions(self) -> None:
        if self._pins is None or self._host is None or self._state is None:
            return
        for path in sorted((self._pins.dir / "pending-transitions").glob("*.json")):
            transition = loads(read_or_none(path, None))
            ev, log, target = transition["event"], transition["log"], transition["T"]
            from .checkpoints import verify_object

            if (
                not verify_object(self._wsk_pub(), target)
                or target["o"]["workspace_id"] != self.workspace_id
                or target["o"]["genesis"] != self._pin
            ):
                raise StoreError("chain.diverged", "invalid pending transition checkpoint")
            info = self._logs.get(log)
            if info is None or info.seq < ev["seq"]:
                continue  # not appended (yet): the restore may still be committed by a retry
            if info.heads[ev["seq"] - 1] != canon.event_head(ev):
                _drop(path)  # its slot holds another head: this restore can never commit
                continue
            if any(i.id == ev["id"] for i in self._state._core.logs[log].invalid):
                continue
            held = self._held_checkpoint()
            if held and held["o"]["n"] > target["o"]["n"]:
                _drop(path)  # the held checkpoint moved past T: the transition is stale
                continue
            if held != transition["C"] and held != target:
                raise StoreError("chain.diverged", "pending restore source differs from held checkpoint")
            self._checkpoints.abandon_above(log, ev["from_seq"])
            self._checkpoints._write(self._checkpoints._workspace_path, target)
            if log != WORKSPACE:
                entry = target["o"]["tickets"][log]
                self._checkpoints.write_ticket(
                    self._host.sign, log, entry["seq"], entry["head"], target["o"]["at"], entry["acc"]
                )
            self._pins.accept_checkpoint(target)
            transition["committed"] = True
            write_atomic(path, canon.cj_checked(transition) + b"\n", mode=0o600)
            completed = self._pins.dir / "transitions"
            completed.mkdir(parents=True, exist_ok=True)
            os.replace(path, completed / path.name)
            fsync_dir(completed)
            fsync_dir(path.parent)
            self._diverged.pop(log, None)

    # ------------------------------------------------------------------ the relay outbox (H12b, §11, §11A)

    @property
    def outbox_active(self) -> bool:
        """Whether appends write outbox entries: the workspace is relay-linked (``relay/link.json`` exists under
        ``<state>/hosts/<ws>/``, written by R1 under the store lock before any network write, 11A.5)."""
        return self._pins is not None and os.path.lexists(self._pins.dir / "relay" / "link.json")

    def _relay_dir(self) -> Path:
        if self._pins is None:
            raise StoreError("validation.host_state", "a relay link needs the host state dir")
        d = self._pins.dir / "relay"
        if d.is_symlink():
            raise StoreError("store.torn_write", "the relay state directory is a symlink")
        return d

    def relay_link(
        self, link: Mapping[str, Any], *, guard: Callable[[], contextlib.AbstractContextManager[object]]
    ) -> None:
        """Write the reader reservation ``relay/link.json`` (11A.5 first link, 11A.6) and so turn the outbox on.

        ``with self.locked(): with guard(): …``: under the store lock, then inside ``guard()`` (the host passes
        ``orch.host.relaylink.link_reservation(service)``: the host-wide lock and the ``sign.convenience_readers`` /
        ``host.limit`` refusals, held across the write so two workspaces cannot pass the cap together), refuse when
        ``link.json`` exists (``relay.link_exists``), then write ``{**link, "state": "linking"}`` atomically
        (fsync, 0600; the directory 0700). From that moment every append carries its outbox entry: appends take
        the same lock. If ``guard()`` raises, nothing is written. ``link`` must be an object naming this workspace
        (if it names one) and no state other than ``linking`` (``validation.relay_link``)."""
        if (
            not isinstance(link, Mapping)
            or link.get("workspace_id", self.workspace_id) != self.workspace_id
            or link.get("state", "linking") != "linking"
        ):
            raise StoreError("validation.relay_link", "link.json names another workspace or a state past linking")
        body = canon.cj_checked({**link, "state": "linking"}) + b"\n"
        with self._locked(), guard():
            d = self._relay_dir()
            path = d / "link.json"
            if os.path.lexists(path):
                raise StoreError("relay.link_exists", "the workspace already has a relay link")
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(d, 0o700)
            write_atomic(path, body, mode=0o600)
            fsync_dir(d)
            fsync_dir(d.parent)  # a new relay/ must survive a crash too (review round 2): still under both locks

    def relay_mark_linked(self, expect: Mapping[str, Any]) -> None:
        """Rewrite ``relay/link.json`` from ``linking`` to ``linked`` under the store lock, only if it holds exactly
        ``expect`` (state ``linking``); otherwise ``relay.link_mismatch`` and nothing is written."""
        with self._locked():
            d = self._relay_dir()
            path = d / "link.json"
            raw = None if path.is_symlink() else read_or_none(path, 1_000_000)
            try:
                have = loads(raw) if raw is not None else None
            except ValueError:
                have = None
            if have is None or dict(expect) != have or have.get("state") != "linking":
                raise StoreError("relay.link_mismatch", "link.json is not the expected linking record")
            write_atomic(path, canon.cj_checked({**have, "state": "linked"}) + b"\n", mode=0o600)
            fsync_dir(d)

    def outbox_entries(self, limit: int | None = None) -> list[OutboxEntry]:
        """The outbox entries, oldest (lowest ``n``) first, each verified (WSK signature, workspace, genesis, ``n``
        and hash; a restore's target holds its line); only the first ``limit`` when given. Takes the lock. Raises
        ``store.torn_write`` for an entry that does not verify: the publisher stops rather than skip or publish it."""
        if limit is not None and (type(limit) is not int or limit < 1):
            raise StoreError("validation.outbox", "limit is a positive integer")
        with self._locked():
            return self._read_outbox(limit)

    def outbox_first(self) -> OutboxEntry | None:
        """The lowest entry (the one R3 publishes next), verified, or None when the outbox is empty."""
        got = self.outbox_entries(limit=1)
        return got[0] if got else None

    def _read_outbox(self, limit: int | None = None) -> list[OutboxEntry]:
        if self._outbox is None:
            return []
        return self._outbox.read(
            wsk_pub=self._wsk_pub(), workspace_id=self.workspace_id, genesis=self._pin, limit=limit
        )

    def outbox_delete(self, n: int, hash: str) -> bool:
        """Delete entry ``n`` after the relay accepted it (R3: after a 200 whose ``{n, hash}`` equals the entry's,
        and after the publication history is appended). ``hash`` must be the entry's checkpoint hash, else
        ``outbox.mismatch`` and nothing is deleted. Returns False when there is no such entry (idempotent)."""
        with self._locked():
            if self._outbox is None:
                return False
            path = self._outbox.path(n)
            if path.is_symlink():
                raise StoreError("store.torn_write", f"outbox entry {n} is a symlink")
            raw = read_or_none(path, None)
            if raw is None:
                return False
            entry = parse_entry(raw, wsk_pub=self._wsk_pub(), workspace_id=self.workspace_id, genesis=self._pin)
            if entry.n != n or entry.hash != hash:
                raise StoreError("outbox.mismatch", f"outbox entry {n} holds another checkpoint")
            return self._outbox.delete(n)

    # ------------------------------------------------------------------ R3's reads (the relay publisher, §11A.5)

    def relay_segment(
        self,
        log: str,
        after: Mapping[str, Any],
        upto: Mapping[str, Any],
        *,
        skip: int = 0,
        limit: int | None = None,
    ) -> Segment:
        """Heads of ``log`` between two checkpoint entries, for one staging page of R3 (§11A.5).

        ``after`` is ``{seq, acc}`` (and ``head`` when ``seq`` > 0; ``seq`` 0 with the zero accumulator for a log the
        relay does not hold yet) and ``upto`` is ``{seq, head, acc}``. The path is the current log when both are points
        of it (O(1) per call: the accumulators are known), else the recorded ancestry (``heads.sqlite``: an abandoned
        branch, for a republication across an accepted restore), checked whole: exactly ``upto.seq - after.seq`` heads
        folding from ``after.acc`` to ``upto.acc`` and ending at ``upto.head``. Returns the part ``skip`` heads in, at
        most ``limit`` heads, with the accumulator before it. Takes the lock. Refuses ``validation.relay`` for a
        malformed range or a branch above :data:`RELAY_BRANCH_MAX`, ``chain.broken`` for an unreadable or unverified
        log, ``chain.diverged`` when no verified path joins the two entries. One verified branch is cached, so paging
        through it costs one walk.

        Not side-effect-free: taking the lock runs the store's recovery (pending appends, a catch-up journal) and may
        load and replay ticket logs, which can write projections; call it as a worker job like every store method."""
        try:
            a_seq, a_acc = after["seq"], after["acc"]
            u_seq, u_head, u_acc = upto["seq"], upto["head"], upto["acc"]
        except (KeyError, TypeError):
            raise StoreError("validation.relay", "a relay segment joins two log entries") from None
        if (
            type(a_seq) is not int
            or type(u_seq) is not int
            or not 0 <= a_seq <= u_seq
            or type(skip) is not int
            or not 0 <= skip <= u_seq - a_seq
            or (limit is not None and (type(limit) is not int or limit < 1))
        ):
            raise StoreError("validation.relay", "a relay segment is a range between two entries of one log")
        span = u_seq - a_seq - skip
        take = span if limit is None else min(limit, span)
        with self._locked():
            if log != WORKSPACE:
                check_uid(log)
                self._ensure({log})
            if log in self._read_errors or log in self._unverified:
                raise StoreError("chain.broken", f"{log}: unreadable or unverified; nothing is published")
            info = self._logs.get(log)

            def point(seq: int, acc: object, head: object) -> bool:
                if info is None:
                    return seq == 0 and acc == _ZERO_ACC
                if seq == 0:
                    return acc == _ZERO_ACC
                return seq <= info.seq and info.accs[seq - 1] == acc and info.heads[seq - 1] == head

            if point(a_seq, a_acc, after.get("head")) and point(u_seq, u_acc, u_head):
                assert info is not None or u_seq == 0
                start = a_seq + skip
                acc = (info.accs[start - 1] if start else _ZERO_ACC) if info is not None else _ZERO_ACC
                heads = tuple(info.heads[start : start + take]) if info is not None else ()
                return Segment(log, start, acc, heads)
            from .checkpoints import _extension

            if u_seq - a_seq > RELAY_BRANCH_MAX:  # review round 2, P2: the walk is bounded by the largest set
                raise StoreError("validation.relay", f"{log}: a branch of {u_seq - a_seq} heads is above the bound")
            key = (log, a_seq, a_acc, u_seq, u_head, u_acc)
            cached = self._relay_branch
            if cached is None or cached[0] != key:
                old = {"seq": a_seq, "head": after.get("head"), "acc": a_acc}
                path = [] if u_seq == a_seq else (self._pins.branch(u_head, a_seq) if self._pins else None)
                if path is None or not _extension(old, {"seq": u_seq, "head": u_head, "acc": u_acc}, path):
                    raise StoreError("chain.diverged", f"{log}: no verified path between the two entries")
                accs, acc = [a_acc], a_acc
                for head in path:
                    acc = canon.log_acc(acc, head)
                    accs.append(acc)
                # one verified branch is kept (the publisher pages through one run at a time): every later page of it
                # is a slice, not a second walk and fold under the lock
                cached = self._relay_branch = (key, tuple(path), tuple(accs))
            _, path, accs = cached
            return Segment(log, a_seq + skip, accs[skip], tuple(path[skip : skip + take]))

    def relay_position(self, checkpoint: Mapping[str, Any] | None) -> RelayPosition:
        """Classify the relay's held checkpoint H (already verified by R1's reader) against this host, for R3's
        startup classification (§11A.5): whether the current logs contain it, the host's numbering high-water mark,
        the newest local workspace checkpoint, and whether H is an abandoned workspace checkpoint. Takes the lock and
        loads every ticket log. Refuses ``checkpoint.prefix_invalid`` for an H that is not this workspace's WSK
        checkpoint; reads ``abandoned/`` through pinned directory fds (a symlinked component is ``validation.path``).

        Not side-effect-free: the lock runs recovery and loading every ticket log may write projections."""
        with self._locked():
            if self._pins is None or self._outbox is None:
                raise StoreError("validation.host_state", "the relay position needs the host state dir")
            self._scan_dirs()
            self._ensure(set(self._exists))
            held = self._pins.held()
            mine = [cp for cp in (held, self._checkpoints.workspace()) if cp and "o" in cp]
            numbering = max(
                [self._checkpoints.highest_n(), self._pins.high_n(), self._outbox.max_n()]
                + [cp["o"]["n"] for cp in mine]
            )
            newest = max(mine, key=lambda cp: cp["o"]["n"], default=None)
            current = {"n": newest["o"]["n"], "hash": canon.checkpoint_hash(newest["o"])} if newest else None
            if checkpoint is None:
                return RelayPosition(True, numbering, current, False)
            o = self._relay_checkpoint(checkpoint)
            contained = True
            for log, entry in {WORKSPACE: o["workspace_log"], **o["tickets"]}.items():
                info = self._logs.get(log)
                seq = entry["seq"]
                if (
                    info is None
                    or log in self._read_errors
                    or not 1 <= seq <= info.seq
                    or info.heads[seq - 1] != entry["head"]
                    or info.accs[seq - 1] != entry["acc"]
                ):
                    contained = False
                    break
            abandoned = False
            if not contained:
                h = canon.checkpoint_hash(o)
                with contextlib.closing(self._abandoned_workspace_checkpoints()) as found:
                    for raw in found:
                        try:
                            cp = loads(raw)
                            abandoned = canon.checkpoint_hash(cp["o"]) == h
                        except (ValueError, KeyError, TypeError, canon.HashError):
                            continue
                        if abandoned:
                            break
            return RelayPosition(contained, numbering, current, abandoned)

    def _abandoned_workspace_checkpoints(self) -> Iterator[bytes]:
        """The bytes of each ``.state/checkpoints/abandoned/workspace-*.json``, read through directory fds pinned from
        the workspace root one component at a time (``O_DIRECTORY | O_NOFOLLOW``), each file opened relative to its
        directory with ``O_NOFOLLOW`` and checked to be a regular file (review round 2, P1): a symlinked component
        anywhere on the path is refused (``validation.path``), never followed outside the store; a missing one means
        none. At most 4 MiB per file."""
        rel = self._checkpoints.dir.relative_to(self.root) / "abandoned"
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fds: list[int] = []
        try:
            fds.append(os.open(self.root, flags))
            for part in rel.parts:
                try:
                    fds.append(os.open(part, flags, dir_fd=fds[-1]))
                except FileNotFoundError:
                    return
                except OSError:  # ELOOP or ENOTDIR: a symlink or not a directory
                    raise StoreError("validation.path", f"{rel}: a component is a symlink or not a directory") from None
            dfd = fds[-1]
            for name in sorted(os.listdir(dfd)):
                if not (name.startswith("workspace-") and name.endswith(".json")):
                    continue
                try:
                    ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=dfd)
                except OSError:
                    continue  # a symlinked entry is not evidence
                try:
                    st = os.fstat(ffd)
                    if not _S_ISREG(st.st_mode) or st.st_size > 4 << 20:
                        continue
                    chunks, size = [], 0
                    while piece := os.read(ffd, 1 << 16):
                        size += len(piece)
                        if size > 4 << 20:
                            break
                        chunks.append(piece)
                    else:
                        yield b"".join(chunks)
                finally:
                    os.close(ffd)
        finally:
            for fd in reversed(fds):
                os.close(fd)

    def relay_ancestor(self, older: Mapping[str, Any], newer: Mapping[str, Any]) -> bool:
        """Whether checkpoint ``older`` is ``newer`` or an ancestor of it: every log of ``older`` is in ``newer`` at or
        past its ``seq``, with a verified path between (the current log, or the recorded ancestry of an abandoned
        branch). R3 uses it to explain a relay H that a restore abandoned (§11A.5). Takes the lock (which runs recovery:
        not side-effect-free). A walk above :data:`RELAY_BRANCH_MAX` heads is refused ``validation.relay``."""
        from .checkpoints import _extension

        with self._locked():
            if self._pins is None:
                return False
            a, b = self._relay_checkpoint(older), self._relay_checkpoint(newer)
            new = {WORKSPACE: b["workspace_log"], **b["tickets"]}
            for log, eo in {WORKSPACE: a["workspace_log"], **a["tickets"]}.items():
                en = new.get(log)
                if en is None or en["seq"] < eo["seq"]:
                    return False
                if en["seq"] - eo["seq"] > RELAY_BRANCH_MAX:  # review round 2, P2: a bounded walk, refused above it
                    raise StoreError("validation.relay", f"{log}: an ancestry walk above the bound")
                if en["seq"] == eo["seq"]:
                    if en != eo:
                        return False
                    continue
                path = self._pins.branch(en["head"], eo["seq"])
                if path is None or not _extension(eo, en, path):
                    return False
            return True

    def _relay_checkpoint(self, cp: Mapping[str, Any]) -> dict[str, Any]:
        from .checkpoints import verify_object

        wsk = self._wsk_pub()
        try:
            ok = (
                wsk is not None
                and verify_object(wsk, cp)
                and cp["o"]["kind"] == "workspace_checkpoint"
                and cp["o"]["workspace_id"] == self.workspace_id
                and cp["o"]["genesis"] == self._pin
            )
        except (KeyError, TypeError):
            ok = False
        if not ok:
            raise StoreError("checkpoint.prefix_invalid", "not a checkpoint of this workspace's key")
        return cp["o"]

    def _recover_catchup(self) -> None:
        """Finish a catch-up replacement whose journal was committed (§11A.5 step 4): before anything numbers a
        checkpoint or reads the outbox."""
        if self._outbox is None:
            return
        intent = self._outbox.read_intent()
        if intent is not None:
            self._apply_catchup(intent)

    def _apply_catchup(self, intent: dict[str, Any]) -> None:
        assert self._outbox is not None and self._pins is not None
        try:
            if set(intent) != {"v", "write", "delete"} or intent["v"] != 1:
                raise StoreError("store.torn_write", "unreadable outbox catch-up journal")
            entries = [
                parse_entry(e, wsk_pub=self._wsk_pub(), workspace_id=self.workspace_id, genesis=self._pin)
                for e in intent["write"]
            ]
            gone = [int(n) for n in intent["delete"] if type(n) is int]
            if not entries or len(gone) != len(intent["delete"]) or any(e.kind != "plain" for e in entries):
                raise StoreError("store.torn_write", "unreadable outbox catch-up journal")
        except (KeyError, TypeError) as e:
            raise StoreError("store.torn_write", f"unreadable outbox catch-up journal ({e})") from None
        self._pins.reserve_n(max(e.n for e in entries))  # idempotent: mint_catchup reserved them before signing
        for entry in entries:
            self._outbox.write(entry)
        keep = {e.n for e in entries}
        for n in gone:
            if n not in keep:
                self._outbox.delete(n)
        last = entries[-1].checkpoint
        cur = self._checkpoints.workspace()
        if cur is None or "o" not in cur or cur["o"]["n"] < last["o"]["n"]:
            self._checkpoints.write_workspace_object(last)
        held = self._pins.held()
        if held is None or "o" not in held or held["o"]["n"] < last["o"]["n"]:
            self._pins.accept_checkpoint(last)
        self._outbox.drop_intent()

    def mint_catchup(
        self,
        *,
        source: Mapping[str, Any] | None,
        cap: int,
        measure: Callable[[tuple[Segment, ...]], int],
    ) -> list[OutboxEntry]:
        """Mint catch-up checkpoints (§11A.5, P3-D37): prefixes Q1 ⊂ … ⊂ Qk from ``source`` (the relay's held
        checkpoint, verified again here; None when the relay holds none) to a fresh snapshot of the current logs,
        each step set within ``cap`` bytes by ``measure`` (R3's exact counter over the step's :class:`Segment` s, one
        per log that grows, the workspace log first), numbered ``m+1 … m+k`` above every existing ``n`` with one
        ``at``, signed by :meth:`Checkpoints.sign_prefix`. In one transaction (a journal) the outbox's plain entries
        are replaced by Q1 … Qk and Qk becomes ``workspace.json``. Returns the new entries; [] when nothing lies
        beyond ``source``.

        Refuses, changing nothing: ``relay.too_large`` when the outbox holds a restore (an anchor: catch-up would
        need numbers below an existing ``n``, so it never synthesizes anything there) or a single event's step does
        not fit; ``checkpoint.prefix_invalid`` for a source that does not verify; ``chain.diverged`` for a source the
        logs do not contain or numbered above the host's numbering; ``chain.broken``/``chain.diverged`` while any log
        is broken or diverged."""
        if type(cap) is not int or cap <= 0:
            raise StoreError("validation.cap", "cap is a positive number of bytes")
        with self._locked():
            if self._host is None or self._pins is None or self._outbox is None:
                raise StoreError("store.read_only", "no workspace key signer: this store can only read")
            self._scan_dirs()
            self._ensure(set(self._exists))
            assert self._state is not None
            self._refuse_unpublishable()
            entries = self._read_outbox()
            if any(e.kind == "restore" for e in entries):
                raise StoreError("relay.too_large", "a queued restore is an anchor: no catch-up across it (§11A.5)")
            held = self._pins.held()
            m = max(
                self._checkpoints.highest_n(),
                self._pins.high_n(),
                self._outbox.max_n(),
                held["o"]["n"] if held and "o" in held else 0,
            )
            base = self._catchup_source(source, m)
            order = self._order_beyond(base)
            if not order:
                return []
            heads_before = {log: info.seq for log, info in self._logs.items()}
            cuts = self._plan_cuts(order, base, cap, measure)
            # R3's measure callback ran in between: whatever it did, the numbering is read again after it, and the
            # plan must still describe the logs (review round 2)
            if {log: info.seq for log, info in self._logs.items()} != heads_before or not self._fresh():
                # also a write through another Store on this thread (the lock is re-entrant): Opus #456 E1
                raise StoreError("checkpoint.prefix_invalid", "the logs changed while the catch-up was planned")
            held = self._pins.held()
            m_before = m
            m = max(
                m,
                self._checkpoints.highest_n(),
                self._pins.high_n(),
                self._outbox.max_n(),
                held["o"]["n"] if held and "o" in held else 0,
            )
            if m != m_before:  # the callback moved the numbering: refuse, R3 re-plans (Opus #456 E1)
                raise StoreError("checkpoint.prefix_invalid", "the numbering moved while the catch-up was planned")
            keys = {(log, seq): k for k, log, seq in order}
            last_at = max(
                [self._now(), *(self._log_last_at(log) for log in self._logs)]
                + [cp["o"]["at"] for cp in (self._checkpoints.workspace(), held, source) if cp and "o" in cp]
                + [e.checkpoint["o"]["at"] for e in entries]
            )
            self._pins.reserve_n(m + len(cuts))  # before anything numbered m+1 … m+k is signed (Opus F1)
            prev: Mapping[str, Any] | None = source
            minted: list[OutboxEntry] = []
            for i, cut in enumerate(cuts):
                cp = self._checkpoints.sign_prefix(
                    self._host.sign,
                    genesis=self._pin or "",
                    n=m + 1 + i,
                    at=last_at,
                    cut=cut,
                    logs=self._logs,
                    base=prev,
                    key=lambda log, seq: keys[(log, seq)],
                    floor_n=m + i,
                )
                minted.append(make_entry(cp, catchup=True))
                prev = cp
            final = minted[-1].checkpoint["o"]
            for log, info in self._logs.items():  # Qk is the fresh snapshot: every log at its current head
                entry = final["workspace_log"] if log == WORKSPACE else final["tickets"].get(log)
                if info.seq and (entry is None or entry["seq"] != info.seq):
                    raise StoreError("checkpoint.prefix_invalid", f"{log}: the last prefix is not the current log")
            intent = {"v": 1, "write": [e.to_json() for e in minted], "delete": [e.n for e in entries]}
            self._outbox.write_intent(intent)  # the commit point of the replacement
            self._apply_catchup(intent)
            return minted

    def _refuse_unpublishable(self) -> None:
        assert self._state is not None
        if self._state.workspace.genesis is None:
            raise StoreError("chain.broken", "no genesis")
        if self._read_errors or self._unverified:
            log = sorted(set(self._read_errors) | self._unverified)[0]
            raise StoreError("chain.broken", f"{log}: unreadable or unverified; nothing is published")
        if self._state.chain_errors:
            failure = self._state.chain_errors[0]
            raise StoreError(failure.code, failure.detail)
        if self._diverged:
            d = next(iter(self._diverged.values()))
            raise StoreError("chain.diverged", f"{d.log}: {d.detail}")
        if self._workspace_checkpoint_missing():
            raise StoreError("checkpoint.missing", "workspace: no workspace checkpoint is held")
        for log in self._logs:  # never certify a head whose ticket checkpoint is missing (Opus F4, as _workspace_logs)
            if self._missing_checkpoint(log):
                raise StoreError("checkpoint.missing", f"{log}: missing checkpoint is not explained by the last append")

    def _catchup_source(self, source: Mapping[str, Any] | None, m: int) -> dict[str, tuple[int, str, str]]:
        """``source``'s per-log cut, after checking it is this workspace's WSK checkpoint and the logs contain it."""
        if source is None:
            return {}
        from .checkpoints import verify_object

        wsk = self._wsk_pub()
        try:
            ok = (
                wsk is not None
                and verify_object(wsk, source)
                and source["o"]["kind"] == "workspace_checkpoint"
                and source["o"]["workspace_id"] == self.workspace_id
                and source["o"]["genesis"] == self._pin
            )
        except (KeyError, TypeError):
            ok = False
        if not ok:
            raise StoreError("checkpoint.prefix_invalid", "the source is not a checkpoint of this workspace's key")
        o = source["o"]
        if o["n"] > m:
            raise StoreError("chain.diverged", f"the source n {o['n']} is above the host's numbering {m}")
        out = {}
        for log, entry in {WORKSPACE: o["workspace_log"], **o["tickets"]}.items():
            info = self._logs.get(log)
            seq = entry["seq"]
            if info is None or not 1 <= seq <= info.seq or info.heads[seq - 1] != entry["head"]:
                raise StoreError("chain.diverged", f"{log}: the source is not contained in the logs")
            if info.accs[seq - 1] != entry["acc"]:
                raise StoreError("chain.diverged", f"{log}: the source's accumulator differs from the log's")
            out[log] = (seq, entry["head"], entry["acc"])
        return out

    def _order_beyond(self, base: Mapping[str, tuple[int, str, str]]) -> list[tuple[tuple[Any, ...], str, int]]:
        """Every event past ``base``, as ``(merged-order key, log, seq)``, sorted (§5.5: the workspace log by
        ``seq``; a ticket event by ``ws_seq``, ``at``, uid, ``seq``)."""
        order: list[tuple[tuple[Any, ...], str, int]] = []
        for log, info in self._logs.items():
            start = base[log][0] if log in base else 0
            if log == WORKSPACE:
                order.extend(((seq, 0, 0, "", seq), log, seq) for seq in range(start + 1, info.seq + 1))
            else:
                for e in self._read_events(log, start + 1, info.seq):
                    order.append(((int(e["ws_seq"]), 1, _epoch(e["at"]), log, int(e["seq"])), log, int(e["seq"])))
        order.sort()
        return order

    def _log_last_at(self, log: str) -> str:
        info = self._logs[log]
        return self._read_event(log, info.seq)["at"] if info.seq else "1970-01-01T00:00:00Z"

    def _plan_cuts(
        self,
        order: list[tuple[tuple[Any, ...], str, int]],
        base: Mapping[str, tuple[int, str, str]],
        cap: int,
        measure: Callable[[tuple[Segment, ...]], int],
    ) -> list[dict[str, tuple[int, str, str]]]:
        """Greedy: each step takes the longest run of the merged order whose step set ``measure`` keeps within
        ``cap`` (exponential then binary search; the size only grows with the run)."""
        prev = {log: c[0] for log, c in base.items()}
        cuts: list[dict[str, tuple[int, str, str]]] = []
        i = 0

        def size(upto: int) -> int:
            grow: dict[str, int] = {}
            for _k, log, seq in order[i:upto]:
                grow[log] = seq
            segs = []
            for log in sorted(grow, key=lambda lg: (lg != WORKSPACE, lg)):
                info, after = self._logs[log], prev.get(log, 0)
                segs.append(Segment(log, after, info.accs[after - 1] if after else _ZERO_ACC,
                                    tuple(info.heads[after : grow[log]])))  # fmt: skip
            return measure(tuple(segs))

        while i < len(order):
            if size(i + 1) > cap:
                raise StoreError("relay.too_large", "a single event's step set is above the cap")
            good, step = i + 1, 1
            while good < len(order):
                probe = min(len(order), i + 2 * step)
                if size(probe) > cap:
                    bad = probe
                    break
                good, step = probe, 2 * step
            else:
                bad = None
            if bad is not None:
                while bad - good > 1:
                    mid = (good + bad) // 2
                    good, bad = (mid, bad) if size(mid) <= cap else (good, mid)
            for _k, log, seq in order[i:good]:
                prev[log] = seq
            cuts.append({log: (seq, self._logs[log].heads[seq - 1], self._logs[log].accs[seq - 1])
                         for log, seq in prev.items() if seq})  # fmt: skip
            i = good
        return cuts

    # ------------------------------------------------------------------ forwarded revocation evidence (H12b)

    def note_forwarded_revocation(self, item: object) -> Literal["held", "pending", "dropped"]:
        """Hold one verified item of the relay's unpublished evidence (§11A.9, P3-D31; R4 calls it as a worker job).

        Every fact comes from the signed objects: ``pk_pub`` parses and names the person; the certificate verifies
        under it (``check_cert``) and is judged at its own ``created_ms`` (an expired historical certificate is
        still evidence); the revocation verifies under it against that certificate; device, person and reason are
        the certificate's and revocation's, and the item's outer ``device_id``, ``person_id`` and ``reason`` and
        ``provenance.digest`` (``sha256:`` over ``cj(revocation)``) must equal them. Then, under the store lock:
        ``held`` when the workspace associates the device with that person (the caller raises the owners' ``needs``
        item); ``pending`` when it does not but the person is a current or past member; ``dropped`` for anything
        else, for anything that does not verify and when ``HostPins.note_revocation`` (forwarded, capped) does not
        retain it. ``held`` and ``pending`` mean the record is durable. Idempotent per ``(device_id, person_id)``.
        The host never appends ``device.revoked`` or a restore for it."""
        facts = _verify_forwarded(item)
        if facts is None:
            return "dropped"
        device, person, reason, rev, cert, pk_pub = facts
        with self._locked():
            if self._pins is None or self._state is None:
                _log.info("forwarded revocation dropped (%s, %s): no host state", device, person)
                return "dropped"
            state = self._state
            if state.device_person(device) == person:
                result: Literal["held", "pending", "dropped"] = "held"
            elif state.is_member(person):
                result = "pending"
            else:
                _log.info("forwarded revocation dropped (%s, %s): not a member", device, person)
                return "dropped"
            owner = None
            if self._genesis_event is not None:
                with contextlib.suppress(KeyError, TypeError):
                    owner = "p_" + self._genesis_event["delegation"]["o"]["owner_person_id"]
            kept = self._pins.note_revocation(
                device, reason, rev, self._now(), cert, pk_pub, chain=False, is_member=state.is_member, owner=owner
            )
            if not kept:
                _log.info("forwarded revocation dropped (%s, %s): over the caps", device, person)
                return "dropped"
            return result


def _restore_evidence(transition: Mapping[str, Any]) -> dict[str, Any]:
    """A restore's outbox evidence (§11 P3-D21): the source C, the signed line, the certificate, the delegation and
    the proof material, taken from its pending transition."""
    out = {k: transition[k] for k in transition if k not in ("committed", "C", "T")}
    out["source"] = transition["C"]
    return out


def _verify_forwarded(item: object) -> tuple[str, str, str, dict[str, Any], dict[str, Any], str] | None:
    """``(d_device, p_person, reason, revocation, device_cert, pk_pub)`` derived from ``item``'s signed objects, or
    None (and a log line naming the class of failure, never key material) when anything does not verify (§11A.9)."""
    from orch.identity import certs
    from orch.identity.errors import Refused

    try:
        if not isinstance(item, dict) or set(item) != _ITEM_KEYS:
            raise ValueError("not an item of the stated shape")
        if len(canon.cj_checked(item)) > MAX_FORWARDED_ITEM_BYTES:
            raise ValueError("the item is above the size cap")
        if not isinstance(item["provenance"], dict) or set(item["provenance"]) != {"cursor", "digest"}:
            raise ValueError("provenance shape")
        schema.validate_def("deviceCert", item["device_cert"])
        schema.validate_def("revocation", item["revocation"])
        if len(item["device_cert"]["o"]["label_sealed"]) > _MAX_LABEL_SEALED:
            raise ValueError("label_sealed above its cap")
        pk_text = item["pk_pub"]
        pk = crypto.validate_public_key(crypto.unb64u(pk_text, crypto.PUB_LEN))
        person_hex = crypto.person_id(pk).hex()
        cert, rev = item["device_cert"], item["revocation"]
        cert_o = certs.check_cert(cert, pk)
        certs.verify_cert(cert, pk, now_ms=cert_o["created_ms"])  # valid at its own creation (§11A.9)
        rev_o = certs.verify_revocation(rev, pk, cert_o)
        device_hex, reason = cert_o["device_id"], rev_o["reason"]
        if rev_o["device_id"] != device_hex or rev_o["person_id"] != person_hex or cert_o["person_id"] != person_hex:
            raise ValueError("the signed objects disagree")
        if (item["device_id"], item["person_id"], item["reason"]) != (device_hex, person_hex, reason):
            raise ValueError("an outer field differs from the signed objects")
        provenance = item["provenance"]
        if not isinstance(provenance, dict) or provenance.get("digest") != canon.artifact_digest(canon.cj_checked(rev)):
            raise ValueError("provenance digest")
    except (Refused, ValueError, KeyError, TypeError, AttributeError, crypto.EncodingError, crypto.CryptoError,
            canon.HashError, schema.SchemaError) as e:  # fmt: skip
        _log.info("forwarded revocation dropped: %s", type(e).__name__)
        return None
    return "d_" + device_hex, "p_" + person_hex, reason, rev, cert, pk_text
