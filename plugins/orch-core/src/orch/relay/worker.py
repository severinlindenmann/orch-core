"""R5: the relay worker of one workspace (contract 11A.4 "The hook", 11A.5 "For R3/R5/R6", 11A.7 "Shutdown").

:class:`RelayWorker` is what ``WorkspaceService`` starts and stops (the host side is ``orch/host/relayhook.py``;
``orch.host`` is import-banned here). Its contract is the one H12a built the service around: ``start()``,
``stop()`` and ``join(timeout) -> bool`` (true when every relay thread ended), plus :meth:`RelayWorker.submit`, the
only way a relay thread reaches the store (a wrapper of ``service.submit`` that refuses once :meth:`stop` began).

It owns, per workspace:

* ONE :class:`orch.wsproto.state.StateDir` and ONE :class:`orch.relay.session.Session` (11A.5 R7-4: the preflight pass
  and the sticky refusal are per Session object), shared by every relay thread;
* the R2 :class:`orch.relay.bridge.BridgeLoop` (threads ``orch-relay-{poll,resp,job0,job1,aux}-<id8>``);
* the R3 publisher behind the :class:`Publisher` seam (:class:`NoPublisher` until R3 lands) and the R4 consumer behind
  the :class:`Unpublished` seam (a step on the aux thread; nothing until R4 lands);
* one short-lived start thread ``orch-relay-start-<id8>``: the convenience check as a worker job (11A.6 "Foreign
  appends"), then the first link when ``link.json`` is still ``linking`` (11A.5, R1's :func:`first_link`), then the
  loop and the publisher. ``start()`` itself never waits for the workspace's worker thread: it runs under
  ``service.relay_lock`` (the starter rule of 11A.4 "As built in H12a").

**The R6 gate.** 11A.1: "``RelayWorker`` does not start the poll thread for a workspace until R6 is merged and its
integration tests against the real relay pass." :data:`R6_READY` is that gate, a code constant (``True`` since the
flip, 16 Oct 2026): the host checks it in ``relaylink.start_blocker`` (so ``may_start`` is false and
``host.status.relay`` names :data:`R6_GATE_REASON`), and :meth:`RelayWorker.start` checks it again (defence in depth)
before any thread exists.
What flipping it requires is listed in 11A.4 "As built in R5".

**Fork and exec.** The state directory's lock fd is close-on-exec and its fork hook closes a child's copy (R1); the
relay sockets are not inheritable (Python's default). A worker records its pid: in a forked child :meth:`submit`
and :meth:`start` refuse, :meth:`stop` does nothing and :meth:`join` never logs out or closes anything (the parent's
token and lock stay the parent's). The pid is checked BEFORE the worker's lock: a lock another parent thread held at
the fork stays locked in the child for ever, so the child never takes it.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from typing import Any, Protocol, runtime_checkable

from orch.wsproto.state import StateDir, StateError

from .bridge import BridgeLoop
from .directory import Directory
from .rids import RidStore
from .session import Session

__all__ = [
    "R6_GATE_REASON",
    "R6_READY",
    "Alarms",
    "LinkSource",
    "NoLinkSource",
    "NoPublisher",
    "Publisher",
    "RelayWorker",
    "SeamMissing",
    "Unpublished",
    "WorkerStopped",
    "check_seam",
    "r6_ready",
]

log = logging.getLogger("orch.relay.worker")

#: The R6 gate (11A.1): was False until R6 (pairing, membership, WK custody, the real ``EpochKeySource`` and ``R6View``)
#: was merged AND its integration tests against the real relay passed. Flipped to True on 16 Oct 2026, after B1 (#608),
#: per the 11A.4 "As built in R5" checklist (D:5780-5793); ``tests/relay/test_worker.py`` pins the literal.
R6_READY = True
R6_GATE_REASON = "r6_not_ready: the relay worker stays dormant until R6 is merged and integration-tested (11A.1)"


def r6_ready() -> bool:
    """THE gate check (every caller uses it): open only for the literal ``True``, never for another truthy value."""
    return R6_READY is True


#: open alarm codes kept per worker (each with a count); a new code past this is counted under one overflow code
MAX_ALARM_CODES = 64
ALARM_DETAIL_MAX = 200


class WorkerStopped(Exception):  # noqa: N818 - the worker's verdict, not an I/O error
    """:meth:`RelayWorker.submit` after :meth:`RelayWorker.stop` began (or in a forked child): no new store job."""


class SeamMissing(Exception):  # noqa: N818 - a dependency group not built yet
    """A seam whose group (R3, R4, R6) is not built: the step refuses; nothing is sent or written."""


@runtime_checkable
class Publisher(Protocol):
    """R3's seam (11A.5 "Publisher (R3)"): a thread ``orch-relay-pub-<id8>`` beside the loop, with the worker
    contract, and the first publication (with catch-up) that :func:`orch.relay.link.first_link` runs as
    ``LinkSteps.publish``. It reaches the store only through :meth:`RelayWorker.submit` (``job``), never under a lock
    of the worker: ``start()`` is called outside the worker's lock and must return without waiting for a job of the
    workspace's worker thread (its thread may submit jobs once running); ``join`` is bounded by its timeout."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def join(self, timeout: float) -> bool: ...

    def publish_first(self, session: Session, directory: Directory) -> None: ...


