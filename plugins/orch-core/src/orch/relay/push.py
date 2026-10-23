"""H13a: push, the host end (orch-v2-host 11A.16.4; "As built in B4"): the planner, the sealing step, the dispatch step
and the push thread ``orch-relay-push-<id8>`` (a worker companion).

* **Planner** (:func:`plan`, a worker job: it needs the store lock). A durable per-log cursor in
  ``outbound.sqlite``; an event's intent key is ``H(log ‖ seq ‖ event_hash ‖ kind ‖ id)``; the start fence
  (``meta.start_ms``) keeps history from being pushed; a rewritten log invalidates the intents of the abandoned
  branch; ``deadline_ms = event_at + ttl`` is fixed at insert. Kinds: ``question`` (a committed ``question.asked``)
  and ``question.closed`` (a ``question.answered``), narrowed to the question's addressee persons minus the asking or
  answering device. ``drop.new`` is H13b's.
* **Seal** (:func:`seal_step`, a worker job): up to 32 ``planned`` intents; deadline, then the audience (D4), then, in
  ONE ``BridgeKeys.push_sealing`` span, ``ts_ms = max(now, push_ts.last + 1)`` per id, the seal, and the outer
  persisted (``OutboundStore.seal``) before any send.
* **Dispatch** (:func:`dispatch_step`, a worker job, before EVERY send attempt): deadline, audience again (D5), the
  host budgets (6 pushes per 10 min per device, one per id per 60 s), the targets recorded with ``auth_gen``.
* **Send** (the push thread, never under the store lock): ``POST /v2/workspaces/{ws}/push`` with ``Orch-Holder`` and
  the PERSISTED outer, resent unchanged on every retry, never resealed except by a 409 ``stale_epoch`` (at most 3
  times, then ``push_epoch_loop``). Answer classes: 200 sent; transport errors, 5xx, 401 and 429/503 retry the same
  bytes with backoff (1, 2, 4 ... 60 s; ``Retry-After`` is a floor) while the deadline holds; 404 is "push is off at
  this relay" (open intents dropped, probe at most hourly); 409 ``fenced`` stops the worker (the session's path);
  400 ``malformed`` fails the intent (``push_malformed``).

Lock order (11A.16.2): store lock, then the seal lock (inside ``push_sealing``), then the ``OutboundStore`` lock. The
push thread itself takes none of the first two: it reaches the store only through ``job``.
"""

from __future__ import annotations

import calendar
import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from orch import canon
from orch.crypto import push as cpush
from orch.net.http import TransportError

from .audience import PushContext, Standing, devices_for
from .keys import KeyMissing, SealRefused
from .outbound import Intent, NewIntent, OutboundStore
from .retry import RelayRefused
from .session import Fenced, NotHolder, RelayMisbehaving, Session, SessionError

__all__ = [
    "DEVICE_BUDGET",
    "DEVICE_BUDGET_WINDOW_MS",
    "ID_INTERVAL_MS",
    "MAX_RESEALS",
    "MAX_TARGETS",
    "PushThread",
    "dispatch_step",
    "plan",
    "seal_step",
]

log = logging.getLogger("orch.relay.push")

