"""The subscription journal of one workspace (§0.9, D-P2-12, F11): every event in merged order, numbered by ``jpos``.

**Positions.** ``jpos`` is an event's 1-based position in the merged order of all logs (F1 §5.5) since the genesis. The
store only accepts an append that sorts after every earlier one, so the merged order is the append order and a new
event always takes the next ``jpos``. The journal is in memory and rebuilt from the logs at start
(:meth:`Journal.build`), so ``jpos`` is deterministic and survives a restart. Only the last :data:`WINDOW` entries are
kept; a cursor before them answers ``sub.reset``.

**What an entry holds.** The `Item` fields of the event (no payload), plus what delivery needs to filter it without the
store: the ticket's visibility just before and just after the event, whether the event changed ``needs`` and, for
``grant.issued``/``grant.revoked``, the grant's person. These come from the journal's own replay of the model (the same
:func:`orch.model.engine.apply_event` the store replays with, over the same events in the same order), so a rebuild
yields exactly what the live path yielded.

* **Visibility** is the ticket's ``visibility`` field (``"workspace"`` or the restricted persons).
* **needs.changed** is set when the ``needs`` of a ticket the event touched (every ticket for a workspace event that
  can change them) or of the workspace changed, and when a ticket with ``needs`` changed its visibility (who may see
  those needs changed). It fires for invisible tickets too: that is the accepted metadata of F13.

**Threads.** The journal belongs to the workspace worker thread. The store calls :meth:`committed` (``on_commit``) and
:meth:`check` (``before_append``) under its lock; :meth:`take` hands the entries not yet published to the event loop's
bus (:mod:`orch.host.bus`) as one immutable :class:`Batch`.

**Foreign appends (F11).** Someone else appending (tier A: an older CLI or a script holding the file-tier WSK) is
found three ways. Before every append and after every op, :meth:`check` compares the store's own count of logs it found
changed on disk (``Store.outside_changes``, kept by the freshness check every lock already does) and the ``tickets/``
directory's mtime (a new ticket log) with what the journal saw: O(1) per append. The 60 s check and ``sub.open`` run
:meth:`check` with ``full``: the signature (size, inode, times) of every log against what the journal read or saw
committed. And :meth:`committed` compares the size of a log on disk with the size the store wrote, so an append that
lands between the store's write and the hook is not counted as ours. The journal then marks itself stale; the worker
rebuilds it after the current op (:meth:`orch.host.workspace.WorkspaceService.sync`), which ends every subscription
with ``reset``. A committed ``restore`` rewrites history, so it rebuilds the journal in the same way (a new epoch).

A foreign append to a **ticket** log changes no authentication snapshot, so until one of these checks runs (at most
60 s, or the next append) the bus may still deliver that ticket's pending items with its old visibility. This is tier A
only and within F11's threat model.

**Cursors** are authenticated with a host key (:class:`Cursors`, kept in the host's state directory so cursors survive a
restart) and bound to the entry they point at (a short hash of its log, ``seq`` and event id). A cursor the host did not
issue is ``sub.reset``, and so is one whose entry is no longer at its ``jpos`` after a rebuild (a foreign writer whose
event sorts before existing positions shifts them). The principal binding inside a cursor is a scope check, not an
access check: every item is filtered for the viewer at delivery whatever the cursor says. Page tokens must not reuse
this encoding.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import itertools
import json
import os
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .readmodel import SummaryState, activity_item, data_of, summary

__all__ = [
    "WINDOW",
    "Batch",
    "Entry",
    "grant_person_of",
    "Journal",
    "Vis",
    "Cursors",
    "Window",
    "cursor_key",
    "entry_ref",
    "peek_cursor",
    "principal_hash",
    "sees",
]

#: entries kept in memory (§0.9: a cursor older than these answers ``sub.reset``)
WINDOW = 10_000
#: workspace events that can change a ticket's ``needs`` (addressees, eligible persons, policies, addon rules): only
#: these recompute every ticket's needs (an allow-list: O(tickets) for the rare ones, O(1) for the rest)
NEEDS_WORKSPACE_TYPES = frozenset(
    {
        "member.added",
        "member.removed",
        "role.changed",
        "device.revoked",
        "policy.changed",
        "settings.changed",
        "addon.granted",
        "addon.disabled",
        "addon.purged",
        "restore",
    }
)
_ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
CURSOR_PREFIX = "c1."

#: a ticket's visibility: ``"workspace"`` or the frozen set of persons on its restricted list
Vis = str | frozenset


def vis_of(value: Any) -> Vis:
    if value == "workspace":
        return "workspace"
    if isinstance(value, Mapping):
        return frozenset(p for p in value.get("restricted", ()) if isinstance(p, str))
    return frozenset()


def sees(person: str | None, vis: Vis | None) -> bool:
    """The F1 §10.7 test (:func:`orch.ops.runtime.visible_to`) on a journal visibility; ``None``: no such ticket."""
    if vis is None:
        return False
    if vis == "workspace":
        return True
    return person is not None and isinstance(vis, frozenset) and person in vis


@dataclass(frozen=True)
class Entry:
    jpos: int
    log: str  # a ticket uid or "workspace"
    key: str | None
    seq: int
    ws_seq: int
    type: str
    at: str
    actor_kind: str
    actor_id: str | None
    #: the ticket's visibility before and after the event (``None``: the ticket did not exist / is not a ticket)
    before: Vis | None = None
    after: Vis | None = None
    #: the event changed ``needs`` (see the module docstring)
    needs: bool = False
    #: ``grant.issued`` / ``grant.revoked``: the person of the grant
    grant_person: str | None = None
    #: a short hash of (log, seq, event id): binds a cursor to the event at its jpos
    ref: str = ""
    #: H6b ``sub.open {detail}`` (§0.9): the viewer-independent ``ActivityItem`` of the event (``actor`` in full,
    #: ``summary``, ``data``) with ``title`` ``None``; the bus sets the ticket's title at delivery
    detail: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)

    @property
    def uid(self) -> str | None:
        return None if self.log == "workspace" else self.log

    def item(self) -> dict[str, Any]:
        """The §0.9 event ``Item`` (v1.4 shape)."""
        return {
            "id": f"{self.jpos}.e",
            "jpos": self.jpos,
            "log": self.log,
            "key": self.key,
            "uid": self.uid,
            "seq": self.seq,
            "ws_seq": self.ws_seq,
            "type": self.type,
            "at": self.at,
            "actor": {"kind": self.actor_kind, "id": self.actor_id},
        }


@dataclass(frozen=True)
class Batch:
    """What the worker hands the bus: new entries, the current visibility of the tickets they touched, the head.
    ``reset``: the journal was rebuilt; ``entries`` is the whole window and ``vis`` every ticket."""

    workspace_id: str
    epoch: str
    head: int
    entries: tuple[Entry, ...]
    vis: Mapping[str, Vis]
    reset: bool = False
    #: the current title of each ticket created or retitled by these entries (every ticket on a reset)
    titles: Mapping[str, str] = field(default_factory=dict)


# --- cursors ----------------------------------------------------------------------------------------------------------


def principal_hash(principal: str) -> str:
    return hashlib.sha256(b"orch/v2/sub-principal|" + principal.encode()).hexdigest()[:16]


def entry_ref(log: str, seq: int, event_id: Any) -> str:
    return hashlib.sha256(f"orch/v2/sub-entry|{log}|{seq}|{event_id}".encode()).hexdigest()[:12]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def peek_cursor(cursor: Any) -> dict[str, Any] | None:
    """The fields of a cursor's body, **unauthenticated** (tests and diagnostics; :meth:`Cursors.verify` checks)."""
    if not isinstance(cursor, str) or len(cursor) > 256 or not cursor.startswith(CURSOR_PREFIX):
        return None
    body, _, _mac = cursor[len(CURSOR_PREFIX) :].partition(".")
    try:
        out = json.loads(_unb64(body))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(out, dict) or set(out) != {"e", "j", "p", "r", "w"}:
        return None
    if not isinstance(out["j"], int) or isinstance(out["j"], bool) or out["j"] < 0:
        return None
    if not all(isinstance(out[k], str) for k in ("e", "p", "r", "w")):
        return None
    return out