class NoPublisher:
    """Until R3: no publisher thread, and the first publication refuses (:class:`SeamMissing`), so a first link never
    reaches ``state: "linked"`` without one (fail closed: ``mark_linked`` follows ``publish`` in ``first_link``)."""

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def join(self, timeout: float) -> bool:
        return True

    def publish_first(self, session: Session, directory: Directory) -> None:
        raise SeamMissing("R3 (the checkpoint publisher) is not built: the first publication cannot run")


@runtime_checkable
class Unpublished(Protocol):
    """R4's seam (11A.9 "Unpublished evidence"): one bounded step, run on the aux thread after the lease renewal
    (10 s total deadline per relay call); it holds evidence only through ``job``
    (``Store.note_forwarded_revocation``)."""

    def step(self) -> None: ...


@runtime_checkable
class LinkSource(Protocol):
    """What the first link needs from R6a and the person path (11A.5 steps 2 and 3; conflict C6): the card (WXK and
    CK custody), the owner-signed genesis pin with its certificate, and the owner's ``PK`` from the genesis
    delegation (R1's directory checks the card and the pin under it)."""

    def card(self) -> Mapping[str, Any]: ...

    def genesis(self) -> tuple[Mapping[str, Any], Mapping[str, Any], str]: ...

    def owner_pk_pub(self) -> bytes: ...


class NoLinkSource:
    """Until R6a and the genesis-pin person path: every step refuses (:class:`SeamMissing`) before anything is sent."""

    def card(self) -> Mapping[str, Any]:
        raise SeamMissing("R6a (card keys) is not built: the card cannot be made")

    def genesis(self) -> tuple[Mapping[str, Any], Mapping[str, Any], str]:
        raise SeamMissing("the genesis pin's person path (relay.genesis_pin) is not built")

    def owner_pk_pub(self) -> bytes:
        raise SeamMissing("the first link's owner key source is not built")


def printable(text: str) -> str:
    """Alarm text without control characters (it reaches ``host.status`` and logs)."""
    return "".join(c if c.isprintable() else "?" for c in text)


def check_seam(obj: Any, kind: type, name: str) -> Any:
    """A registered seam object must conform to its protocol (its methods exist); else :class:`SeamMissing` and the
    worker does not start."""
    if not isinstance(obj, kind):
        raise SeamMissing(f"the {name} seam does not provide the {kind.__name__} methods")
    return obj