TTL_S = 86_400  # question and question.closed: high priority, 24 h (11A.16.4 "Kinds")
SEAL_BATCH = 32
MAX_TARGETS = 1024  # the relay's cap per request; a larger set is split
MAX_RESEALS = 3  # a 409 stale_epoch sends the intent back to planned this many times, then push_epoch_loop
DEVICE_BUDGET = (
    6  # pushes per DEVICE_BUDGET_WINDOW_MS per (device, workspace): the host's own, stricter than the relay's
)
DEVICE_BUDGET_WINDOW_MS = 600_000
ID_INTERVAL_MS = 60_000  # one push per id per minute
PLAN_EVERY_S = 30.0  # the planner runs at least this often (and on every append)
PROBE_FIRST_MS = 60_000  # push off at the relay (404): probe again after 60 s, then back off ...
PROBE_EVERY_MS = 3_600_000  # ... up to hourly (and at once on every relay (re)link or worker start)
PLAN_LIMIT = 256
SKEW_S = 60  # a log whose mtime is older than the fence by more than this cannot hold a post-fence event
BACKOFF_MAX_S = 60.0
UNKNOWN_FAILURES_MAX = 10  # consecutive unclassified failures before the push thread stops (fail closed)
SEND_DEADLINE_S = 30.0
ROUTE = "POST /v2/workspaces/{ws}/push"
_FALLBACK_LABEL = "New question"
#: B10 Part B: the phone-approval hint's push. The generic wake: a fixed label, the hint's opaque id, nothing else
HINT_LOG = "phone.hint"
HINT_LABEL = "Needs your approval"
HINT_KIND = "ticket.update"  # a protocol kind (the phone's set is closed); the id is the hint's, the label fixed
#: ``hints(after)`` -> ``(highest counter seen, [open hint dicts with a counter above after])`` (the host's; it reads
#: the hint file and the log under the store lock the planner already holds)
HintSource = Callable[[int], tuple[int, list[dict[str, Any]]]]


class PushStore(Protocol):
    """What the planner and the audience read of the workspace store (``orch.store.Store``); all under ``locked()``."""

    auth_gen: int

    def locked(self) -> Any: ...

    @property
    def state(self) -> Any: ...

    def log_marks(self) -> dict[str, tuple[int, int]]: ...

    def events_after(self, log: str, after: int, limit: int = ...) -> list[tuple[dict[str, Any], str]]: ...

    def head_at(self, log: str, seq: int) -> str | None: ...

    def log_seq(self, log: str) -> int | None: ...

    def seq_before(self, log: str, at_epoch: int) -> int: ...


# -- the planner ------------------------------------------------------------------------------------------------------


def _at_s(stamp: str) -> int:
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))


def question_label(text: object) -> str:
    """A push label for a question text: whitespace collapsed, a fixed point of ``clean_label`` (at most 80 code
    points, not ending in a space), else the fallback. Padding to 80 is the sealer's."""
    from orch.identity.errors import Refused
    from orch.identity.pairing import clean_label

    if type(text) is not str:
        return _FALLBACK_LABEL
    try:
        label = clean_label(" ".join(text.split())).rstrip()
        return label if label and clean_label(label) == label else _FALLBACK_LABEL
    except Refused:
        return _FALLBACK_LABEL


def _device_hex(actor: Any) -> frozenset[str]:
    dev = actor.get("device") if isinstance(actor, dict) else None
    if type(dev) is str and dev.startswith("d_") and len(dev) == 34:
        return frozenset({dev[2:]})
    return frozenset()


def _intents_for(
    event: dict[str, Any], head: str, log_name: str, state: Any, now_ms: int, start_s: int
) -> list[NewIntent]:
    typ = event.get("type")
    if typ not in ("question.asked", "question.answered"):
        return []
    try:
        if _at_s(event["at"]) < start_s:
            return []  # the start fence (Q-H2): nothing from before this outbox existed
        at_ms = _at_s(event["at"]) * 1000
    except (KeyError, ValueError, TypeError):
        return []
    view = state.tickets.get(log_name)
    if view is None:
        return []
    qlocal = event["question"]["id"] if typ == "question.asked" else event.get("question")
    qv = next((x for x in view.questions if x.id == qlocal), None)
    if qv is None or event.get("hash") != qv.hash or not qv.qid.isascii() or not 0 < len(qv.qid) <= 128:
        return []  # unknown, or superseded by a re-ask: the newer event gets its own intent
    if typ == "question.asked":
        if qv.answered or event.get("qid") != qv.qid:
            return []
        kind, label = "question", question_label(event["question"].get("text"))
    else:
        kind, label = "question.closed", "Answered"
    return [
        NewIntent(
            log=log_name, seq=event["seq"], event_hash=head, kind=kind, id=qv.qid, ticket=log_name,
            narrow=frozenset(qv.addressees), exclude=_device_hex(event.get("actor")), label=label, priority="high",
            deadline_ms=at_ms + TTL_S * 1000, created_ms=now_ms,
        )
    ]  # fmt: skip


