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
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
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
from .fsio import append_durable, fsync_dir, read_or_none, write_atomic
from .index import Index
from .lock import FileLock
from .logs import LogInfo, file_size, read_new_lines
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


@dataclass
class _Plan:
    """An external change the store found in one ticket's files and the host events that answer it."""

    events: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)  # (event, append kwargs)


def _epoch(stamp: str) -> int:
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))


def _fmt(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Store:
    """A workspace directory. Use :meth:`open`; ``append`` is the only way to add an event."""

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
    ) -> None:
        self.root = root
        self.workspace_id = workspace_id
        self._expected_genesis = expected_genesis
        self._host = host
        self._verifier = verifier
        self._clock = clock
        self._name = workspace_name
        self.state_dir = root / ".state"
        self._pins = HostPins(host_state_dir if host_state_dir is not None else self.state_dir, workspace_id)
        self._external_pins = host_state_dir is not None
        self._lock = FileLock(self.state_dir / "lock")
        self._checkpoints = Checkpoints(self.state_dir / "checkpoints", workspace_id)
        self._index = Index(self.state_dir / "index.sqlite")
        self.reports: list[Report] = []
        self._logs: dict[str, LogInfo] = {}
        self._state: State | None = None
        self._read_errors: dict[str, str] = {}
        self._diverged: dict[str, Divergence] = {}
        self._genesis_event: dict[str, Any] | None = None
        self._created_at: dict[str, str] = {}
        self._json_cache: dict[str, tuple[Any, bytes]] = {}
        self._applied_raw: bytes | None = None
        self._applied_n = 0
        self._last_at = 0
        self._last_pos: tuple[int, int, str, int] | None = None
        self._needs_reload = False
        self._torn: set[str] = set()
        self._ticket_appends_since_cp = 0
        self._healing = False

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
        verifier: Verifier | None = None,
        clock: Callable[[], float] = time.time,
        workspace_name: str | None = None,
    ) -> Store:
        """Open (and, for a new directory, prepare) the workspace at ``path``.

        ``expected_workspace_id`` and ``expected_genesis`` are the pins: the id from ``config.json`` and the genesis
        head from ``<host state dir>/hosts/<workspace_id>/genesis`` (§5.11). ``expected_genesis=None`` means "use the
        pin file in ``host_state_dir`` if there is one, otherwise trust the genesis the logs hold and pin it" (trust on
        first use). Replay uses both pins for every read. ``host`` is the workspace key signer; without it the store
        is read-only (``append`` raises ``store.read_only``). On the way in the store recovers a crashed write,
        checks the checkpoints and looks for external edits (see :meth:`scan`); what it did is in ``reports``.
        """
        root = Path(path)
        store = cls(
            root,
            workspace_id=expected_workspace_id,
            expected_genesis=expected_genesis,
            host=host,
            host_state_dir=Path(host_state_dir) if host_state_dir is not None else None,
            verifier=verifier or CryptoVerifier(),
            clock=clock,
            workspace_name=workspace_name,
        )
        store._open()
        return store

    def _open(self) -> None:
        for d in ("events", "tickets", ".state"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        cfg = read_or_none(self.root / "config.json")
        if cfg is not None:
            try:
                have = json.loads(cfg)["workspace"]["id"]
            except (ValueError, KeyError, TypeError):
                have = None
            if have is not None and have != self.workspace_id:
                raise StoreError("trust.genesis_mismatch", "config.json belongs to another workspace")
        with self._lock:
            self._recover_pending()
            self._load_all()
            if self._host is not None:
                self._after_open_heal()

    def close(self) -> None:
        self._index.close()

    # ------------------------------------------------------------------ small helpers

    def _now(self) -> str:
        return _fmt(int(self._clock()))

    @property
    def state(self) -> State:
        """The derived state now (claims, leases and grants judged by the clock)."""
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
        """Every broken log as ``(log, seq, code, detail)``: unreadable lines, and what the model reports."""
        out = [(log, self._logs[log].error_seq, "chain.broken", msg) for log, msg in sorted(self._read_errors.items())]
        out += [(c.log, c.seq, c.code, c.detail) for c in self.state.chain_errors if c.log not in self._read_errors]
        return out

    def head_seq(self, log: str) -> int:
        """The seq of the last event of ``log`` (0 for an unknown log): the ticket head in the dedup key."""
        info = self._logs.get(log)
        return info.seq if info else 0

    def log_head(self, log: str) -> str | None:
        info = self._logs.get(log)
        return info.head if info else None

    def log_path(self, log: str) -> Path:
        return self._path_of(log)

    def _path_of(self, log: str) -> Path:
        if log == WORKSPACE:
            return self.root / "events" / "workspace.jsonl"
        if not _ULID.fullmatch(log):
            raise StoreError("validation.log", f"{log!r} is not a ticket uid")
        return self.root / "tickets" / log / "events.jsonl"

    def _report(self, code: str, detail: str, log: str | None = None) -> None:
        r = Report(code, detail, log)
        if r not in self.reports:
            self.reports.append(r)

    # ------------------------------------------------------------------ loading

    def _load_all(self) -> None:
        """Read every log from disk, replay, and rebuild every cache. The pins are enforced here."""
        pin = self._pins.genesis() if self._external_pins else None
        if self._expected_genesis is not None and pin is not None and pin != self._expected_genesis:
            raise StoreError("trust.genesis_mismatch", "the genesis pin file differs from the genesis given")
        self._pin = self._expected_genesis or pin
        self._logs = {WORKSPACE: LogInfo(WORKSPACE, self._path_of(WORKSPACE))}
        self._read_errors = {}
        ws_events = read_new_lines(self._logs[WORKSPACE])
        tickets: dict[str, list[dict[str, Any]]] = {}
        tdir = self.root / "tickets"
        for p in sorted(tdir.iterdir()) if tdir.is_dir() else []:
            if not _ULID.fullmatch(p.name):
                continue
            info = LogInfo(p.name, p / "events.jsonl")
            if not info.path.exists():
                continue  # an empty ticket directory: the event never made it (recovery removes it when empty)
            tickets[p.name] = read_new_lines(info)
            self._logs[p.name] = info
        for name, info in self._logs.items():
            if info.error:
                self._read_errors[name] = info.error
        self._genesis_event = ws_events[0] if ws_events and ws_events[0]["type"] == "workspace.created" else None
        self._created_at = {u: evs[0]["at"] for u, evs in tickets.items() if evs and evs[0]["type"] == "ticket.created"}
        self._state = replay(
            ws_events,
            tickets,
            verifier=self._verifier,
            now=self._now(),
            expected_workspace_id=self.workspace_id,
            expected_genesis=self._pin,
        )
        ws = self._state.workspace
        if ws_events and ws.genesis is None:
            detail = next((c.detail for c in self._state.chain_errors if c.log == WORKSPACE), "untrusted genesis")
            raise StoreError("trust.genesis_mismatch", detail)
        if ws.genesis is not None and self._pin is None and self._external_pins:
            self._pins.pin_genesis(ws.genesis)
        if ws.genesis is not None:
            self._pin = self._pin or ws.genesis
        last_at, last_pos = 0, None
        for name, evs in [(WORKSPACE, ws_events), *tickets.items()]:
            for e in evs:
                last_at = max(last_at, _epoch(e["at"]))
                if name != WORKSPACE:
                    pos = (e["ws_seq"], _epoch(e["at"]), name, e["seq"])
                    last_pos = pos if last_pos is None or pos > last_pos else last_pos
        self._last_at, self._last_pos = last_at, last_pos
        self._json_cache = {}
        raw = read_or_none(self.state_dir / "applied")
        self._applied_raw = raw
        try:
            self._applied_n = int(json.loads(raw)["n"]) if raw else 0
        except (ValueError, KeyError, TypeError):
            self._applied_n = 0
        self._needs_reload = False
        self._diverged = {}
        wsk = self._wsk_pub()
        if wsk is not None:
            for d in find_divergence(self._checkpoints, wsk, ws.genesis, self._logs):
                if d.code == "trust.genesis_mismatch":
                    raise StoreError(d.code, d.detail)
                self._diverged[d.log] = d
        self._ensure_index()

    def _wsk_pub(self) -> bytes | None:
        if self._genesis_event is None:
            return None
        return crypto.unb64u(self._genesis_event["wsk_pub"], crypto.PUB_LEN)

    def _ensure_index(self) -> None:
        assert self._state is not None
        ws = self._state.workspace
        if ws.genesis is None:
            return
        if not self._index.is_current(self._logs, self.workspace_id, ws.genesis):
            self._index.rebuild(self._state, self._logs, self.workspace_id, ws.genesis)

    def rebuild_index(self) -> None:
        """Drop and rebuild ``.state/index.sqlite`` from the logs (it is derived data)."""
        with self._lock:
            assert self._state is not None
            if self._state.workspace.genesis is not None:
                self._index.rebuild(self._state, self._logs, self.workspace_id, self._state.workspace.genesis)

    @property
    def index(self) -> Index:
        return self._index

    # ------------------------------------------------------------------ locking, sync, recovery

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            self._recover_pending()
            raw = read_or_none(self.state_dir / "applied")
            if self._needs_reload or raw != self._applied_raw:
                self._load_all()
            yield

    def _recover_pending(self) -> None:
        pdir = self.state_dir / "pending"
        if not pdir.is_dir():
            return
        for d in sorted(pdir.iterdir()):
            self._recover_one(d)
        with contextlib.suppress(OSError):
            pdir.rmdir()
        tdir = self.root / "tickets"
        for p in tdir.iterdir() if tdir.is_dir() else []:  # a ticket directory whose event never landed
            with contextlib.suppress(OSError):
                if p.is_dir() and not (p / "events.jsonl").exists() and not any(p.iterdir()):
                    p.rmdir()

    def _recover_one(self, d: Path) -> None:
        try:
            m = json.loads((d / "manifest.json").read_bytes())
            line = m["line"].encode("utf-8")
            log = m["log"]
            path = self._path_of(log)
        except (OSError, ValueError, KeyError, StoreError, TypeError):
            shutil.rmtree(d, ignore_errors=True)  # the manifest is written last: the event was never appended
            return
        raw = read_or_none(path) or b""
        if raw.endswith(line):
            self._needs_reload = True
            torn = self._install(m, d)
            for rel in torn:
                self._torn.add(rel)
                self._report("store.torn_write", f"{rel} did not survive the crash of event {m['id']}", log)
        else:
            for k in range(min(len(line) - 1, len(raw)), 0, -1):  # a torn prefix of our own line: cut it off
                if raw[-k:] == line[:k] and (len(raw) == k or raw[-k - 1] == 0x0A):
                    with open(path, "r+b") as f:
                        f.truncate(len(raw) - k)
                        os.fsync(f.fileno())
                    self._needs_reload = True
                    break
        shutil.rmtree(d, ignore_errors=True)

    def _install(self, m: dict[str, Any], d: Path) -> list[str]:
        """Step 4 of the write order (idempotent). Returns the relative paths that could not be installed."""
        torn: list[str] = []
        for f in m["files"]:
            dest, src = self.root / f["to"], d / str(f["n"])
            data = read_or_none(src)
            if data is not None and _sha(data) == f["sha256"]:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dest)
                fsync_dir(dest.parent)
            else:
                now = read_or_none(dest)
                if now is None or _sha(now) != f["sha256"]:
                    torn.append(f["to"])
        uid = m.get("body_copy")
        if uid:
            body = read_or_none(self.root / "tickets" / uid / "body.md")
            if body is not None and f"tickets/{uid}/body.md" not in torn:
                write_atomic(self.state_dir / "body" / f"{uid}.md", body)
        self._write_applied(m["id"], m["log"], m["seq"])
        return torn

    def _write_applied(self, event_id: str, log: str, seq: int) -> None:
        self._applied_n += 1
        raw = canon.cj_checked({"id": event_id, "log": log, "n": self._applied_n, "seq": seq}) + b"\n"
        write_atomic(self.state_dir / "applied", raw)
        self._applied_raw = raw

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
        return read_or_none(self.root / "tickets" / uid / "ticket.json")

    def body_sections(self, uid: str) -> dict[str, str]:
        """The section texts of the ticket's ``body.md`` as on disk. Raises ``StoreError`` if the file is not a body the
        log accounts for (call :meth:`scan` first to have such files answered with host events)."""
        assert self._state is not None
        raw = read_or_none(self.root / "tickets" / uid / "body.md")
        if raw is None:
            return {}
        try:
            return render.parse_body(raw, self._state.tickets[uid].type)
        except render.BodyError as e:
            raise StoreError("validation.body", str(e)) from None

    def next_key(self) -> str:
        """The next ticket key: one above the highest number in ``keys.jsonl`` or in any ``ticket.created`` (§2)."""
        assert self._state is not None
        best = 0
        for t in self._state.tickets.values():
            m = _KEY_NUM.fullmatch(t.key)
            best = max(best, int(m.group(1)) if m else 0)
        for line in (read_or_none(self.root / "keys.jsonl") or b"").splitlines():
            with contextlib.suppress(ValueError, KeyError, TypeError):
                m = _KEY_NUM.fullmatch(json.loads(line)["key"])
                best = max(best, int(m.group(1)) if m else 0)
        return f"{self._state.workspace.prefix}-{best + 1:04d}"

    def normalise_ref(self, ref: str) -> str:
        """``43``, ``#43``, ``demo-43`` and ``DEMO-0043`` are the ticket key ``DEMO-0043``; a uid is its ticket's key.
        Anything else comes back unchanged."""
        assert self._state is not None
        r = ref.strip().lstrip("#")
        if _ULID.fullmatch(r) and r in self._state.tickets:
            return self._state.tickets[r].key
        prefix = self._state.workspace.prefix
        m = re.fullmatch(r"(?:(?i:" + re.escape(prefix) + r")-)?([0-9]+)", r) if prefix else None
        if m and int(m.group(1)) >= 1:
            return f"{prefix}-{int(m.group(1)):04d}"
        return ref

    def uid_of(self, ref: str) -> str | None:
        assert self._state is not None
        r = self.normalise_ref(ref)
        t = next((t for t in self._state.tickets.values() if t.key == r), None)
        return t.uid if t else None

    # ------------------------------------------------------------------ the one write path

    def append(
        self,
        event: Mapping[str, Any],
        *,
        log: str,
        body: Mapping[str, str | None] | None = None,
        artifacts: Mapping[str, bytes] | None = None,
        idem: str | None = None,
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
            new_uid = uid or new_ulid()
            ev = {
                "type": "ticket.created",
                "actor": dict(actor),
                "key": self.next_key(),
                "ticket_type": ticket_type,
                "title": title,
                "owner": owner,
            }
            return self._append(ev, new_uid, None, None, idem)

    # -- steps

    def _check_writable(self, log: str, typ: str) -> None:
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
        if idem is not None:
            dup = self._idem_hit(idem, log, e)
            if dup is not None:
                return dup
        actor = e.get("actor")
        host_event = isinstance(actor, dict) and actor.get("kind") == "host"
        healed = [] if self._healing else self._heal_for(log, typ, host_event)
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
        ev: dict[str, Any] = {}
        genesis = e["type"] == "workspace.created"
        if genesis and self._host.public_key != crypto.unb64u(e.get("wsk_pub", ""), crypto.PUB_LEN):
            raise StoreError("validation.host_key", "the genesis names a workspace key other than this host's")
        ev: dict[str, Any] = {}
        for attempt in (0, 1):
            ev = dict(e)
            self._stamp(ev, log, bump=attempt)
            try:
                schema.validate("event", {**ev, "host_sig": _PLACEHOLDER_SIG}, log=kind)
            except schema.SchemaError as err:
                raise StoreError("validation.event", str(err)) from None
            if genesis:  # the genesis checks its own host_sig (§5.11 step 4), so it is signed before it is admitted
                self._host_sign(ev, log)
            r = admit(old_state, ev, log=log)
            if isinstance(r, Refusal):
                if r.code == Code.CHAIN_BAD_WS_SEQ and attempt == 0:
                    continue  # the store's own ordering: stamp one second later, once
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
        if (info.offset if info else 0) != file_size(path):
            self._needs_reload = True
            raise StoreError("chain.broken", f"{log}: the log changed on disk while the store held the lock")
        try:
            manifest = self._write_pending(ev, log, line, writes)
            append_durable(path, line)  # the commit
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

    def _stamp(self, ev: dict[str, Any], log: str, *, bump: int) -> None:
        info = self._logs.get(log)
        seq = (info.seq if info else 0) + 1
        ev["seq"] = seq
        ev["prev"] = info.head if info else None
        t = max(int(self._clock()), self._last_at) + bump
        if log != WORKSPACE:
            wsh = self._logs[WORKSPACE].seq if WORKSPACE in self._logs else 0
            ev["ws_seq"] = wsh
            lp = self._last_pos
            while lp is not None and (wsh, t, log, seq) <= lp:  # minimal bump: the merged order must grow
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
        cur = self.body_sections(uid) if (self.root / "tickets" / uid / "body.md").exists() else {}
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
        data = (artifacts or {}).get(e.get("name", ""))
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
                out.append(_Write("keys.jsonl", self._keys_bytes(new)))
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
            out.append(_Write("keys.jsonl", self._keys_bytes(new, extra=(view.key, uid, ev["at"]))))
        return out

    def _ticket_for(self, view: Any, uid: str) -> bytes:
        data = self._ticket_bytes(uid, view)
        try:
            schema.validate("ticket", json.loads(data))
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
                doc = json.loads(cur)
                schema.validate("workspace", doc)
                name, run_for = doc["workspace"]["name"], doc["agents"]["run_for"]
        return render.render_config(
            state.workspace, host_id=g["host_id"], wsk_pub=g["wsk_pub"], name=name, run_for=run_for
        )

    def _keys_bytes(self, state: State, extra: tuple[str, str, str] | None = None) -> bytes:
        rows = {t.uid: (t.key, self._created_at.get(t.uid, "")) for t in state.tickets.values()}
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
        info.offset += len(line)
        info.seq = ev["seq"]
        info.heads.append(canon.event_head(ev))

    # -- after the commit

    def _after_commit(self, old: State, new: State, ev: dict[str, Any], log: str) -> None:
        assert self._host is not None
        self._last_at = max(self._last_at, _epoch(ev["at"]))
        if log != WORKSPACE:
            pos = (ev["ws_seq"], _epoch(ev["at"]), log, ev["seq"])
            self._last_pos = pos if self._last_pos is None or pos > self._last_pos else self._last_pos
            if ev["type"] == "ticket.created":
                self._created_at[log] = ev["at"]
        typ = ev["type"]
        if typ == "workspace.created":
            self._genesis_event = ev
            self._pin = new.workspace.genesis
            if self._external_pins and new.workspace.genesis:
                self._pins.pin_genesis(new.workspace.genesis)
        if typ == "device.revoked":
            self._pins.note_revocation(ev["device"], ev["reason"], ev["revocation"])
        if typ == "restore":
            self._checkpoints.abandon_above(log, ev["from_seq"])
            self._diverged.pop(log, None)
        self._write_checkpoints(log, ev, new)
        changed = {u for u, v in new.tickets.items() if old.tickets.get(u) is not v}
        if new.workspace.genesis is not None:
            try:
                if not self._index.path.exists():
                    self._index.rebuild(new, self._logs, self.workspace_id, new.workspace.genesis)
                else:
                    self._index.update(new, self._logs, [log], changed)
            except Exception:  # noqa: BLE001 - derived data: never fail an append for it
                self._index.close()
        if typ == "restore" and log == WORKSPACE:
            self._reappend_revocations()

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

    def _idem_hit(self, idem: str, log: str, e: dict[str, Any]) -> Appended | None:
        """A retry of an append that already happened returns its result; one whose first try never reached the log
        reuses the event id it recorded (so a retry can never append twice)."""
        raw = read_or_none(self._intent_path(idem))
        try:
            rec = json.loads(raw) if raw else None
        except ValueError:
            rec = None
        if not isinstance(rec, dict) or not isinstance(rec.get("log"), str) or not isinstance(rec.get("id"), str):
            return None
        info = self._logs.get(rec["log"])
        seq = rec.get("seq")
        if not (
            isinstance(seq, int)
            and info is not None
            and 1 <= seq <= info.seq
            and info.heads[seq - 1] == rec.get("head")
        ):
            seq = self._seq_of_id(rec["log"], rec["id"]) if info is not None else None
        if seq is not None:
            return Appended(self._read_event(rec["log"], seq), self.state, True)
        if (e.get("actor") or {}).get("kind") == "person" and e.get("id") not in (None, rec["id"]):
            raise StoreError("validation.idem", "a retried signed event keeps the id of its first attempt")
        e["id"] = rec["id"]
        return None

    def _seq_of_id(self, log: str, event_id: str) -> int | None:
        raw = read_or_none(self._path_of(log)) or b""
        needle = b'"id":"' + event_id.encode() + b'"'
        for i, line in enumerate(raw.splitlines(), 1):
            if needle in line:
                with contextlib.suppress(canon.HashError, KeyError):
                    if canon.parse_event_line(line + b"\n")["id"] == event_id:
                        return i
        return None

    def _read_event(self, log: str, seq: int) -> dict[str, Any]:
        lines = (read_or_none(self._path_of(log)) or b"").splitlines()
        return canon.parse_event_line(lines[seq - 1] + b"\n")

    def _idem_intent(self, idem: str, log: str, event_id: str) -> None:
        write_atomic(self._intent_path(idem), json.dumps({"log": log, "id": event_id}).encode("utf-8"), mode=0o600)

    def _idem_done(self, idem: str, log: str, ev: dict[str, Any]) -> None:
        rec = {"log": log, "id": ev["id"], "seq": ev["seq"], "head": canon.event_head(ev)}
        write_atomic(self._intent_path(idem), json.dumps(rec).encode("utf-8"), mode=0o600, durable=False)

    # ------------------------------------------------------------------ external edits (§5.8)

    def scan(self) -> list[dict[str, Any]]:
        """Look at every ticket's files and ``config.json``/``keys.jsonl``; answer each external change with the host
        events §5.8 names (``edit.external`` for ``body.md``; revert and ``projection.repaired`` for the rest).
        Returns the events appended. A read-only store reports instead (``reports``)."""
        with self._locked():
            return self._scan_all()

    def _scan_all(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        assert self._state is not None
        if self._host is None:
            return out
        out += self._heal_workspace()
        for uid in sorted(self._state.tickets):
            out += self._heal_ticket(uid)
        return out

    def _after_open_heal(self) -> None:
        assert self._state is not None
        if self._state.workspace.genesis is None or self._read_errors or self._diverged:
            return
        self._scan_all()
        self._checkpoint_all()

    def _heal_for(self, log: str, typ: str, host_event: bool) -> list[dict[str, Any]]:
        if typ in ("workspace.created", "ticket.created"):
            return []
        if log == WORKSPACE:
            return [] if host_event else self._heal_workspace()
        return [] if host_event and typ in _TICKET_HOST_EVENTS else self._heal_ticket(log)

    def _host_append(self, typ: str, log: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
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
            ("keys.jsonl", "keys_mismatch", lambda: self._keys_bytes(self._state)),
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
            doc = json.loads(have)
            want = json.loads(self._ticket_bytes(uid))
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
                self._report("store.torn_write", f"{bpath} is not a body and no valid copy to restore", uid)
                return []
            self._torn.discard(bpath)
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
            self._report(
                "store.torn_write", f"bound sections {bound} of {bpath} changed and no valid copy to revert", uid
            )
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

    def _is_bound(self, uid: str, sid: str, entry: dict[str, Any] | None) -> bool:
        assert self._state is not None
        r = external_edit_voids(self._state, uid, {sid: entry})
        return isinstance(r, Refusal) and r.code == Code.TICKET_FROZEN

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
            path = self._path_of(log)
            raw = read_or_none(path) or b""
            lines = raw.splitlines(keepends=True)
            if from_seq >= len(lines):
                return 0
            keep, cut = b"".join(lines[:from_seq]), b"".join(lines[from_seq:])
            ab = self.state_dir / "abandoned"
            ab.mkdir(parents=True, exist_ok=True)
            write_atomic(ab / f"{log}-{from_seq + 1}.jsonl", cut, mode=0o600)
            write_atomic(path, keep, mode=0o644)
            self._needs_reload = True
            self._load_all()
            return len(lines) - from_seq

    def _reappend_revocations(self) -> None:
        """§5.10: after a workspace ``restore`` the host puts back every person-key-signed revocation it has seen, as
        ``device.revoked`` with actor H. A device the restored chain does not know has nothing to revoke."""
        assert self._state is not None
        for rec in self._pins.revocations():
            dev = self._state.workspace.devices.get(rec["device"])
            if dev is None or dev.revoked:
                continue
            self._host_append(
                "device.revoked",
                WORKSPACE,
                {"device": rec["device"], "reason": rec["reason"], "revocation": rec["revocation"]},
            )
