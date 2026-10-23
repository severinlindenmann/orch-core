"""The persisted ``event_deliver`` queue (protocol.md §8.7, L2-C3-4).

``DeliveryQueue`` is the custodial engine's ``deliver`` callable: ``queue(ws, log, signed)`` stores the signed event's
exact canonical bytes in a private SQLite file BEFORE it returns (the engine refuses ``unavailable`` when that fails, so
an event is either queued or never sent). A pump then sends, per workspace, the oldest pending event over the
workspace's live link as ``event_deliver {log, event}``: one in flight per link, later events wait behind it.

The host's reply decides, through the contract's reference :func:`orch_hub.contract.phase4.deliver_next` (vectors
``deliver_*``):

* ``{}``: done (also the reply to a duplicate: the host dedups by event id);
* ``unavailable`` / ``unknown_snapshot``, no reply (timeout, link drop) or an unreadable reply: send the SAME bytes
  again after ``min(1 s * 2^(n-1), 5 min)``; the wait is stored, so a new link does not skip it;
* any other code: final, the event is dropped and the channel told the code (``notify``);
* 24 h after the first send: dropped with the last code.

Invariants: a resend is the stored bytes, never a rebuilt or re-signed event (the queue has no way to produce an event);
no event bytes, certificates or signatures in logs; the file is private (:mod:`orch_hub.fsutil`); an unreadable stored
event is dropped as ``internal`` rather than sent. Ended rows keep only the outcome (ids, type, code, attempts): that is
the hub-local audit of delivery (the chain has no delivery record type).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from ..contract import phase4 as P4
from ..contract.canon import CanonError, cj, loads_strict
from ..fsutil import open_private_db

_log = logging.getLogger("orch_hub.deliver")

REPLY_TIMEOUT_MS = 30_000  # no reply this long after a send counts as "no reply" (§8.7)
KEEP_ENDED_MS = 30 * 24 * 3600 * 1000  # ended rows (outcome only) are pruned after 30 days
DELIVER_FILE = "event-deliver.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliver (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ws TEXT NOT NULL, log TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL,
  user_id TEXT NOT NULL, channel_id TEXT NOT NULL,
  event BLOB NOT NULL,
  enq_ms INTEGER NOT NULL, first_sent_ms INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
  next_ms INTEGER NOT NULL, last_code TEXT,
  state TEXT NOT NULL CHECK (state IN ('pending','done','dropped')) DEFAULT 'pending',
  end_ms INTEGER, end_code TEXT
);
CREATE INDEX IF NOT EXISTS deliver_pending ON deliver (ws, state, id);
"""


class LinkPort(Protocol):
    """One live link: signs and writes an h2w request, returns its ``seq`` (raises when the link is gone)."""

    def send_request(self, op: str, body: dict[str, Any]) -> int: ...


class _Link:
    def __init__(self, port: LinkPort) -> None:
        self.port = port
        self.lock = threading.Lock()
        self.inflight: tuple[int, int, int] | None = None  # (row id, h2w seq, sent_ms)
        self.dead = False
        self.strikes = 0  # consecutive sends on this link that got no reply (a silent link is tried last)