class Planner:
    """The per-workspace planner state (the marks of the logs already read)."""

    def __init__(self, store: PushStore, ob: OutboundStore, hints: HintSource | None = None) -> None:
        self.store, self.ob, self.hints = store, ob, hints
        self._marks: dict[str, tuple[int, int]] = {}

    def run(self, now_ms: int) -> int:
        """One planning pass (call it as a worker job). Returns the number of intents inserted."""
        store, ob = self.store, self.ob
        total = 0
        start_s = ob.start_ms // 1000
        with store.locked():
            marks = store.log_marks()
            state = store.state
            for log_name in sorted(marks):
                if log_name == "workspace" or self._marks.get(log_name) == marks[log_name]:
                    continue  # the workspace log holds no push kind; an unchanged log was read already
                cur = ob.cursor(log_name)
                if cur is None:
                    if marks[log_name][1] // 1_000_000_000 < start_s - SKEW_S:
                        self._marks[log_name] = marks[log_name]
                        continue  # untouched since before the fence: nothing in it can be pushed
                    seq0 = store.seq_before(log_name, start_s)
                    cur = (seq0, store.head_at(log_name, seq0))
                    ob.plan(log_name, cur[0], cur[1])
                else:
                    got = store.head_at(log_name, cur[0])
                    if got != cur[1]:
                        if got is None and store.log_seq(log_name) is None:
                            continue  # unreadable right now (a read error): retry later, never "rewritten"
                        cur = self._rewritten(log_name, start_s)
                seq, head = cur
                while True:
                    evs = store.events_after(log_name, seq, PLAN_LIMIT)
                    if not evs:
                        break
                    new = [i for e, h in evs for i in _intents_for(e, h, log_name, state, now_ms, start_s)]
                    seq, head = evs[-1][0]["seq"], evs[-1][1]
                    total += ob.plan(log_name, seq, head, new)
                    if len(evs) < PLAN_LIMIT:
                        break
                self._marks[log_name] = marks[log_name]
            total += self._plan_hints(now_ms)
        return total

    def _plan_hints(self, now_ms: int) -> int:
        """B10 Part B: one ``phone.request`` intent per new OPEN phone-approval hint (cursor ``phone.hint``, the hint
        counter). The intent holds no ticket text: the push is a wake, the phone asks ``phone.requests``."""
        if self.hints is None:
            return 0
        cur = self.ob.cursor(HINT_LOG)
        after = cur[0] if cur else 0
        try:
            top, fresh = self.hints(after)
        except Exception as e:  # noqa: BLE001 - a bad hint file never stops question pushes (fail closed for hints)
            log.warning("phone hints unreadable: %s", type(e).__name__)
            return 0
        if top <= after:
            return 0
        new = [
            NewIntent(
                log=HINT_LOG, seq=h["n"], event_hash=h["id"], kind=HINT_KIND, id=h["id"], ticket=h["uid"],
                narrow=frozenset({h["person"]}), exclude=frozenset(), label=HINT_LABEL, priority="high",
                deadline_ms=int(h["expires"]) * 1000, created_ms=now_ms,
            )
            for h in fresh
        ]  # fmt: skip
        return self.ob.plan(HINT_LOG, top, None, new)

    def _rewritten(self, log_name: str, start_s: int) -> tuple[int, str | None]:
        """The log no longer holds the cursor's point (a restore abandoned a tail): every unsent intent whose
        ``(seq, event_hash)`` is no longer in the log is invalidated, and planning restarts at the fence (an event
        already planned has its key, so ``INSERT OR IGNORE`` finds it)."""
        dead = [i.key for i in self.ob.open_intents(log_name) if self.store.head_at(log_name, i.seq) != i.event_hash]
        seq0 = self.store.seq_before(log_name, start_s)
        head0 = self.store.head_at(log_name, seq0)
        self.ob.plan(log_name, seq0, head0, invalidate=dead)
        return seq0, head0