class Cursors:
    """Opaque cursors (§0.9, at most 256 bytes): ``c1.<body>.<mac>``, the body ``{workspace, principal hash, epoch,
    jpos, entry ref}``, the MAC an HMAC-SHA256 under the host's cursor key (128 bits kept)."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("a cursor key has at least 32 bytes")
        self._key = key

    def _mac(self, body: str) -> str:
        return _b64(hmac.new(self._key, b"orch/v2/sub-cursor|" + body.encode(), hashlib.sha256).digest()[:16])

    def encode(self, workspace_id: str, principal: str, epoch: str, jpos: int, ref: str) -> str:
        body = _b64(
            json.dumps(
                {"e": epoch, "j": int(jpos), "p": principal_hash(principal), "r": ref, "w": workspace_id},
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
        return f"{CURSOR_PREFIX}{body}.{self._mac(body)}"

    def verify(self, cursor: Any) -> dict[str, Any] | None:
        """The body of a cursor this host issued, or ``None``."""
        out = peek_cursor(cursor)
        if out is None:
            return None
        body, _, mac = cursor[len(CURSOR_PREFIX) :].partition(".")
        given = mac.encode("utf-8", "surrogatepass")  # bytes: a non-ASCII MAC is a mismatch, not an error
        return out if hmac.compare_digest(given, self._mac(body).encode("ascii")) else None


def cursor_key(directory: Path) -> bytes:
    """The host's cursor key: 32 random bytes in ``<state>/host/sub-cursor.key`` (0600), made once. Kept across
    restarts, so a client resumes with the cursor it stored; deleting it only makes every cursor ``sub.reset``."""
    path = Path(directory) / "sub-cursor.key"
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        pass
    else:
        try:
            os.write(fd, os.urandom(32))
            os.fsync(fd)
        finally:
            os.close(fd)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError(f"{path} must be the host's own and private (0600)")
        key = os.read(fd, 64)
    finally:
        os.close(fd)
    if len(key) != 32:
        raise ValueError(f"{path} is not a cursor key")
    return key


class Window:
    """The last :data:`WINDOW` entries and the head; ``jpos`` is contiguous in the window."""

    def __init__(self) -> None:
        self.window: deque[Entry] = deque(maxlen=WINDOW)
        self.head = 0

    @property
    def floor(self) -> int:
        """The oldest position a cursor may name: its entry must be in the window (to check its ref)."""
        return self.window[0].jpos if self.window else self.head

    def entry_at(self, jpos: int) -> Entry | None:
        if not self.window or not self.window[0].jpos <= jpos <= self.window[-1].jpos:
            return None
        return self.window[jpos - self.window[0].jpos]

    def ref_at(self, jpos: int) -> str:
        entry = self.entry_at(jpos)
        return entry.ref if entry is not None else ""

    def after(self, jpos: int) -> list[Entry]:
        """The entries after ``jpos``, in order."""
        if not self.window or jpos >= self.head:
            return []
        start = max(0, jpos - self.window[0].jpos + 1)
        return list(itertools.islice(self.window, start, None))


# --- the journal ------------------------------------------------------------------------------------------------------


def _sig(path: Path) -> tuple[int, int, int, int] | None:
    from orch.store.logs import stat_sig

    return stat_sig(path)


@dataclass
class _Walk:
    """The journal's own replay of the model: the core, each ticket's visibility and needs, the workspace needs."""

    core: Any
    ctx: Any
    vis: dict[str, Vis] = field(default_factory=dict)
    needs: dict[str, tuple] = field(default_factory=dict)
    ws_needs: tuple = ()
    #: the criteria and tasks of each ticket before the event being summarised (``ticket.updated`` summaries)
    summaries: Any = field(default_factory=SummaryState)
    #: each ticket's current title
    titles: dict[str, str] = field(default_factory=dict)
    #: H6b ``needs`` ``since``: per ticket, per need ``(kind, ref)``, the ``at`` of the event whose append made it hold
    #: (counted from the latest time it began to hold without a break)
    since: dict[str, dict[tuple[str, str | None], str]] = field(default_factory=dict)
    #: H6b ``ticket.query`` ``updated_at``/``head_seq``: per ticket, ``(at, seq)`` of its last event
    last: dict[str, tuple[str, int]] = field(default_factory=dict)

    def track(self, uid: str, needs: tuple, at: str) -> None:
        old = self.since.get(uid, {})
        new = {(n.kind, n.ref): old.get((n.kind, n.ref), at) for n in needs}
        if new:
            self.since[uid] = new
        else:
            self.since.pop(uid, None)


def grant_person_of(typ: str, e: Mapping[str, Any], grants: Any) -> str | None:
    """The person of the grant a ``grant.*`` event names: a ``grant.issued``'s signer, a ``grant.revoked``'s grant
    (looked up in ``grants``). The one derivation used by the journal's ``Entry`` and by ``activity.list``."""
    if typ in ("grant.issued", "grant.requested"):
        a = e.get("actor") if isinstance(e.get("actor"), Mapping) else {}
        if a.get("kind") == "host":  # C10: a host-actor grant.issued answers a request; the grant carries the person
            g = grants.get(e.get("grant"))
            return getattr(g, "person", None) if g is not None else None
        return a.get("id") if isinstance(a.get("id"), str) else None
    if typ == "grant.revoked":
        g = grants.get(e.get("grant"))
        return getattr(g, "person", None) if g is not None else None
    return None