class DeliveryQueue:
    def __init__(
        self,
        path: str = ":memory:",
        *,
        now_ms: Callable[[], int] | None = None,
        reply_timeout_ms: int = REPLY_TIMEOUT_MS,
        notify: Callable[[dict[str, Any]], None] | None = None,
        poll_s: float = 0.25,
        sender: Callable[[str, str, dict[str, Any]], int | None] | None = None,
    ) -> None:
        """``sender(ws, log, event)`` is the write seam (serve wiring: ``Router.send_event``): it writes one
        ``event_deliver`` to the workspace's live link and raises when it cannot. It MUST return the h2w ``seq`` of the
        write: a reply counts only when it matches (workspace, seq) of the one in-flight send, so a late ack of an
        earlier attempt can never end a later event. A sender that returns no seq is a failed send. Without it the queue
        writes on the port the link server attached.

        ``notify(outcome)`` is called once per ended event with ``{workspace_id, log, event_id, type, user_id,
        channel_id, outcome ("done" | "dropped"), code (None when done), attempts}``: tell the channel (D13). It never
        carries event bytes and a failing hook never stops the queue."""
        self._sender = sender
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._timeout, self._notify, self._poll = reply_timeout_ms, notify, poll_s
        self._dblock = threading.RLock()
        self._db = open_private_db(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._links: dict[str, list[_Link]] = {}  # per workspace, in attach order
        self._links_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.prune()

    # --- the engine's `deliver` --------------------------------------------------------------------------------------

    def __call__(self, ws: str, log: str, event: Mapping[str, Any]) -> None:
        """Queue one signed event durably. Raises when it cannot be stored (the engine then refuses ``unavailable``)."""
        blob = cj(dict(event))
        actor = event["actor"]["id"]
        now = self._now()
        with self._dblock:
            if self._db.execute("SELECT 1 FROM deliver WHERE event_id=?", (event["id"],)).fetchone() is not None:
                _log.warning("event %s is already queued; not queued twice", event["id"])
                return
            self._db.execute(
                "INSERT INTO deliver (ws, log, event_id, event_type, user_id, channel_id, event, enq_ms, "
                "next_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (ws, log, event["id"], event["type"], actor[2:], event["channel"], blob, now, now),
            )
            self._db.commit()
        self._wake.set()

    enqueue = __call__

    # --- links -------------------------------------------------------------------------------------------------------

    def attach(self, ws: str, port: LinkPort) -> _Link:
        """A workspace's link is up (authenticated, bound): its pending events go out. Links never replace each other:
        a short-lived second link (the host's ``snapshot_get``) must not take the queue from the main one (#51). A
        delivery is bound to the link it was sent on; when that link closes, the event is retried on a remaining one."""
        link = _Link(port)
        with self._links_lock:
            self._links.setdefault(ws, []).append(link)
        self._wake.set()
        return link

    def detach(self, ws: str, link: _Link) -> None:
        self._kill(link)
        with self._links_lock:
            ls = self._links.get(ws, [])
            if link in ls:
                ls.remove(link)
            if not ls:
                self._links.pop(ws, None)
        self._wake.set()  # the event that was in flight on it goes out again on a remaining link, after its backoff

    def _kill(self, link: _Link) -> None:
        """The link is over. An event in flight on it got no reply: it waits out its backoff (§8.7) before any new link
        sends it again."""
        with link.lock:
            link.dead = True
            if link.inflight is not None:
                row_id, _seq, _sent = link.inflight
                link.inflight = None
                self._settle(row_id, None)

    def on_reply(self, ws: str, link: _Link, o: Mapping[str, Any]) -> None:
        """A w2h reply on ``link``. Only the reply to the in-flight ``event_deliver`` counts; anything else is ignored."""
        if o.get("op") != "event_deliver":
            return
        with link.lock:
            if link.inflight is None or link.inflight[1] != o.get("re"):
                _log.info("event_deliver reply for ws %s matches no in-flight send; dropped", ws)
                return
            row_id, _seq, _sent = link.inflight
            link.inflight = None
            link.strikes = 0
            self._settle(row_id, o.get("body"))
        self._wake.set()

    # --- the pump ----------------------------------------------------------------------------------------------------

    def pump(self) -> None:
        """One pass over every attached link (also what the thread runs): settle a timed-out send, send the next due
        event. Safe to call from tests with a fake clock."""
        with self._links_lock:
            by_ws = {ws: list(ls) for ws, ls in self._links.items()}
        for ws, links in by_ws.items():
            self._pump_ws(ws, links)

    def _pump_ws(self, ws: str, links: list[_Link]) -> None:
        """One event in flight per workspace, whatever the number of links. A timed-out send is settled and its link
        struck; the next send goes to the live link with the fewest strikes (the oldest on a tie: the main link)."""
        for link in links:
            with link.lock:
                if link.dead or link.inflight is None:
                    continue
                row_id, _seq, sent = link.inflight
                if self._now() - sent < self._timeout:
                    return  # still waiting for its reply
                link.inflight = None
                link.strikes += 1
                self._settle(row_id, None)  # no reply in time: resend later (same bytes)
        for link in sorted((x for x in links if not x.dead), key=lambda x: x.strikes):  # stable: attach order
            with link.lock:
                if link.dead:
                    continue
                self._send_head(ws, link)
            return

    def _send_head(self, ws: str, link: _Link) -> None:
        now = self._now()
        with self._dblock:
            row = self._db.execute(
                "SELECT id, log, event, enq_ms, first_sent_ms, attempts, next_ms, last_code FROM deliver "
                "WHERE ws=? AND state='pending' ORDER BY id LIMIT 1",
                (ws,),
            ).fetchone()
        if row is None:
            return
        row_id, log, blob, enq_ms, first_sent, _attempts, next_ms, last_code = row
        if now - (first_sent if first_sent is not None else enq_ms) >= P4.DELIVER_GIVE_UP_MS:
            self._end(row_id, "dropped", last_code or "unavailable")  # never reached the host in 24 h
            return
        if next_ms > now:
            return  # still under the backoff wait (also after a link drop)
        try:
            event = loads_strict(bytes(blob))
            if type(event) is not dict:
                raise CanonError("not an object")
        except CanonError:
            _log.error("a queued event is unreadable; dropped without sending")
            self._end(row_id, "dropped", "internal")
            return
        with self._dblock:  # count the attempt BEFORE the send: a crash in between still counts
            self._db.execute(
                "UPDATE deliver SET attempts=attempts+1, first_sent_ms=COALESCE(first_sent_ms, ?) WHERE id=?",
                (now, row_id),
            )
            self._db.commit()
        try:
            if self._sender is not None:
                r = self._sender(ws, log, event)
                if type(r) is not int or r < 1:
                    raise OSError("sender returned no seq")  # unmatchable reply: treated as a failed send
                seq = r
            else:
                seq = link.port.send_request("event_deliver", {"log": log, "event": event})
        except Exception:  # noqa: BLE001 (the link is gone: no reply)
            link.dead = self._sender is None
            self._settle(row_id, None)
            return
        link.inflight = (row_id, seq, now)

    def _settle(self, row_id: int, reply: Any) -> None:
        """Apply the §8.7 table to the reply (None = no reply) of the attempt that was just made."""
        now = self._now()
        with self._dblock:
            row = self._db.execute(
                "SELECT attempts, enq_ms, first_sent_ms FROM deliver WHERE id=? AND state='pending'", (row_id,)
            ).fetchone()
        if row is None:
            return
        attempts, enq_ms, first_sent = row
        res = P4.deliver_next(
            reply,
            attempt=attempts,
            first_sent_ms=first_sent if first_sent is not None else enq_ms,
            now_ms=now,
        )
        if res.get("done"):
            self._end(row_id, "done", None)
        elif "drop" in res:
            self._end(row_id, "dropped", res["drop"])
        else:
            code = reply["error"] if type(reply) is dict and type(reply.get("error")) is str else "unavailable"
            with self._dblock:
                self._db.execute(
                    "UPDATE deliver SET next_ms=?, last_code=? WHERE id=?", (now + res["resend_in_ms"], code, row_id)
                )
                self._db.commit()
            _log.info("event delivery will be retried (code %s) in %d ms", code, res["resend_in_ms"])

    def _end(self, row_id: int, outcome: str, code: str | None) -> None:
        now = self._now()
        with self._dblock:
            row = self._db.execute(
                "SELECT ws, log, event_id, event_type, user_id, channel_id, attempts FROM deliver WHERE id=?",
                (row_id,),
            ).fetchone()
            if row is None:
                return
            self._db.execute(
                "UPDATE deliver SET state=?, end_ms=?, end_code=?, event=x'' WHERE id=? AND state='pending'",
                (outcome, now, code, row_id),
            )
            self._db.commit()
        ws, log, event_id, etype, user, channel, attempts = row
        _log.info(
            "custodial delivery %s: workspace=%s event=%s type=%s code=%s attempts=%d",
            outcome,
            ws,
            event_id,
            etype,
            code,
            attempts,
        )
        if self._notify is not None:
            try:
                self._notify(
                    {
                        "workspace_id": ws,
                        "log": log,
                        "event_id": event_id,
                        "type": etype,
                        "user_id": user,
                        "channel_id": channel,
                        "outcome": outcome,
                        "code": code,
                        "attempts": attempts,
                    }
                )
            except Exception:  # noqa: BLE001 - a hook must never stop the queue
                _log.warning("delivery notify hook failed")

    # --- reads -------------------------------------------------------------------------------------------------------

    def pending(self, ws: str | None = None) -> list[dict[str, Any]]:
        return self._rows("pending", ws)

    def outcomes(self, ws: str | None = None) -> list[dict[str, Any]]:
        """Ended events, oldest first: ``{event_id, type, outcome, code, attempts, ...}``. Never the bytes."""
        return self._rows("done", ws) + self._rows("dropped", ws)

    def _rows(self, state: str, ws: str | None) -> list[dict[str, Any]]:
        q = (
            "SELECT ws, log, event_id, event_type, user_id, channel_id, attempts, enq_ms, first_sent_ms, next_ms, "
            "last_code, state, end_ms, end_code FROM deliver WHERE state=?"
        )
        a: tuple[Any, ...] = (state,)
        if ws is not None:
            q, a = q + " AND ws=?", (state, ws)
        with self._dblock:
            rows = self._db.execute(q + " ORDER BY id", a).fetchall()
        keys = (
            "workspace_id", "log", "event_id", "type", "user_id", "channel_id", "attempts", "enq_ms", "first_sent_ms",
            "next_ms", "last_code", "state", "end_ms", "end_code",
        )  # fmt: skip
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def prune(self) -> None:
        with self._dblock:
            self._db.execute(
                "DELETE FROM deliver WHERE state!='pending' AND end_ms < ?", (self._now() - KEEP_ENDED_MS,)
            )
            self._db.commit()

    # --- the thread --------------------------------------------------------------------------------------------------

    def start(self) -> DeliveryQueue:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="event-deliver", daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._poll)
            self._wake.clear()
            try:
                self.pump()
            except Exception:  # noqa: BLE001 - the pump must outlive one bad pass
                _log.exception("event delivery pass failed")

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._dblock:
            self._db.close()


def build_delivery(
    state_dir: str,
    *,
    notify: Callable[[dict[str, Any]], None] | None = None,
    now_ms: Callable[[], int] | None = None,
    sender: Callable[[str, str, dict[str, Any]], int | None] | None = None,
) -> DeliveryQueue:
    """The builder the serve wiring calls: the queue in ``<state_dir>/event-deliver.db`` (private), thread not started.
    Wire it three ways (``sender=router.send_event`` once that exists): ``CustodialEngine(deliver=queue)``, ``LinkServer(deliveries=queue)``, ``queue.start()`` /
    ``queue.close()`` with the server."""
    return DeliveryQueue(os.path.join(state_dir, DELIVER_FILE), now_ms=now_ms, notify=notify, sender=sender)