def plan(planner: Planner, now_ms: int) -> int:
    return planner.run(now_ms)


# -- sealing and dispatch ---------------------------------------------------------------------------------------------


class _Ctx(Protocol):
    store: PushStore
    ob: OutboundStore
    keys: Any
    signer: Any
    view: Any
    standing: Standing
    alarm: Callable[[str, str], None]


def _audience(ctx: _Ctx, it: Intent) -> list[str]:
    members = ctx.view.member_list()
    return devices_for(
        ctx.store.state, members,
        PushContext(it.ticket, it.narrow, it.exclude), standing=ctx.standing,
    )  # fmt: skip


def seal_step(ctx: _Ctx, now_ms: int) -> int:
    """Seal up to 32 ``planned`` intents (a worker job). Returns the number of rows handled (sealed, dropped or
    failed; 0 when none is planned or the snapshot is not sealable: the caller loops until 0). A snapshot that is not
    sealable (the rotation gate is up) leaves them ``planned``: the thread runs this again."""
    with ctx.store.locked():
        batch = ctx.ob.planned(SEAL_BATCH)
        if not batch:
            return 0
        auth_gen = ctx.store.auth_gen
        todo: list[tuple[Intent, list[str]]] = []
        for it in batch:
            if now_ms >= it.deadline_ms:
                ctx.ob.drop(it.key, "deadline")
                continue
            targets = _audience(ctx, it)
            if not targets:
                ctx.ob.drop(it.key, "no_target")
                continue
            todo.append((it, targets))
        if not todo:
            return len(batch)  # all dropped: they were handled, the loop goes on to the next batch
        out: list[tuple[str, bytes, int, int, Sequence[str], int]] = []
        last: dict[str, int] = {}
        with ctx.keys.push_sealing(ctx.signer) as sealer:
            if not sealer.sealable:
                return 0
            for it, targets in todo:
                prev = last.get(it.id, ctx.ob.ts_last(it.id))
                ts = max(now_ms, (prev if prev is not None else -1) + 1)
                try:
                    outer, epoch = sealer.seal_push({"kind": it.kind, "id": it.id, "label": it.label, "ts_ms": ts})
                except (cpush.PushError, SealRefused, KeyMissing) as e:
                    ctx.ob.fail(it.key, "seal_input")
                    ctx.alarm("push_seal_failed", type(e).__name__)
                    continue
                last[it.id] = ts
                out.append((it.key, outer, epoch, ts, targets, auth_gen))
            ctx.ob.seal(out, now_ms)  # persisted inside the seal span, before any send (D6)
        return len(batch)  # every row of the batch left "planned" (sealed, dropped or failed)