class Journal(Window):
    def __init__(self, workspace_id: str, root: str | os.PathLike[str], cursors: Cursors) -> None:
        super().__init__()
        self.workspace_id = workspace_id
        self.root = Path(root)
        self.cursors = cursors
        self.epoch = ""
        #: entries committed since the last :meth:`take`
        self.unpublished: list[Entry] = []
        #: tickets whose visibility changed since the last :meth:`take`
        self.touched: set[str] = set()
        #: tickets created or retitled since the last :meth:`take`
        self.retitled: set[str] = set()
        #: the journal no longer matches the logs (a foreign append or a restore): rebuild before using it
        self.stale = False
        #: why it went stale (for the audit line)
        self.cause = ""
        self.sigs: dict[str, tuple[int, int, int, int] | None] = {}
        self._store: Any = None
        self._seen_changes = 0
        self._tickets_mtime: int | None = None
        self._walk: _Walk | None = None
        #: bumped by every :meth:`build` (start, foreign append, restore): a cache of positions keys on it, because a
        #: foreign writer can shift positions without changing the epoch
        self.generation = 0
        self._genesis = ""
        self._restore = ""

    # -- positions
    def cursor(self, principal: str, jpos: int | None = None) -> str:
        jpos = self.head if jpos is None else jpos
        return self.cursors.encode(self.workspace_id, principal, self.epoch, jpos, self.ref_at(jpos))

    def position(self, cursor: Any, principal: str) -> int | None:
        """The ``jpos`` a client cursor stands for, or ``None`` (``sub.reset``): not a cursor this host issued, another
        workspace, principal or epoch, outside the window, or no longer pointing at the same event."""
        body = self.cursors.verify(cursor)
        if body is None or body["w"] != self.workspace_id or body["p"] != principal_hash(principal):
            return None
        if body["e"] != self.epoch or not self.floor <= body["j"] <= self.head:
            return None
        if body["r"] != self.ref_at(body["j"]):
            return None
        return int(body["j"])

    # -- building (worker thread)
    def build(self, store: Any) -> Batch:
        """Rebuild from the logs (start, a foreign append, a restore). Returns the whole window as a reset batch."""
        from orch.model.types import Core

        self.generation += 1

        with store.locked():
            events = store.merged_events()
            self.sigs = self._scan()
            self._tickets_mtime = self._tickets_stamp()
            self._seen_changes = store.outside_changes
            ctx = store.replay_context()  # the store's own: verifier and both pins
        self._store = store
        self._walk = _Walk(Core(), ctx)
        self.window = deque(maxlen=WINDOW)
        self.head, self.unpublished, self.touched, self.retitled = 0, [], set(), set()
        self._genesis = self._restore = ""
        for log, event in events:
            self.window.append(self._step(log, event))
        self.unpublished, self.touched, self.retitled = [], set(), set()
        self.stale, self.cause = False, ""
        self._epoch()
        return Batch(
            self.workspace_id, self.epoch, self.head, tuple(self.window), dict(self._walk.vis), reset=True,
            titles=dict(self._walk.titles),
        )  # fmt: skip

    def _epoch(self) -> None:
        self.epoch = hashlib.sha256(f"{self._genesis}|{self._restore}".encode()).hexdigest()[:16]

    def _scan(self) -> dict[str, tuple[int, int, int, int] | None]:
        """The signature of every log on disk now: the workspace log and each ``tickets/<uid>/events.jsonl``."""
        out = {"workspace": _sig(self.root / "events" / "workspace.jsonl")}
        try:
            names = sorted(e.name for e in os.scandir(self.root / "tickets"))
        except OSError:
            names = []
        for name in names:
            if _ULID.fullmatch(name):
                sig = _sig(self.root / "tickets" / name / "events.jsonl")
                if sig is not None:
                    out[name] = sig
        return out

    def _tickets_stamp(self) -> int | None:
        try:
            return os.stat(self.root / "tickets").st_mtime_ns
        except OSError:
            return None

    def _path(self, log: str) -> Path:
        if log == "workspace":
            return self.root / "events" / "workspace.jsonl"
        return self.root / "tickets" / log / "events.jsonl"

    def _ticket_needs(self, uid: str) -> tuple:
        from orch.model.needs import needs_of

        walk = self._walk
        assert walk is not None
        t = walk.core.tickets.get(uid)
        if t is None:
            return ()
        lc = walk.core.logs.get(uid)
        return tuple(needs_of(walk.core.ws, t, bool(lc and lc.frozen())))

    def _workspace_needs(self) -> tuple:
        from orch.model.needs import workspace_needs

        walk = self._walk
        assert walk is not None
        lc = walk.core.logs.get("workspace")
        return tuple(workspace_needs(walk.core.ws, bool(lc and lc.frozen())))

    def _step(self, log: str, e: dict[str, Any]) -> Entry:
        """Apply one event to the journal's replay and return its entry (the next ``jpos``)."""
        from orch import canon
        from orch.model.engine import apply_event

        walk = self._walk
        assert walk is not None
        core = walk.core
        actor = e.get("actor") if isinstance(e.get("actor"), Mapping) else {}
        actor_id = actor.get("id") if isinstance(actor.get("id"), str) else None
        typ = str(e.get("type"))
        self.head += 1
        if log == "workspace":
            grant_person = grant_person_of(typ, e, core.ws.grants)
            apply_event(core, log, e, walk.ctx, commit=True)
            if typ == "workspace.created":
                self._genesis = core.ws.genesis or canon.event_head(e)
            if typ == "restore":
                self._restore = canon.event_head(e)
            changed = False
            ws_needs = self._workspace_needs()
            if ws_needs != walk.ws_needs:
                walk.ws_needs, changed = ws_needs, True
            if typ in NEEDS_WORKSPACE_TYPES:
                for uid in list(core.tickets):
                    needs = self._ticket_needs(uid)
                    if needs != walk.needs.get(uid, ()):
                        walk.needs[uid], changed = needs, True
                        walk.track(uid, needs, str(e["at"]))
                    vis = vis_of(core.tickets[uid].fields.get("visibility"))
                    if vis != walk.vis.get(uid):
                        walk.vis[uid] = vis
                        self.touched.add(uid)
            seq = int(e["seq"])
            data = data_of("workspace", e, grant_person=grant_person, devices=core.ws.devices)
            detail = activity_item(self.head, "workspace", e, key=None, title=None, summary_text=summary(e), data=data)
            return Entry(
                self.head, "workspace", None, seq, seq, typ, str(e["at"]), str(actor.get("kind")), actor_id,
                needs=changed, grant_person=grant_person, ref=entry_ref(log, seq, e.get("id")), detail=detail,
            )  # fmt: skip
        before = walk.vis.get(log)
        old_needs = walk.needs.get(log, ())
        walk.last[log] = (str(e["at"]), int(e["seq"]))
        apply_event(core, log, e, walk.ctx, commit=True)
        if typ == "restore":
            self._restore = canon.event_head(e)
        t = core.tickets.get(log)
        after = vis_of(t.fields.get("visibility")) if t is not None else None
        needs = self._ticket_needs(log)
        changed = needs != old_needs or (before != after and bool(needs or old_needs))
        if t is not None:
            walk.vis[log], walk.needs[log] = after, needs  # type: ignore[assignment]
            walk.track(log, needs, str(e["at"]))
            if after != before:
                self.touched.add(log)
            title = str(t.fields.get("title", ""))
            if walk.titles.get(log) != title:
                walk.titles[log] = title
                self.retitled.add(log)
        key = t.key if t is not None else (e.get("key") if isinstance(e.get("key"), str) else None)
        detail = activity_item(
            self.head,
            log,
            e,
            key=key,
            title=None,
            summary_text=summary(e, walk.summaries.of(log)),
            data=data_of(log, e),
        )
        return Entry(
            self.head, log, key, int(e["seq"]), int(e["ws_seq"]), typ, str(e["at"]), str(actor.get("kind")),
            actor_id, before, after, changed, ref=entry_ref(log, int(e["seq"]), e.get("id")), detail=detail,
        )  # fmt: skip

    # -- the store's hooks (worker thread, under the store lock)
    def check(self, full: bool = False) -> None:
        """``before_append`` (and after every op): has someone else appended? Cheap by default (the store's own count
        of logs it found changed, one ``stat`` of ``tickets/``); ``full`` compares every log's signature."""
        if self.stale or self._walk is None:
            return
        store = self._store
        if store is not None and store.outside_changes != self._seen_changes:
            self.stale, self.cause = True, "a log changed outside the host"
            return
        if self._tickets_stamp() != self._tickets_mtime:
            self.stale, self.cause = True, "a ticket log appeared outside the host"
            return
        if not full:
            return
        now = self._scan()
        for log, sig in now.items():
            if self.sigs.get(log) != sig:
                self.stale, self.cause = True, f"{log} changed outside the host"
                return
        if set(self.sigs) - set(now):
            self.stale, self.cause = True, "a log disappeared"

    def committed(self, log: str, event: dict[str, Any]) -> None:
        """``on_commit``: this store committed ``event`` to ``log``; it takes the next ``jpos``."""
        if self.stale or self._walk is None:
            return
        try:
            if event.get("type") == "restore":  # history changed: a new epoch, built from the logs
                self.stale, self.cause = True, "a restore"
                return
            entry = self._step(log, event)
        except Exception as exc:  # noqa: BLE001 - never fail the store's commit: rebuild from the logs instead
            self.stale, self.cause = True, f"journal step failed: {type(exc).__name__}"
            return
        self.window.append(entry)
        self.unpublished.append(entry)
        sig = _sig(self._path(log))
        written = self._store.log_size(log) if self._store is not None else None
        if sig is None or written is None or sig[0] != written:  # bytes beyond what the store wrote: not ours
            self.stale, self.cause = True, f"{log} holds more than the host wrote"
        new = log not in self.sigs
        self.sigs[log] = sig
        if new:
            self._tickets_mtime = self._tickets_stamp()

    def last_event(self, uid: str) -> tuple[str, int] | None:
        """H6b: ``(at, seq)`` of ticket ``uid``'s last event, or ``None`` (unknown, or the journal is stale)."""
        walk = self._walk
        return walk.last.get(uid) if walk is not None and not self.stale else None

    def need_since(self, uid: str) -> dict[tuple[str, str | None], str]:
        """H6b: ``{(kind, ref): at}`` for the model needs of ticket ``uid`` now (``needs`` ``since``, §0.8): the ``at``
        of
        the event (of either log) after which each began to hold without a break. Empty when unknown."""
        walk = self._walk
        return dict(walk.since.get(uid, {})) if walk is not None and not self.stale else {}

    def take(self) -> Batch | None:
        """The entries committed since the last call, as a batch for the bus (``None``: nothing new)."""
        if not self.unpublished and not self.touched and not self.retitled:
            return None
        walk = self._walk
        assert walk is not None
        vis = {uid: walk.vis[uid] for uid in self.touched if uid in walk.vis}
        titles = {uid: walk.titles[uid] for uid in self.retitled if uid in walk.titles}
        batch = Batch(self.workspace_id, self.epoch, self.head, tuple(self.unpublished), vis, titles=titles)
        self.unpublished, self.touched, self.retitled = [], set(), set()
        return batch
