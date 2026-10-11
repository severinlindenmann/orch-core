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

**Foreign appends (F11).** The journal keeps the file signature (size, inode, times) of every log as of what it read
or saw committed. :meth:`check` compares them with the files now: a log that changed without a commit through this
store, or a new ticket log, was appended by someone else (tier A: an older CLI or a script holding the file-tier WSK).
The journal then marks itself stale; the worker rebuilds it after the current op
(:meth:`orch.host.workspace.WorkspaceService.sync`), which ends every subscription with ``reset``. A committed
``restore`` rewrites history, so it rebuilds the journal in the same way (a new epoch).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "WINDOW",
    "Batch",
    "Entry",
    "Journal",
    "Vis",
    "decode_cursor",
    "encode_cursor",
    "principal_hash",
    "sees",
]

#: entries kept in memory (§0.9: a cursor older than these answers ``sub.reset``)
WINDOW = 10_000
#: workspace events that cannot change a ticket view (``orch.model.state``): no ticket needs are recomputed for them
_NO_TICKET_EFFECT = frozenset(
    {"device.added", "device.removed", "grant.issued", "projection.repaired", "invalid.acknowledged"}
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


# --- cursors ----------------------------------------------------------------------------------------------------------


def principal_hash(principal: str) -> str:
    return hashlib.sha256(b"orch/v2/sub-principal|" + principal.encode()).hexdigest()[:16]


def encode_cursor(workspace_id: str, principal: str, epoch: str, jpos: int) -> str:
    """An opaque cursor (§0.9, at most 256 bytes): ``{workspace, principal hash, history epoch, jpos}``."""
    body = json.dumps(
        {"e": epoch, "j": int(jpos), "p": principal_hash(principal), "w": workspace_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return CURSOR_PREFIX + base64.urlsafe_b64encode(body).decode().rstrip("=")


def decode_cursor(cursor: Any) -> dict[str, Any] | None:
    """The fields of a cursor, or ``None`` for anything that is not one."""
    if not isinstance(cursor, str) or len(cursor) > 256 or not cursor.startswith(CURSOR_PREFIX):
        return None
    raw = cursor[len(CURSOR_PREFIX) :]
    try:
        body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict) or set(body) != {"e", "j", "p", "w"}:
        return None
    if not isinstance(body["j"], int) or isinstance(body["j"], bool) or body["j"] < 0:
        return None
    if not all(isinstance(body[k], str) for k in ("e", "p", "w")):
        return None
    return body


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


class Journal:
    def __init__(self, workspace_id: str, root: str | os.PathLike[str]) -> None:
        self.workspace_id = workspace_id
        self.root = Path(root)
        self.window: deque[Entry] = deque(maxlen=WINDOW)
        self.head = 0
        self.epoch = ""
        #: entries committed since the last :meth:`take`
        self.unpublished: list[Entry] = []
        #: tickets whose visibility changed since the last :meth:`take`
        self.touched: set[str] = set()
        #: the journal no longer matches the logs (a foreign append or a restore): rebuild before using it
        self.stale = False
        #: why it went stale (for the audit line)
        self.cause = ""
        self.sigs: dict[str, tuple[int, int, int, int] | None] = {}
        self._walk: _Walk | None = None
        self._genesis = ""
        self._restore = ""

    # -- positions
    @property
    def floor(self) -> int:
        """The oldest cursor position whose following entries are all in the window."""
        return self.window[0].jpos - 1 if self.window else self.head

    def cursor(self, principal: str, jpos: int | None = None) -> str:
        return encode_cursor(self.workspace_id, principal, self.epoch, self.head if jpos is None else jpos)

    def position(self, cursor: Any, principal: str) -> int | None:
        """The ``jpos`` a client cursor stands for, or ``None`` (``sub.reset``): not a cursor, another workspace,
        principal or epoch, older than the window, or beyond the head."""
        body = decode_cursor(cursor)
        if body is None or body["w"] != self.workspace_id or body["p"] != principal_hash(principal):
            return None
        if body["e"] != self.epoch or not self.floor <= body["j"] <= self.head:
            return None
        return int(body["j"])

    # -- building (worker thread)
    def build(self, store: Any) -> Batch:
        """Rebuild from the logs (start, a foreign append, a restore). Returns the whole window as a reset batch."""
        from orch.model.types import Core

        with store.locked():
            events = store.merged_events()
            self.sigs = self._scan()
            ctx = store.state._ctx  # the store's own replay context: verifier and both pins
        self._walk = _Walk(Core(), ctx)
        self.window = deque(maxlen=WINDOW)
        self.head, self.unpublished, self.touched = 0, [], set()
        self._genesis = self._restore = ""
        for log, event in events:
            self.window.append(self._step(log, event))
        self.unpublished, self.touched = [], set()
        self.stale, self.cause = False, ""
        self._epoch()
        return Batch(self.workspace_id, self.epoch, self.head, tuple(self.window), dict(self._walk.vis), reset=True)

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
            grant_person = None
            if typ == "grant.issued":
                grant_person = actor_id
            elif typ == "grant.revoked":
                g = core.ws.grants.get(e.get("grant"))
                grant_person = g.person if g is not None else None
            apply_event(core, log, e, walk.ctx, commit=True)
            if typ == "workspace.created":
                self._genesis = core.ws.genesis or canon.event_head(e)
            if typ == "restore":
                self._restore = canon.event_head(e)
            changed = False
            ws_needs = self._workspace_needs()
            if ws_needs != walk.ws_needs:
                walk.ws_needs, changed = ws_needs, True
            if typ not in _NO_TICKET_EFFECT:
                for uid in list(core.tickets):
                    needs = self._ticket_needs(uid)
                    if needs != walk.needs.get(uid, ()):
                        walk.needs[uid], changed = needs, True
                    vis = vis_of(core.tickets[uid].fields.get("visibility"))
                    if vis != walk.vis.get(uid):
                        walk.vis[uid] = vis
                        self.touched.add(uid)
            seq = int(e["seq"])
            return Entry(
                self.head, "workspace", None, seq, seq, typ, str(e["at"]), str(actor.get("kind")), actor_id,
                needs=changed, grant_person=grant_person,
            )  # fmt: skip
        before = walk.vis.get(log)
        old_needs = walk.needs.get(log, ())
        apply_event(core, log, e, walk.ctx, commit=True)
        if typ == "restore":
            self._restore = canon.event_head(e)
        t = core.tickets.get(log)
        after = vis_of(t.fields.get("visibility")) if t is not None else None
        needs = self._ticket_needs(log)
        changed = needs != old_needs or (before != after and bool(needs or old_needs))
        if t is not None:
            walk.vis[log], walk.needs[log] = after, needs  # type: ignore[assignment]
            if after != before:
                self.touched.add(log)
        key = t.key if t is not None else (e.get("key") if isinstance(e.get("key"), str) else None)
        return Entry(
            self.head, log, key, int(e["seq"]), int(e["ws_seq"]), typ, str(e["at"]), str(actor.get("kind")),
            actor_id, before, after, changed,
        )  # fmt: skip

    # -- the store's hooks (worker thread, under the store lock)
    def check(self) -> None:
        """``before_append``: a log that changed since the journal last read it, or a new one, is a foreign append."""
        if self.stale or self._walk is None:
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
        self.sigs[log] = _sig(self._path(log))

    def take(self) -> Batch | None:
        """The entries committed since the last call, as a batch for the bus (``None``: nothing new)."""
        if not self.unpublished and not self.touched:
            return None
        walk = self._walk
        assert walk is not None
        vis = {uid: walk.vis[uid] for uid in self.touched if uid in walk.vis}
        batch = Batch(self.workspace_id, self.epoch, self.head, tuple(self.unpublished), vis)
        self.unpublished, self.touched = [], set()
        return batch