def dispatch_step(ctx: _Ctx, key: str, now_ms: int, *, off_until_ms: int = 0) -> tuple[str, Any]:
    """The step before EVERY send attempt (a worker job): ``("send", (targets, ttl_s))``, ``("wait", next_ms)`` or
    ``("skip", reason)`` (the intent was dropped, or is no longer ``sealed``)."""
    with ctx.store.locked():
        it = ctx.ob.get(key)
        if it is None or it.state != "sealed" or it.outer is None:
            return "skip", "not_sealed"
        if now_ms >= it.deadline_ms:
            ctx.ob.drop(key, "deadline")
            return "skip", "deadline"
        if off_until_ms and now_ms < off_until_ms:
            return "wait", off_until_ms  # push is off at this relay: delayed (kept) until the next probe or deadline
        targets = _audience(ctx, it)
        if not targets:
            ctx.ob.drop(key, "no_target")
            return "skip", "no_target"
        if it.kind != "question.closed":
            # a closed push replaces a stale "New question" at once: exempt from both the per-id interval and the
            # per-device budget (and not counted in it); the interval applies between pushes of the SAME kind
            recent = ctx.ob.sent_since(now_ms - DEVICE_BUDGET_WINDOW_MS)
            again = max((t for i, k, t, _ in recent if i == it.id and k == it.kind), default=None)
            if again is not None and now_ms < again + ID_INTERVAL_MS:
                return "wait", again + ID_INTERVAL_MS
            used: dict[str, int] = {}
            for _, k, _, devices in recent:
                if k == "question.closed":
                    continue
                for d in devices:
                    used[d] = used.get(d, 0) + 1
            targets = [d for d in targets if used.get(d, 0) < DEVICE_BUDGET]
            if not targets:
                ctx.ob.drop(key, "over_budget")  # the phone reconciles when it opens
                return "skip", "over_budget"
        ctx.ob.record_targets(key, targets, ctx.store.auth_gen)
        return "send", (targets, max(1, min(TTL_S, (it.deadline_ms - now_ms) // 1000)))


# -- the push thread --------------------------------------------------------------------------------------------------


def _is_custody_refusal(e: BaseException) -> bool:
    """Key, seal and custody refusals: the push thread never retries them (a refused control stays refused). The R6
    key store's ``MembershipUnsafe`` is matched by name: only r6 imports that module (test_import_ban.py)."""
    from orch.wsproto.state import StateError

    named = type(e).__name__ == "MembershipUnsafe"
    return named or isinstance(e, (cpush.PushError, SealRefused, KeyMissing, StateError))


def _is_transient(e: BaseException) -> bool:
    """Known-transient failures: a database error (sqlite3, matched by module: only the stores open sqlite), a
    transport error, a worker-job timeout."""
    if type(e).__module__ == "sqlite3" and type(e).__name__ == "OperationalError":
        return True
    return isinstance(e, (TransportError, OSError, TimeoutError))


class PushThread:
    """The worker companion ``orch-relay-push-<id8>`` (``start``, ``stop``, ``join``, ``wake``). ``job(fn)`` runs
    ``fn`` on the workspace's worker thread. :meth:`step` is one pass (plan, seal, send what is due) and returns the
    seconds to wait; the thread loops it, and a test calls it with an injected clock."""

    def __init__(
        self,
        store: PushStore,
        *,
        ob: OutboundStore,
        job: Callable[[Callable[[], Any]], Any],
        session: Session,
        keys: Any,
        signer: Any,
        view: Any,
        standing: Standing,
        alarm: Callable[[str, str], None],
        conn: Callable[[], Any] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        id8: str | None = None,
        hints: HintSource | None = None,
    ) -> None:
        self.store, self.ob, self.keys, self.signer, self.view = store, ob, keys, signer, view
        self.standing, self.alarm = standing, alarm
        self._job, self._session, self._conn_factory, self._on_error, self._now = job, session, conn, on_error, now_ms
        self.id8 = id8 or session.workspace_id[:8]
        self.planner = Planner(store, ob, hints)
        self._conn: Any = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._woken = False  # a commit since the last planning pass (wake() sets it; step() consumes it)
        self._thread: threading.Thread | None = None
        self._closed = False
        self._last_plan_ms = 0
        self._off_until_ms = 0
        self._probe_ms = PROBE_FIRST_MS
        self._seen_gen: int | None = None
        self._pruned_ms = 0
        self.state = "on"  # "off_at_relay" after a 404
        self.error: BaseException | None = None

    # -- lifecycle ---------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("started twice")
        self._thread = threading.Thread(target=self._run, name=f"orch-relay-push-{self.id8}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        c = self._conn
        if c is not None:
            try:
                c.shutdown()
            except Exception:  # noqa: BLE001 - best effort
                pass

    def join(self, timeout: float) -> bool:
        t = self._thread
        if t is not None:
            t.join(max(0.0, timeout))
            if t.is_alive():
                log.error("relay_thread_stuck: %s", t.name)
                return False
        self._close()
        return True

    def close(self) -> None:
        """Close the outbox (``join`` does it once the thread ended; a start that failed does it directly)."""
        self._close()

    def _close(self) -> None:
        if not self._closed:
            self._closed = True
            self.ob.close()

    def reprobe(self) -> None:
        """The relay was (re)linked: probe push again at once (a relay that said 404 may have turned it on)."""
        self._off_until_ms, self._probe_ms = 0, PROBE_FIRST_MS
        self.ob.release_waiting("off_at_relay")
        self.wake()

    def wake(self) -> None:
        """The store committed an event: plan now, not at the next 30 s check."""
        self._woken = True
        self._wake.set()

    def push_status(self) -> dict[str, Any]:
        """For ``host.status.relay``: the push state and the intent counts. No label, id or key."""
        out: dict[str, Any] = {"state": self.state}
        try:
            out["intents"] = self.ob.counts()
        except Exception:  # noqa: BLE001 - a closed store
            pass
        return out

    def _run(self) -> None:
        from .worker import WorkerStopped

        try:
            while not self._stop.is_set() and self._session.gen is None:
                self._stop.wait(1.0)  # the start fence: nothing before this holder's own host/hold
            errors = 0
            while not self._stop.is_set():
                try:
                    wait = self.step()
                    errors = 0
                except (WorkerStopped, SessionError):  # Fenced is a SessionError: the session's path
                    raise
                except Exception as e:  # noqa: BLE001 - classified below: fail closed on a custody refusal
                    name = type(e).__name__
                    errors += 1
                    if _is_custody_refusal(e) or (not _is_transient(e) and errors >= UNKNOWN_FAILURES_MAX):
                        # (a) a key/seal/custody refusal, or (c) an unknown failure that kept happening: the PUSH thread
                        # ends (reads, writes and the relay worker keep running); a refused control is never retried
                        self.state = "stopped"
                        log.error("relay push thread stopped: %s", name)
                        self.alarm("push_stopped", name)
                        return
                    wait = min(BACKOFF_MAX_S, 2.0 ** min(errors, 10))
                    log.error("relay push step failed: %s", name)
                    self.alarm("push_error", name)
                self._wake.wait(wait)
                self._wake.clear()
        except WorkerStopped:
            pass
        except Exception as e:  # noqa: BLE001 - a fence or session error: the thread ends, R5 is told
            if not self._stop.is_set():
                self.error = e
                log.error("relay push thread stopped: %s", type(e).__name__)
                if self._on_error is not None:
                    self._on_error(e)
        finally:
            self._stop.set()
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    pass

    # -- one pass ----------------------------------------------------------------------------------------------------

    def step(self) -> float:
        """Plan (at least every 30 s, and when woken), seal, then send every intent that is due. Returns the seconds
        until the next thing to do."""
        now = self._now()
        if self._stop.is_set():
            return 1.0
        gen = self._session.gen
        if gen != self._seen_gen:
            if self._seen_gen is not None and self._off_until_ms:
                self.reprobe()  # a new link (take-over, relink): probe now
            self._seen_gen = gen
        if self._woken or now - self._last_plan_ms >= PLAN_EVERY_S * 1000 or self._last_plan_ms == 0:
            self._woken, self._last_plan_ms = False, now
            self._job(lambda: self.planner.run(self._now()))
        while self._job(lambda: seal_step(self, self._now())):
            pass  # until no planned row remains (a batch that was all dropped counts as handled)
        wait = PLAN_EVERY_S
        for it in self.ob.sealed():
            if self._stop.is_set():
                break
            now = self._now()
            if it.next_ms > now:
                wait = min(wait, (it.next_ms - now) / 1000)
                continue
            nxt = self._send(it)
            if nxt is not None:
                wait = min(wait, max(0.05, nxt))
        if self._now() - self._pruned_ms > 3_600_000:
            self._pruned_ms = self._now()
            self.ob.prune(self._now() - 24 * 3_600_000)
        return max(0.05, wait)

    # -- sending -----------------------------------------------------------------------------------------------------

    def _transport(self) -> Any:
        if self._conn is None and self._conn_factory is not None:
            self._conn = self._conn_factory()
        return self._conn

    def _send(self, it: Intent) -> float | None:
        """One attempt for a ``sealed`` intent, with the dispatch step first. Returns seconds to retry, or ``None``."""
        key = it.key
        kind, val = self._job(lambda: dispatch_step(self, key, self._now(), off_until_ms=self._off_until_ms))
        if kind == "skip":
            return None
        if kind == "wait":
            return max(0.05, (val - self._now()) / 1000)
        targets, ttl_s = val
        fresh = self.ob.get(key)
        if fresh is None or fresh.outer is None or fresh.state != "sealed":
            return None
        outer = canon.loads_strict(fresh.outer)  # the PERSISTED bytes, never rebuilt (D6)
        accepted = 0
        try:
            for i in range(0, len(targets), MAX_TARGETS):
                body = {"targets": targets[i : i + MAX_TARGETS], "outer": outer, "priority": it.priority,
                        "ttl_s": ttl_s}  # fmt: skip
                got = self._session.call(
                    "POST", f"/v2/workspaces/{self._session.workspace_id}/push", ROUTE, body=body, holder=True,
                    keys={"accepted"}, request_cap=64 * 1024, deadline_s=SEND_DEADLINE_S, transport=self._transport(),
                )  # fmt: skip
                n = got["accepted"]
                accepted += n if type(n) is int and n >= 0 else 0
        except Fenced:
            raise  # the worker's fenced path (11A.5): the loop stops, intents stay for the next start
        except RelayRefused as e:
            return self._refused(it, e)
        except NotHolder:
            return 1.0
        except (TransportError, RelayMisbehaving, SessionError) as e:
            return self._backoff(it, type(e).__name__, None)
        self.state, self._off_until_ms, self._probe_ms = "on", 0, PROBE_FIRST_MS
        self.ob.sent(key, accepted, self._now())
        return None

    def _backoff(self, it: Intent, reason: str, retry_after: int | None) -> float:
        delay = max(min(BACKOFF_MAX_S, 2.0 ** min(it.attempts, 10)), float(retry_after or 0))
        self.ob.retry(it.key, self._now() + int(delay * 1000), reason)
        return delay

    def _refused(self, it: Intent, e: RelayRefused) -> float | None:
        if e.status == 404:
            self.state = "off_at_relay"
            self._off_until_ms = self._now() + self._probe_ms
            self._probe_ms = min(self._probe_ms * 2, PROBE_EVERY_MS)
            self.ob.retry(it.key, self._off_until_ms, "off_at_relay")  # kept: delayed until the next probe
            log.info(
                "relay push is off at this relay (404); next probe in %d s", (self._off_until_ms - self._now()) // 1000
            )
            return max(0.05, (self._off_until_ms - self._now()) / 1000)
        if e.status == 409 and e.code == "stale_epoch":
            n = self.ob.reseal(it.key, "stale_epoch")
            if n is not None and n > MAX_RESEALS:
                self.ob.fail(it.key, "push_epoch_loop")
                self.alarm("push_epoch_loop", it.kind)
            return 0.05  # planned again: the next pass reseals under the new epoch
        if e.status == 400:
            self.ob.fail(it.key, "malformed")
            self.alarm("push_malformed", f"{it.kind} {e.code}")
            return None
        if e.status == 429 or e.status >= 500 or e.status == 401:
            return self._backoff(it, f"http_{e.status}", e.retry_after)
        self.ob.fail(it.key, f"http_{e.status}")
        self.alarm("push_refused", f"{e.status} {e.code}")
        return None