class Alarms:
    """The worker's open alarms (11A.3 ``alarms.json``, shown in ``host.status.relay``): per code a count, the first
    and last time and the last detail (clipped; callers pass classes and routes, never secrets). At most
    :data:`MAX_ALARM_CODES` codes; ``persist`` (the state directory's atomic write) runs when a code first appears."""

    def __init__(self, *, now_ms: Callable[[], int], persist: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._now_ms, self._persist = now_ms, persist
        self._lock = threading.Lock()
        self._open: dict[str, dict[str, Any]] = {}

    def __call__(self, code: str, detail: str) -> None:
        code = printable(str(code)[:64])
        detail = printable(str(detail)[:ALARM_DETAIL_MAX])
        now = self._now_ms()
        with self._lock:
            row = self._open.get(code)
            new = row is None
            if new and len(self._open) >= MAX_ALARM_CODES:
                code, new = "relay_alarm_codes_exhausted", "relay_alarm_codes_exhausted" not in self._open
                row = self._open.get(code)
            if row is None:
                row = self._open[code] = {"code": code, "count": 0, "first_ms": now, "last_ms": now, "detail": ""}
            row["count"] += 1
            row["last_ms"], row["detail"] = now, detail
            snapshot = self._snapshot_locked() if new else None
        log.warning("relay alarm %s: %s", code, detail)
        if snapshot is not None:
            self.save(snapshot)

    def _snapshot_locked(self) -> dict[str, Any]:
        return {"v": 1, "alarms": [dict(r) for r in self._open.values()]}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def codes(self) -> list[str]:
        with self._lock:
            return list(self._open)

    def save(self, snapshot: dict[str, Any] | None = None) -> None:
        if self._persist is None:
            return
        try:
            self._persist(snapshot if snapshot is not None else self.snapshot())
        except (OSError, StateError, ValueError) as e:  # an alarm is never lost to a write error: it stays in memory
            log.error("relay alarms could not be written: %s", type(e).__name__)


#: the methods each seam protocol requires (the host checks that each is callable: ``orch.relay`` has no getattr)
SEAM_METHODS: dict[type, tuple[str, ...]] = {
    Publisher: ("start", "stop", "join", "publish_first"),
    Unpublished: ("step",),
    LinkSource: ("card", "genesis", "owner_pk_pub"),
}


class RelayWorker:
    """One workspace's relay worker. Build it, :meth:`attach` the loop (whose ``Bridge.job`` is :meth:`job`), then
    :meth:`start`. ``submit(fn) -> Future`` is the workspace's own ``service.submit``; ``precheck()`` (run as a
    worker job on the start thread) returns ``None`` or the alarm code that keeps the worker from serving (11A.6:
    ``convenience_events_present``); ``first_link()`` runs R1's first link when ``link_state()`` is ``"linking"``;
    ``on_ended()`` runs once, when every thread ended (the host's slot of the 8-per-host cap)."""

    def __init__(
        self,
        *,
        workspace_id: str,
        state: StateDir,
        session: Session,
        submit: Callable[[Callable[[], Any]], Future[Any]],
        alarms: Alarms,
        stop_event: threading.Event,
        link_state: Callable[[], str | None],
        precheck: Callable[[], str | None] = lambda: None,
        first_link: Callable[[], None] = lambda: None,
        publisher: Publisher | None = None,
        unpublished: Unpublished | None = None,
        on_ended: Callable[[], None] = lambda: None,
        mono: Callable[[], float] = time.monotonic,
        conn: Callable[[], Any] | None = None,
    ) -> None:
        self.workspace_id, self.id8 = workspace_id, workspace_id[:8]
        #: the connection factory of this link (one transport per thread, 11A.7): the seam factories of the host
        #: (R3's publisher, R6b's membership thread) build their own connection through it (B1)
        self.conn = conn
        #: threads that live and die with the worker beside the publisher (R6b's membership thread): objects with
        #: ``start()``, ``stop()`` and ``join(timeout) -> bool``; added before :meth:`start`
        self.companions: list[Any] = []
        self.state, self.session, self.alarm = state, session, alarms
        self._submit, self._stop_event = submit, stop_event
        self._link_state, self._precheck, self._first_link = link_state, precheck, first_link
        self.publisher: Publisher = publisher if publisher is not None else NoPublisher()
        self.unpublished = unpublished
        self._on_ended, self._mono = on_ended, mono
        self._lock = threading.Lock()
        self._pid = os.getpid()
        self._stopping = False
        self._started = False
        self._running = False  # the loop and the publisher were started
        self._ended = False
        self._reaping = False
        self._finish_lock = threading.Lock()
        #: the reaper's join bound and its pause between rounds (instance attributes so a test can shorten them)
        self.REAP_JOIN_S, self.REAP_PAUSE_S = 60.0, 1.0
        self.loop: BridgeLoop | None = None
        self.rids: RidStore | None = None
        self._start_thread: threading.Thread | None = None
        #: why the worker stopped by itself (a class name or an alarm code; never a message with relay bytes)
        self.error: str | None = None

    # -- the store seam ----------------------------------------------------------------------------------------------

    def submit(self, fn: Callable[[], Any]) -> Future[Any]:
        """``service.submit(fn)``, refused (:class:`WorkerStopped`) once :meth:`stop` began or in a forked child: no
        relay thread queues a store job after the stop (11A.7 step 1)."""
        if os.getpid() != self._pid:  # before the lock (a parent thread may have held it at the fork)
            raise WorkerStopped("a forked child never uses the parent's worker")
        with self._lock:
            if self._stopping:
                raise WorkerStopped("the relay worker is stopping")
            return self._submit(fn)

    def job(self, fn: Callable[[], Any]) -> Any:
        """R2's ``job``: :meth:`submit` and wait for the result (11A.5 "For R3/R5/R6")."""
        return self.submit(fn).result()

    # -- lifecycle ---------------------------------------------------------------------------------------------------

    def attach(self, loop: BridgeLoop, rids: RidStore) -> None:
        if self.loop is not None:
            raise RuntimeError("the loop is attached once")
        self.loop, self.rids = loop, rids

    @property
    def stopping(self) -> bool:
        return self._stopping

    def start(self) -> None:
        """Check the R6 gate, then hand off to the start thread. Never blocks on the workspace's worker thread."""
        if not r6_ready():
            raise SeamMissing(R6_GATE_REASON)
        if os.getpid() != self._pid:
            raise WorkerStopped("a forked child never starts the parent's worker")
        with self._lock:
            if self._started:
                raise RuntimeError("started twice")
            if self._stopping:
                raise WorkerStopped("the relay worker was stopped before it started")
            if self.loop is None:
                raise RuntimeError("no loop attached")
            self._started = True
            t = threading.Thread(target=self._start_main, name=f"orch-relay-start-{self.id8}", daemon=True)
            self._start_thread = t
        t.start()

    def _start_main(self) -> None:
        try:
            why = self.job(self._precheck)
            if why is not None:
                self._fail(str(why), why)
                return
            if self._link_state() == "linking":
                self._first_link()
                # the first link can take long: the history is checked again right before anything is served; a
                # foreign append after this job is caught by the service's audit (the worker is service.relay by then)
                why = self.job(self._precheck)
                if why is not None:
                    self._fail(str(why), why)
                    return
            with self._lock:
                if self._stopping:
                    return
                assert self.loop is not None
                if self.unpublished is not None:
                    self.loop.aux_steps.append(("unpublished", self.unpublished.step))
                self.loop.start()
                self._running = True
            # outside the worker's lock: a publisher whose start submits a job cannot deadlock the start thread
            self.publisher.start()
            if self._stopping:  # a stop between the lock and here: tell it (again; stop is idempotent)
                self.publisher.stop()
            for companion in self.companions:
                companion.start()
                if self._stopping:
                    companion.stop()
        except WorkerStopped:
            return
        except Exception as e:  # noqa: BLE001 - the start failed: the worker stops, by class only
            self._fail("relay_start_failed", type(e).__name__)
        except BaseException as e:  # R2's WorkerStop and the like (N4-1): stop, then never swallow it
            self._fail("relay_start_failed", type(e).__name__)
            raise

    def _fail(self, code: str, detail: str) -> None:
        if not self._stopping:
            self.error = code if code != "relay_start_failed" else f"{code}: {detail}"
            self.alarm(code, detail)
        self.stop()

    def stop(self) -> None:
        """11A.7 shutdown, without blocking: from now on :meth:`submit` refuses; the stop event wakes every wait; the
        loop shuts its connections down and wakes its queues; the publisher is told to stop. Idempotent; nothing in a
        forked child (the threads and sockets are the parent's)."""
        if os.getpid() != self._pid:
            return
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            running = self._running
        self._stop_event.set()
        if self.loop is not None:
            self.loop.stop()
        if running:
            self.publisher.stop()
        for companion in self.companions:
            companion.stop()

    def join(self, timeout: float) -> bool:
        """True when every relay thread ended within ``timeout`` (shared): the start thread, the loop (joined twice:
        threads the poll thread spawned after a first snapshot are caught by the second) and the publisher. Once all
        ended (in the owning process only): a best-effort logout, then the rid store and the state directory are
        closed (the lock is released for the next worker) and ``on_ended`` runs, once. When a thread outlives the
        bound, a reaper thread ``orch-relay-reap-<id8>`` waits for the rest and then releases them, so no caller has
        to join again and no lock holder ever runs the logout's network I/O (review Low 3)."""
        if os.getpid() != self._pid:
            return True  # a forked child has none of the parent's threads; it releases nothing
        ended = self._join_all(timeout)
        if ended:
            self._finish()
        else:
            self._reap()
        return ended

    def _join_all(self, timeout: float) -> bool:
        end = self._mono() + timeout

        def left() -> float:
            return max(0.0, end - self._mono())

        t = self._start_thread
        if t is not None:
            t.join(left())
        ended = t is None or not t.is_alive()
        if self.loop is not None:
            ended = self.loop.join(left()) and ended
            ended = self.loop.join(left()) and ended
        ended = self.publisher.join(left()) and ended
        for companion in self.companions:
            ended = companion.join(left()) and ended
        return ended

    def _reap(self) -> None:
        with self._lock:
            if self._reaping or self._ended:
                return
            self._reaping = True

        def reap() -> None:
            # a join seam that returns false at once must not make this spin: wait between rounds (review Low 1)
            while not self._join_all(self.REAP_JOIN_S):
                time.sleep(self.REAP_PAUSE_S)
            self._finish()

        threading.Thread(target=reap, name=f"orch-relay-reap-{self.id8}", daemon=True).start()

    def alive(self) -> bool:
        """Whether this worker still holds anything (``host.status.relay``, the service's stuck check): a relay thread
        runs, or it stopped but has not yet released its directory and slot. No I/O, no join."""
        if self._ended:
            return False
        t = self._start_thread
        if t is not None and t.is_alive():
            return True
        if self.loop is not None and any(th.is_alive() for th in self.loop.threads()):
            return True
        return self._stopping

    def _finish(self) -> None:
        """Release, once: the logout, the rid store, the state directory, ``on_ended``. Serialised by its own lock
        (never the worker's): the reaper and a later ``join`` may both get here, and the second returns only once the
        first has released everything; ``_ended`` is set last, so ``alive()`` stays true until then."""
        if os.getpid() != self._pid:
            return  # a forked child: the token, the database and the lock are the parent's
        with self._finish_lock:
            if self._ended or not self._stopping:
                return
            try:
                if self.loop is not None and self.loop.error is not None and self.error is None:
                    self.error = type(self.loop.error).__name__
                # each step on its own (round 5 L4): one that raises is alarmed and the next still runs, so a failed
                # rid-store close never leaks the state directory's lock
                for step, fn in (
                    ("alarms", self.alarm.save),
                    ("logout", self.session.logout),
                    ("rids", self.rids.close if self.rids is not None else (lambda: None)),
                    ("state", self.state.close),
                ):
                    try:
                        fn()
                    except Exception as e:  # noqa: BLE001 - alarmed by step and class, then the next step
                        if step == "logout":
                            log.info("relay logout failed (ignored)")  # best effort (11A.7 step 3)
                        else:
                            self.alarm("relay_release_failed", f"{step}: {type(e).__name__}")
            finally:
                try:
                    self._on_ended()
                except Exception:  # noqa: BLE001 - the host's bookkeeping never keeps a stopped worker alive
                    log.exception("relay worker of %s: on_ended failed", self.workspace_id)
                self._ended = True

    def status(self) -> dict[str, Any]:
        """For ``host.status.relay``: no secret, no relay bytes. ``serving`` only while the loop runs without an
        error; a loop that stopped itself (``fenced``, a lost session: R2 stops its own threads) shows its error
        class here, and the worker stays in place until ``orch host relay stop`` (then ``start``)."""
        loop_error = self.loop.error if self.loop is not None else None
        alive = self.alive()
        error = self.error or (type(loop_error).__name__ if loop_error is not None else None)
        return {
            "alive": alive,
            "stopping": self._stopping,
            "serving": self._running and not self._stopping and loop_error is None and alive,
            "error": error,
            "gen": self.session.gen,
            "fenced": self.session.fenced,
            "alarms": self.alarm.snapshot()["alarms"],
        }
