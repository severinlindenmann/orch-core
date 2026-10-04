"""Events to addons (v2 §11.8). Core commands only append events. `orch serve` pumps new events into each addon's
outbox through `on_event` (enqueue only, no commands) and drains the outbox in the background."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import json
import uuid
from pathlib import Path

from orch.addons.cache import cache_dir
from orch.addons.loader import _log_error
from orch.addons.runner import rendering
from orch.clock import stamp_s
from orch.core.events import last_seq, read_events
from orch.core.fsutil import atomic_write_text


def outbox_path(ws, name: str) -> Path:
    return cache_dir(ws, name) / "outbox.jsonl"


def _cursor_path(ws, name: str) -> Path:
    return cache_dir(ws, name) / "events.cursor"


class Outbox:
    def __init__(self, path: Path):
        self.path = Path(path)

    def put(self, data: dict, *, item_id: str | None = None) -> str:
        """Append one item. With `item_id`, an item already pending under that id is not added again."""
        item = {"id": item_id or uuid.uuid4().hex, "at": stamp_s(), "data": data}
        try:
            line = json.dumps(item, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            raise ValueError(f"outbox items must be JSON values ({e})") from e
        if item_id is not None and any(i["id"] == item_id for i in self.pending()):
            return item_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
        return item["id"]

    def pending(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []
        out = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                out.append(item)
        return out

    def ack(self, ids) -> int:
        done = {i for i in ids if isinstance(i, str)}
        items = self.pending()
        keep = [i for i in items if i["id"] not in done]
        atomic_write_text(self.path, "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in keep))
        return len(items) - len(keep)


class _EventBox:
    """The outbox as `on_event` sees it for one event: item ids follow from (addon, event seq, n), so pumping
    the same event again (a cursor that was not saved) adds nothing twice."""

    def __init__(self, box: Outbox, prefix: str):
        self._box, self._prefix, self._n = box, prefix, 0

    def put(self, data: dict) -> str:
        self._n += 1
        return self._box.put(data, item_id=f"{self._prefix}-{self._n}")


def pump(ws, loaded) -> int:
    """Hand the events after the addon's cursor to its `on_event`; the first pump only sets the cursor to now."""
    if not loaded.has("events") or not hasattr(loaded.obj, "on_event"):
        return 0
    cpath = _cursor_path(ws, loaded.name)
    newest = last_seq(ws)
    try:
        cursor = int(cpath.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        cursor = None
    if cursor is None or cursor > newest:  # first pump, or the log was reset: start now, never replay history
        atomic_write_text(cpath, f"{newest}\n")
        return 0
    events = read_events(ws, after=cursor)
    box = Outbox(outbox_path(ws, loaded.name))
    for event in events:
        try:
            with rendering():  # on_event may only enqueue: no ctx.run here
                # a copy: the events are shared with every other reader of the log (core caches the parsed log)
                mine = dataclasses.replace(event, data=copy.deepcopy(event.data))
                loaded.obj.on_event(mine, _EventBox(box, f"{loaded.name}-{event.seq}"))
        except Exception:
            _log_error(ws, loaded.name, f"on_event {event.kind} {event.ticket}")
        cursor = event.seq
    if events:
        atomic_write_text(cpath, f"{cursor}\n")
    return len(events)


def drain(ws, loaded) -> int:
    """Give the pending outbox items to the addon's `drain`; remove the ones it returns as acknowledged."""
    if not loaded.has("events") or not hasattr(loaded.obj, "drain"):
        return 0
    box = Outbox(outbox_path(ws, loaded.name))
    items = box.pending()
    if not items:
        return 0
    try:
        acked = loaded.obj.drain(loaded.ctx.provider_context(), items) or []
        if not isinstance(acked, (list, tuple, set, frozenset)):
            raise TypeError(f"drain must return a list of acknowledged ids, not {type(acked).__name__}")
    except Exception:
        _log_error(ws, loaded.name, "drain")
        return 0
    return box.ack(acked)


def forget_cursors(ws, registry) -> None:
    """Drop the event cursor of every addon that is not loaded (disabled, untrusted, removed), so enabling it
    again starts at now and never replays what happened meanwhile."""
    if not getattr(registry, "imported", True):
        return
    loaded = {a.name for a in registry}
    base = ws.state_dir / "addons"
    if not base.is_dir():
        return
    for cpath in base.glob("*/events.cursor"):
        if cpath.parent.name not in loaded:
            with contextlib.suppress(OSError):
                cpath.unlink()


def pump_one(ws, loaded) -> None:
    try:
        pump(ws, loaded)
        drain(ws, loaded)
    except Exception:  # one addon's broken files never stop the others
        _log_error(ws, loaded.name, "outbox")


def pump_all(ws, registry) -> None:
    """Synchronous: every addon in turn (tests and tools). `orch serve` uses OutboxPump instead."""
    forget_cursors(ws, registry)
    for loaded in registry:
        pump_one(ws, loaded)


class OutboxPump:
    """Pumps and drains each addon in its own worker thread, so a hanging addon never stalls the others.
    Single-flight per addon: while an addon's thread is still busy (also after a timeout), it is skipped."""

    def __init__(self, ws, *, event_timeout: float = 5.0, drain_timeout: float = 60.0):
        self.ws = ws
        self.event_timeout, self.drain_timeout = event_timeout, drain_timeout
        self.busy: set[str] = set()
        self._tasks: set[asyncio.Future] = set()

    def start_round(self, registry) -> list[str]:
        """Start one round (call on the event loop); returns the addons started."""
        started = []
        for loaded in registry:
            if loaded.name in self.busy or not loaded.has("events"):
                continue
            self.busy.add(loaded.name)
            task = asyncio.ensure_future(self._one(loaded))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            started.append(loaded.name)
        return started

    async def _step(self, loaded, fn, timeout: float, what: str) -> bool:
        """Run fn(ws, loaded) in a thread; False when it timed out (the addon stays busy until it returns)."""
        future = asyncio.ensure_future(asyncio.to_thread(fn, self.ws, loaded))
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            _log_error(self.ws, loaded.name, what, f"timed out after {timeout:g} s\n")
            future.add_done_callback(lambda _f, name=loaded.name: self.busy.discard(name))
            return False

    async def _one(self, loaded) -> None:
        release = True
        try:
            if not await self._step(loaded, pump, self.event_timeout, "on_event"):
                release = False
                return
            if not await self._step(loaded, drain, self.drain_timeout, "drain"):
                release = False
        except Exception:
            _log_error(self.ws, loaded.name, "outbox")
        finally:
            if release:
                self.busy.discard(loaded.name)

    async def run_round(self, registry) -> None:
        await asyncio.to_thread(forget_cursors, self.ws, registry)
        self.start_round(registry)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
