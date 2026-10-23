"""R3: the checkpoint publisher (contract 11A.5 "Publisher (R3)", "Maximum publication size", "Startup
classification"; 11A.3 ``publish.json``/``published.jsonl``; §11 the outbox; proto §19.8 P3-D20/D37).

**What it publishes, in order.** The relay's held checkpoint H is read through R1's verified reader on every step and
classified against the host (:meth:`Publisher.classify`). Then, oldest first:

1. a republication across an accepted restore the relay lost (H older than the restore): the plain extension from H
   to the restore's source C (if needed), then the stored restore body (``published-restore-<n>.json``);
2. the lowest outbox entry (:meth:`Store.outbox_first`; R3 stages only that one, gaps in ``n`` are normal):
   * plain, ``n`` at or below H: the final PUT alone (the relay answers ``ignored``; the same checkpoint is already
     held: nothing is sent);
   * plain, above H: the set from H to it, if it is an extension of H and fits the cap C; otherwise catch-up
     (``Store.mint_catchup(source=H, cap=0.9 C)``, which supersedes the plain entries) and plan again;
   * a restore: only from its own source (H == C, P3-D13 R2; an H behind C first gets C republished), never split:
     a set above C is ``relay.too_large``, alarmed and never sent;
3. with an empty outbox, a relay H that is not the newest local checkpoint (the first link, a relay rollback): catch-up
   from H.

**One set per publication.** Pages ``PUT staging/{source}/{target}/pages/{i}``, the manifest, then ``PUT checkpoint
{checkpoint, source_hash[, restore]}``. Exactly-once resume: the manifest (every page digest) is computed first, from
the logs one page at a time (at most one page plus the manifest in memory), every fold checked against the signed
target, and recorded durably (``publish-manifest.json``, then ``publish.json``: the commit point) **before** the first
PUT of the set. A resume PUTs the identical manifest (an identical PUT is a free no-op) and only the pages the relay
reports missing, each rebuilt and compared with its recorded digest before it is sent.

**The outbox protocol.** An entry is deleted only after the relay's acknowledgement is verified: a 200 whose
``result`` is ``accepted`` or ``ignored`` with exactly the entry's ``{n, hash}``, confirmed by a fresh verified read of
H (the target itself after ``accepted``; H at or above it after ``ignored``); then the publication history is
appended (``published.jsonl``, plus ``published-restore-<n>.json`` or ``published-last.json``), and only then
``Store.outbox_delete(n, hash)``. A crash between any two of these repeats the idempotent remainder.

**Size (P3-D37).** C = min(``link.json`` ``max_publication_bytes``, ``relay-info`` ``max_publication_bytes``, the
relay's 64 MiB staging quota, any in-memory lowering); the host never stages more than C. The exact counter
(:func:`orch.wsproto.wire.set_bytes`) is what ``mint_catchup`` measures. An unexpected ``too_large`` lowers C to
``min(C, refused/2)`` (at most 4 times per hour, then ``relay_too_large`` until the operator corrects ``link.json``).

**Threads.** :class:`Publisher` is R5's ``-pub-`` thread with its own connection: ``start()``, ``stop()``,
``join(timeout) -> bool``. Every store call is one worker job (``job(fn)``, R5's ``RelayWorker.submit(fn).result()``),
one at a time, never across networking; ``mint_catchup``'s ``measure`` is a pure counter. Nothing is sent before the
session holds a generation (the start fence of R3: no write before the holder's own ``host/hold``). A fence or
session error stops the publisher (``error``, ``on_error``); everything else is alarmed and backed off.
:meth:`Publisher.publish_first` (alias ``link_publish``) is R5's seam and ``LinkSteps.publish`` for R1's first link.
"""

from __future__ import annotations

import collections
import logging
import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, NoReturn, Protocol, TypeVar

from orch import canon
from orch.net.http import RedirectRefused, Transport, TransportError
from orch.ops import readers
from orch.store.errors import StoreError
from orch.store.outbox import OutboxEntry, RelayPosition, Segment
from orch.wsproto import wire
from orch.wsproto.state import StateDir, StateError

from .directory import Directory, VerifiedCheckpoint
from .retry import PERMANENT_QUOTAS, RelayRefused, delay
from .session import Fenced, LoginHeldOff, NotHolder, RelayMisbehaving, Session, SessionError

__all__ = [
    "CATCHUP_SHARE",
    "FINAL_CAP",
    "History",
    "PublishHeld",
    "PublishStore",
    "Publisher",
    "measure",
]

log = logging.getLogger("orch.relay.publish")

T = TypeVar("T")
Job = Callable[[Callable[[], T]], T]

STAGE_CAP = 64 << 20  # the relay's per-workspace staging quota: no relay accepts a larger set (P3-D20)
FINAL_CAP = 2 * 1024 * 1024 + 524_288 + 65_536  # the final PUT request cap (2 686 976 bytes, P3-D20)
LINE_CAP = 524_288  # a restore line, cj plus its LF
CATCHUP_SHARE = (9, 10)  # catch-up plans each step under 0.9 C (11A.5)
STAGE_DEADLINE_S = 120.0  # one page or manifest PUT (11A.7)
SMALL_DEADLINE_S = 30.0
STAGE_RESPONSE_CAP = 16 * 1024
# 11A.7 lists 16 KiB for the staging and final answers; the relay's manifest answer lists every missing index and a
# restore's final answer may carry its unpublished refs, so both get 1 MiB (judgement call, 11A.5 "As built in R3")
LIST_RESPONSE_CAP = 1 << 20
INFO_CAP = 4 * 1024
LOWERINGS_PER_HOUR = 4
CLOCK_SKEW_AFTER = 3
QUOTA_RETRIES = 5
QUOTA_WAIT_S = 3600.0
HELD_RECHECK_S = 3600.0
STAGING_EXPIRY_S = 3600.0  # a staged set lives one hour from its creation (P3-D20)
MANIFEST_ROUNDS = 3
INCOMPLETE_LIMIT = 3
START_POLL_S = 0.5
IDLE_POLL_S = 2.0  # an idle publisher checks the outbox locally this often (R5's wake() is immediate)
IDLE_RECHECK_S = 60.0  # and reads H from the relay again at least this often
RENEW_EVERY_S = 10.0  # publish_first renews the 40 s lease this often (review R2; the aux thread does it once running)
RENEW_RETRY_S = 2.0  # and retries this soon after a failed renewal: 10 + 10 (deadline) + 2 + 10 < 40 (review delta F2)
MIN_CAP = 4 * 1024  # an in-memory lowering never goes below this: below it the publication is held (review R6)
MISMATCH_LIMIT = 2  # plain restore_source_mismatch answers in a row before relay_misbehaving (review R4)
HISTORY_SEGMENT = 1024  # lines per published.jsonl segment; older segments are kept as published-<k>.jsonl
RECORD_LIMIT = 8 << 20  # published-restore-<n>.json and publish-manifest.json (StateDir.BIG_FILE)
ZERO = "sha256:" + "0" * 64
EMPTY: dict[str, Any] = {"seq": 0, "head": None, "acc": ZERO}


class PublishHeld(Exception):
    """Publication cannot proceed without a change elsewhere (the owner, the operator, the relay): alarmed with
    ``code``; R3 checks again after :data:`HELD_RECHECK_S`. Never carries relay text."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class _Again(Exception):  # noqa: N818 - internal: the plan changed (catch-up minted, C lowered); plan again now
    pass


class PublishStore(Protocol):
    """The store API R3 uses (H12b plus R3's reads), each call one worker job under the store lock."""

    @property
    def genesis(self) -> str | None: ...

    def outbox_first(self) -> OutboxEntry | None: ...

    def outbox_entries(self, limit: int | None = None) -> list[OutboxEntry]: ...

    def outbox_delete(self, n: int, hash: str) -> bool: ...

    def mint_catchup(
        self, *, source: Mapping[str, Any] | None, cap: int, measure: Callable[[tuple[Segment, ...]], int]
    ) -> list[OutboxEntry]: ...

    def relay_segment(
        self,
        log: str,
        after: Mapping[str, Any],
        upto: Mapping[str, Any],
        *,
        skip: int = 0,
        limit: int | None = None,
    ) -> Segment: ...

    def relay_position(self, checkpoint: Mapping[str, Any] | None) -> RelayPosition: ...

    def relay_ancestor(self, older: Mapping[str, Any], newer: Mapping[str, Any]) -> bool: ...


def measure(segments: tuple[Segment, ...]) -> int:
    """R3's exact counter for ``Store.mint_catchup``: the staged bytes of the step set ``segments`` (appended pages
    plus the manifest), from the layout alone. Pure: it never calls the store."""
    return wire.set_bytes(wire.plan_pages([("appended", s.log, s.after_seq, len(s.heads)) for s in segments]))


def _entries(cp: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if cp is None:
        return {}
    o = cp["o"]
    return {"workspace": o["workspace_log"], **o["tickets"]}


def _ref(cp: Mapping[str, Any] | None) -> str | None:
    return None if cp is None else canon.checkpoint_hash(cp["o"])


@dataclass(frozen=True)
class _Run:
    """One contiguous part of a set: ``kind`` pages of ``log`` from ``after`` (``{seq, acc[, head]}``) to ``upto``
    (``{seq, head, acc}``); ``heads`` from the restore evidence, or None (read from the store page by page)."""

    kind: str
    log: str
    after: dict[str, Any]
    upto: dict[str, Any]
    heads: tuple[str, ...] | None = None

    @property
    def count(self) -> int:
        return int(self.upto["seq"]) - int(self.after["seq"])


@dataclass
class _Pub:
    """One planned publication."""

    kind: str  # "plain" | "restore"
    target: dict[str, Any]
    n: int
    hash: str
    source: VerifiedCheckpoint | None
    runs: list[_Run] = field(default_factory=list)
    restore: dict[str, Any] | None = None  # the final body's restore part
    entry: OutboxEntry | None = None  # deleted after acceptance
    direct: bool = False  # n at or below H: the final PUT alone, no set
    record_source: dict[str, Any] | None = None  # a restore's C, kept with its history record
    acc_from: str | None = None


# -- the publication history (11A.3) -------------------------------------------------------------------------------


class History:
    """``published.jsonl`` and its records, append-only, in the flat relay state directory (judgement call: StateDir
    is one flat directory, so ``published/restore-<n>.json`` is ``published-restore-<n>.json`` and
    ``published/last.json`` is ``published-last.json``). A line ``{n, hash, kind, at_ms}`` per accepted publication;
    every :data:`HISTORY_SEGMENT` lines the segment is kept as ``published-<k>.jsonl`` (never deleted) and a new one
    starts.
    Each restore's record ``{body, source, acc_from}`` is kept forever; ``published-last.json`` is the last accepted
    plain checkpoint. The record is written before the line: the line is the commit point."""

    CURRENT = "published.jsonl"
    LAST = "published-last.json"

    def __init__(self, state: StateDir) -> None:
        self._s = state
        self._count: int | None = None  # archived segments, probed once per process (review R5)
        self._restores: list[dict[str, Any]] | None = None  # every accepted restore's record, loaded once

    @staticmethod
    def _parse(raw: bytes | None, name: str) -> list[dict[str, Any]]:
        if raw is None:
            return []
        out = []
        for line in raw.splitlines():
            try:
                obj = wire.loads_canonical(line)
            except wire.WireError:
                raise StateError(f"{name}: unreadable line") from None
            if (
                type(obj) is not dict
                or set(obj) != {"n", "hash", "kind", "at_ms"}
                or type(obj["n"]) is not int
                or obj["kind"] not in ("plain", "restore")
            ):
                raise StateError(f"{name}: unreadable line")
            out.append(obj)
        return out

    @staticmethod
    def _segment(k: int) -> str:
        return f"published-{k:06d}.jsonl"

    def _segments(self) -> list[str]:
        if self._count is None:
            k = 0
            while self._s.read(self._segment(k + 1)) is not None:
                k += 1
            self._count = k
        return [self._segment(k) for k in range(1, self._count + 1)]

    def lines(self) -> list[dict[str, Any]]:
        """Every line, oldest first (archived segments, then the current one). Reads every segment: only for the
        rare explanation of an H the logs do not contain, and for tests; a step uses :meth:`last` and
        :meth:`restores`, which are bounded (review R5)."""
        out: list[dict[str, Any]] = []
        for name in [*self._segments(), self.CURRENT]:
            out += self._parse(self._s.read(name), name)
        return out

    def last(self) -> dict[str, Any] | None:
        cur = self._parse(self._s.read(self.CURRENT), self.CURRENT)
        if cur:
            return cur[-1]
        segs = self._segments()
        old = self._parse(self._s.read(segs[-1]), segs[-1]) if segs else []
        return old[-1] if old else None

    def contains(self, hash: str) -> bool:
        return any(line["hash"] == hash for line in self.lines())

    def _record(self, n: int, h: str) -> dict[str, Any]:
        raw = self._s.read(f"published-restore-{n}.json", limit=RECORD_LIMIT)
        try:
            rec = wire.loads_body(raw or b"", cap=RECORD_LIMIT)
        except wire.WireError:
            raise StateError(f"the record of accepted restore {n} is missing or unreadable") from None
        if type(rec) is not dict or set(rec) != {"body", "source", "acc_from"}:
            raise StateError(f"the record of accepted restore {n} is malformed")
        return {"n": n, "hash": h, **rec}

    def restores(self) -> list[dict[str, Any]]:
        """Every accepted restore's record, by ``n``: ``{n, hash, body, source, acc_from}``. Scanned once per process
        and kept up to date by :meth:`append`. A restore line whose record is missing or unreadable is a
        :class:`StateError` (no degrade, no guess)."""
        if self._restores is None:
            out, seen = [], set()
            for line in self.lines():
                if line["kind"] == "restore" and line["n"] not in seen:
                    seen.add(line["n"])
                    out.append(self._record(line["n"], line["hash"]))
            self._restores = sorted(out, key=lambda r: r["n"])
        return list(self._restores)

    def append(
        self,
        *,
        n: int,
        hash: str,
        kind: str,
        at_ms: int,
        target: Mapping[str, Any],
        body: Mapping[str, Any] | None = None,
        source: Mapping[str, Any] | None = None,
        acc_from: str | None = None,
    ) -> None:
        if kind == "restore":
            rec = {"body": dict(body or {}), "source": source, "acc_from": acc_from}
            self._s.write(f"published-restore-{n}.json", canon.dumps(rec), limit=RECORD_LIMIT)
        else:
            self._s.write(self.LAST, canon.dumps(dict(target)), limit=RECORD_LIMIT)
        restores = self.restores()  # loaded before the line: a new restore is added below, once
        with self._s.lock:
            cur = self._s.read(self.CURRENT) or b""
            lines = self._parse(cur, self.CURRENT)
            if lines and lines[-1]["n"] == n and lines[-1]["hash"] == hash:
                return  # a repeat after a crash between the line and the outbox delete
            line = canon.dumps({"n": n, "hash": hash, "kind": kind, "at_ms": at_ms}) + b"\n"
            if len(lines) >= HISTORY_SEGMENT:
                segs = self._segments()
                # idempotent (review R10): a crash after the claim and before the rewrite left the same lines in both
                if not segs or self._s.read(segs[-1]) != cur:
                    name = self._segment(len(segs) + 1)
                    if not self._s.claim(name, cur) and self._s.read(name) != cur:
                        raise StateError(f"{name} exists with other lines")
                    self._count = len(segs) + 1
                cur = b""
            self._s.write(self.CURRENT, cur + line)
        if kind == "restore" and all(r["n"] != n for r in restores):
            self._restores = sorted([*restores, self._record(n, hash)], key=lambda r: r["n"])


# -- the staging journal (11A.3 publish.json) ------------------------------------------------------------------------


class _Journal:
    """``publish-manifest.json`` (the manifest bytes, every page digest) and ``publish.json`` (the commit point:
    ``{v, n, hash, kind, source_hash, cap, size, manifest_digest}``). Judgement call: the design's per-page
    ``pages:[{i, digest, count, put}]`` is the manifest itself, recorded whole before the first PUT; which pages are
    already staged is asked of the relay (the manifest answer's ``missing``), never assumed."""

    HEAD = "publish.json"
    MANIFEST = "publish-manifest.json"

    def __init__(self, state: StateDir) -> None:
        self._s = state

    def load(self, pub: _Pub, source_hash: str | None, size: int) -> bytes | None:
        try:
            head = self._s.read_json(self.HEAD)
            if head is None:
                return None
            raw = self._s.read(self.MANIFEST, limit=RECORD_LIMIT)
        except StateError:
            self.clear()
            return None
        want = {"v": 1, "n": pub.n, "hash": pub.hash, "kind": pub.kind, "source_hash": source_hash, "size": size}
        if (
            type(head) is not dict
            or raw is None
            or {k: head.get(k) for k in want} != want
            or head.get("manifest_digest") != _digest(raw)
        ):
            self.clear()
            return None
        return raw

    def save(self, pub: _Pub, source_hash: str | None, size: int, cap: int, manifest: bytes) -> None:
        self.clear()
        self._s.write(self.MANIFEST, manifest, limit=RECORD_LIMIT)
        head = {"v": 1, "n": pub.n, "hash": pub.hash, "kind": pub.kind, "cap": cap, "source_hash": source_hash,
                "size": size, "manifest_digest": _digest(manifest)}  # fmt: skip
        self._s.write_json(self.HEAD, head)

    def clear(self) -> None:
        self._s.remove(self.HEAD)  # the commit point goes first
        self._s.remove(self.MANIFEST)


def _digest(data: bytes) -> str:
    return wire.sha256_ref(data)


# -- the publisher -------------------------------------------------------------------------------------------------


class Publisher:
    """R3 for one workspace. ``job(fn)`` runs ``fn`` on the workspace's worker thread (R5: ``RelayWorker.submit(fn)
    .result()``); ``conn()`` makes the publisher thread's own connection (11A.7); ``max_publication_bytes`` is
    ``link.json``'s C; ``alarm(code, detail)`` raises a host alarm; ``on_diverged(H)`` is R5's degrade hook for an
    unexplained fork (D-P2-12: the store refuses appends until an owner restore); ``on_error(e)`` is told when a fence
    or session error stops the publisher."""

    def __init__(
        self,
        store: PublishStore,
        *,
        job: Job,
        session: Session,
        owner_pk_pub: bytes,
        wsk_pub: bytes,
        state: StateDir,
        max_publication_bytes: int,
        alarm: Callable[[str, str], None],
        conn: Callable[[], Transport] | None = None,
        on_diverged: Callable[[VerifiedCheckpoint], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        relay_level: Callable[[], int | None] | None = None,
        now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        mono: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        id8: str | None = None,
    ) -> None:
        if type(max_publication_bytes) is not int or max_publication_bytes < 1:
            raise ValueError("max_publication_bytes is a positive integer")
        self._store, self._job, self._session, self._state = store, job, session, state
        self._owner, self._wsk = owner_pk_pub, wsk_pub
        self._link_cap = max_publication_bytes
        self._alarm_cb, self._on_diverged, self._on_error = alarm, on_diverged, on_error
        self._relay_level = relay_level
        self._now_ms, self._mono, self._rng = now_ms, mono, rng or random.SystemRandom()
        self._conn_factory = conn
        self._conn: Transport | None = None
        self._dir: Directory | None = None
        self.id8 = id8 or session.workspace_id[:8]
        self.history = History(state)
        self._journal = _Journal(state)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._info_read = False
        self._info_cap: int | None = None
        self.reader_level = 0
        self._lowered: int | None = None
        self._lowerings: collections.deque[float] = collections.deque()
        self._alarmed: set[str] = set()
        self._transient = 0
        self._quota = 0
        self._time_refusals = 0
        self._incomplete = 0
        self._diverged_claims = 0
        self._mismatch_claims = 0
        self._replans = 0
        self._last_renew = 0.0
        self._sync_renew = False
        self._alert = threading.Event()
        self._held = False
        self._renew_error: BaseException | None = None
        self._permanent = False
        self.error: BaseException | None = None
        self.published: list[tuple[int, str, str]] = []  # (n, hash, kind) accepted in this run, for R5 and tests

    # -- lifecycle (R5) ------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("started twice")
        self._thread = threading.Thread(target=self._run, name=f"orch-relay-pub-{self.id8}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Set the stop event and end a blocked call (``shutdown`` of this thread's connection, 11A.7)."""
        self._stop.set()
        self._wake.set()
        self._alert.set()
        c = self._conn
        if c is not None:
            try:
                c.shutdown()
            except Exception:  # noqa: BLE001 - best effort
                pass

    def join(self, timeout: float) -> bool:
        t = self._thread
        if t is None:
            return True
        t.join(max(0.0, timeout))
        if t.is_alive():
            log.error("relay_thread_stuck: %s", t.name)
        return not t.is_alive()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _run(self) -> None:
        try:
            while not self._stop.is_set() and self._session.gen is None:
                self._stop.wait(START_POLL_S)  # the start fence: nothing before this holder's own host/hold
            while not self._stop.is_set():
                state, wait = self.step()
                if state == "idle":
                    self._idle()
                elif wait > 0 and self._held:
                    # a PublishHeld (an oversized restore, the reader-level hold...) is re-checked at once on wake()
                    self._wake.wait(wait)
                    self._wake.clear()
                    if self._stop.is_set():
                        return
                elif wait > 0 and self._stop.wait(wait):
                    # review delta 2, F1: backoff, Retry-After floors, quota and permanent-refusal waits are never cut
                    # by wake() (R5 wakes on every append): only stop() ends them
                    return
        except Exception as e:  # noqa: BLE001 - a fence or session error (or a bug): the publisher stops, R5 is told
            if not self._stop.is_set():
                self.error = e
                log.error("relay publisher stopped: %s", type(e).__name__)
                if self._on_error is not None:
                    self._on_error(e)
            self._stop.set()

    def wake(self) -> None:
        """R5's hint that the store queued an outbox entry (an append while linked): publish now, not at the next
        idle check."""
        self._wake.set()

    def _idle(self) -> None:
        """Idle: a local check of the outbox every :data:`IDLE_POLL_S` (or at once on :meth:`wake`), and a full step
        (which reads H from the relay) only when an entry is queued or :data:`IDLE_RECHECK_S` passed, so an idle
        workspace does not read the relay every second."""
        since = self._mono()
        while not self._stop.is_set():
            self._wake.wait(IDLE_POLL_S)
            self._wake.clear()
            if self._stop.is_set() or self._mono() - since >= IDLE_RECHECK_S:
                return
            try:
                queued = self._job(lambda: self._store.outbox_first())
            except StoreError as e:  # review R3: a store fault never stops the publisher; only a fence or session error
                if e.code == "store.busy":
                    continue  # another process holds the workspace lock: poll again
                self._raise(_store_alarm(e.code), "")
                return  # the step meets it again and holds with its backoff
            if queued is not None:
                return

    # -- R1's first link -----------------------------------------------------------------------------------------

    def publish_first(self, session: Session, directory: Directory) -> None:
        """R5's ``Publisher.publish_first`` (``orch.relay.worker``, PR #532), so ``LinkSteps.publish`` (R1
        ``first_link`` step 4): publish on the caller's thread until the relay holds the
        newest local checkpoint and the outbox is empty. A held publication raises :class:`PublishHeld` (the link
        stays ``linking`` and resumes at the next start); a fence or session error propagates."""
        if session is not self._session:
            raise ValueError("the first link publishes through this publisher's own session")
        del directory  # the publisher reads through its own verified reader (same session, own connection)
        done = threading.Event()
        self._alert.clear()
        self._renew_error = None
        renewer = None
        if self._conn_factory is not None:  # a renewal thread with its own connection covers a long single call too
            renewer = threading.Thread(target=self._renew_loop, args=(done,), name=f"orch-relay-renew-{self.id8}",
                                       daemon=True)  # fmt: skip
            renewer.start()
        self._last_renew = self._mono()
        self._sync_renew = renewer is None
        try:
            while not self._stop.is_set():
                self._first_check()
                self._tick()
                state, wait = self.step(raise_held=True)
                if state == "idle":
                    return
                while wait > 0 and not self._stop.is_set():  # waits are cut at the renewal period
                    self._first_check()
                    part = min(wait, RENEW_EVERY_S)
                    self._alert.wait(part)  # the renewal thread or stop() wakes it at once (review delta F3)
                    self._alert.clear()
                    wait -= part
                    self._first_check()
                    self._tick()
        finally:
            self._sync_renew = False
            done.set()
            if renewer is not None:
                renewer.join(SMALL_DEADLINE_S)
        raise PublishHeld("relay_stopped", "the publisher was stopped during the first link")

    def _first_check(self) -> None:
        """During the first link: a renewal that ended the session (a fence, a malformed hold answer) or a fenced
        session stops ``publish_first`` at once (review delta F2, F3)."""
        err = self._renew_error
        if err is not None:
            raise err
        if self._session.fenced:
            raise Fenced("fenced during the first link")

    def _tick(self) -> None:
        """Without a renewal thread (no connection factory): renew the first link's lease before a request once
        :data:`RENEW_EVERY_S` passed. A transient failure (5xx, transport, a login hold-off) is retried after
        :data:`RENEW_RETRY_S`; only a fence or another session error propagates (review delta F1)."""
        if self._sync_renew and self._mono() - self._last_renew >= RENEW_EVERY_S:
            try:
                self._session.renew(transport=self._transport())
            except RedirectRefused:
                self._alarm_cb("relay_redirect", "a 3xx answer to the first link's lease renewal")
                raise
            except (RelayRefused, TransportError, LoginHeldOff) as e:
                log.info("relay first-link renewal failed: %s", type(e).__name__)
                self._last_renew = self._mono() - RENEW_EVERY_S + RENEW_RETRY_S
                return
            self._last_renew = self._mono()

    def _renew_loop(self, done: threading.Event) -> None:
        """The first link's lease renewal (review R2): every :data:`RENEW_EVERY_S` over its own connection while
        :meth:`publish_first` runs; a transient failure (5xx, transport, a login hold-off) is retried after
        :data:`RENEW_RETRY_S`, so one timed-out renewal does not lose the lease. A fence or another session error ends
        it AND is handed to ``publish_first``, which raises it at once (review delta F2)."""
        conn = self._conn_factory() if self._conn_factory is not None else None
        period = RENEW_EVERY_S
        try:
            while not done.wait(period):
                try:
                    self._session.renew(transport=conn)
                    period = RENEW_EVERY_S
                except RedirectRefused as e:  # review delta 2, F5: permanent, alarmed, handed over (never retried)
                    self._alarm_cb("relay_redirect", "a 3xx answer to the first link's lease renewal")
                    self._renew_error = e
                    self._alert.set()
                    return
                except (RelayRefused, TransportError, LoginHeldOff) as e:
                    log.info("relay first-link renewal failed: %s", type(e).__name__)
                    period = RENEW_RETRY_S
                except SessionError as e:
                    log.warning("relay first-link renewal stopped: %s", type(e).__name__)
                    self._renew_error = e
                    self._alert.set()
                    return
                except Exception as e:  # noqa: BLE001 - a bug: alarmed and handed over, never a silent end (F5)
                    self._alarm_cb("relay_renewal_failed", type(e).__name__)
                    self._renew_error = e
                    self._alert.set()
                    return
        finally:
            if conn is not None:
                try:
                    conn.shutdown()
                except Exception:  # noqa: BLE001 - best effort
                    pass

    link_publish = publish_first

    # -- one step ------------------------------------------------------------------------------------------------

    def step(self, *, raise_held: bool = False) -> tuple[str, float]:
        """One publication step: ``("idle", 0)``, ``("progress", 0)``, or ``("wait", seconds)`` after an alarm or a
        refusal. Fence and session errors (but :class:`RelayMisbehaving`) propagate."""
        self._held = False
        try:
            done = self.run_once()
        except _Again:
            return "progress", 0.0
        except _Wait as w:
            return "wait", w.seconds
        except PublishHeld as h:
            self._raise(h.code, h.detail)
            if raise_held:
                raise
            self._held = True
            return "wait", HELD_RECHECK_S
        except RelayMisbehaving as e:
            self._raise("relay_misbehaving", str(e))
            return "wait", self._backoff()
        except RelayRefused as e:
            self._permanent = False
            wait = self._refused(e)
            if raise_held and self._permanent:  # the first link: a permanent answer is R1/R5's to decide (delta F3)
                raise PublishHeld("relay_publish_refused", f"{e.status} {e.code}") from None
            return "wait", wait
        except RedirectRefused:
            self._raise("relay_redirect", "a 3xx answer: no redirect is followed (11A.7)")
            return "wait", HELD_RECHECK_S
        except TransportError as e:
            log.info("relay publish: transport %s", e.kind)
            return "wait", self._backoff()
        except NotHolder:
            raise
        except StoreError as e:
            return "wait", self._store_error(e, raise_held)
        except (wire.WireError, StateError) as e:
            self._raise("relay_publish_local", type(e).__name__)
            return "wait", self._backoff()
        if done:
            return "progress", 0.0
        return "idle", 0.0

    def run_once(self) -> bool:
        """Read and classify H, then publish the next item. True when something was published (or deleted)."""
        cap = self.cap()
        genesis = self._job(lambda: self._store.genesis)
        if genesis is None:
            raise PublishHeld("chain.broken", "no genesis")
        H = self._reader().get_checkpoint(genesis=genesis)
        pos = self._job(lambda: self._store.relay_position(None if H is None else H.checkpoint))
        self.classify(H, pos)
        pub = self._next(H, pos, cap)
        if pub is None:
            self._settled()
            return False
        self._publish(pub, H, cap)
        self._settled()
        return True

    # -- classification (11A.5 "Startup classification") --------------------------------------------------------

    def classify(self, H: VerifiedCheckpoint | None, pos: RelayPosition) -> None:
        """Raise :class:`PublishHeld` for H7 host-state loss and an unexplained fork (``chain.diverged``, after the
        degrade hook); alarm ``relay_rolled_back`` for an H behind the history. H was verified by R1's reader (WSK
        signature, workspace, pinned genesis, the genesis pin's owner-device signature)."""
        last = self.history.last()
        if H is None:
            if last is not None:
                self._raise("relay_rolled_back", "the relay holds no checkpoint after an accepted publication")
            return
        if H.n > pos.numbering:
            raise PublishHeld("relay_host_state_lost", f"relay n {H.n} is above the host's numbering")  # H7
        if pos.contained:
            if last is not None and H.n < last["n"]:
                self._raise("relay_rolled_back", f"relay n {H.n} is below the last accepted n {last['n']}")
            return
        for e in self._job(lambda: self._store.outbox_entries()):
            if e.hash == H.hash or (e.restore is not None and _ref(e.restore["source"]) == H.hash):
                return  # a mismatch with an entry still in the outbox is not divergence: the outbox rules decide (§11)
        for rec in self.history.restores():
            if H.n >= rec["n"]:
                continue
            src = rec["source"]
            if src is None:
                continue
            ancestor = H.hash == _ref(src) or self._job(lambda src=src: self._store.relay_ancestor(H.checkpoint, src))
            if ancestor and (pos.abandoned or self.history.contains(H.hash)):
                self._raise("relay_rolled_back", f"relay n {H.n} is behind accepted restore {rec['n']}")
                return
        if self._on_diverged is not None and "chain.diverged" not in self._alarmed:
            self._on_diverged(H)
        raise PublishHeld("chain.diverged", f"relay n {H.n} is neither in the logs nor explained by a restore")

    # -- planning ------------------------------------------------------------------------------------------------

    def _next(self, H: VerifiedCheckpoint | None, pos: RelayPosition, cap: int) -> _Pub | None:
        hn = 0 if H is None else H.n
        hh = None if H is None else H.hash
        for rec in self.history.restores():  # 1: a relay that lost an accepted restore
            if rec["n"] <= hn:
                continue
            src = rec["source"]
            if _ref(src) != hh:
                if src is None:
                    raise PublishHeld("restore_source_mismatch", f"accepted restore {rec['n']} has no source")
                return self._plain(H, src, cap, None, anchor=True)
            return self._stored_restore(H, rec, cap)
        entry = self._job(self._store.outbox_first)  # 2: the lowest outbox entry
        if entry is None:
            cur = pos.current
            if cur is None or cur["hash"] == hh or (H is not None and cur["n"] <= H.n):
                return None
            if not self._catch_up(H, cap):  # 3: the relay is behind the logs, nothing queued (first link, a rollback)
                return None  # nothing lies beyond H: the newest local checkpoint holds the same logs
            raise _Again()
        if entry.kind == "restore":
            if H is not None and entry.n <= H.n:
                # review R1: the relay already holds this restore (a lost acknowledgement, a crash before the delete)
                # or a later checkpoint descending from it: finish the history and the delete, never hold for ever
                if entry.hash == H.hash or self._job(
                    lambda: self._store.relay_ancestor(entry.checkpoint, H.checkpoint)
                ):
                    self._finish_held(self._restore_done(entry))
                    raise _Again()
                raise PublishHeld("restore_source_mismatch", f"restore n {entry.n} is at or below the relay's n {H.n}")
            return self._restore(H, entry, cap)
        if H is not None and entry.n <= H.n:
            if entry.hash == H.hash:
                self._finish_held(_Pub("plain", entry.checkpoint, entry.n, entry.hash, H, entry=entry, direct=True))
                raise _Again()
            return _Pub("plain", entry.checkpoint, entry.n, entry.hash, H, entry=entry, direct=True)
        return self._plain(H, entry.checkpoint, cap, entry, anchor=False)

    def _plain(
        self, H: VerifiedCheckpoint | None, target: dict[str, Any], cap: int, entry: OutboxEntry | None, *, anchor: bool
    ) -> _Pub:
        runs = _extension_runs(None if H is None else H.checkpoint, target)
        n, h = target["o"]["n"], canon.checkpoint_hash(target["o"])
        if runs is None:
            if anchor:  # a restore's source must be published as it is: no catch-up below an anchor (11A.5)
                raise PublishHeld("relay_rolled_back", f"the relay's H does not extend to restore source n {n}")
            self._replan(H, cap)
        size = wire.set_bytes(_layout(runs))
        if size > cap:
            if anchor:
                raise PublishHeld("relay.too_large", f"the set to restore source n {n} is {size} > {cap} bytes")
            self._replan(H, cap)
        return _Pub("plain", target, n, h, H, runs=runs, entry=entry)

    def _replan(self, H: VerifiedCheckpoint | None, cap: int) -> NoReturn:
        """Catch-up for a queued plain entry that is too large or not an extension of H: the minted entries
        supersede it; plan again."""
        if not self._catch_up(H, cap):
            raise PublishHeld("relay_outbox_unpublishable", "catch-up found nothing beyond H for a queued entry")
        raise _Again()

    def _catch_up(self, H: VerifiedCheckpoint | None, cap: int) -> list[OutboxEntry]:
        share = cap * CATCHUP_SHARE[0] // CATCHUP_SHARE[1]
        try:
            minted = self._job(
                lambda: self._store.mint_catchup(source=None if H is None else H.checkpoint, cap=share, measure=measure)
            )
        except StoreError as e:
            if e.code == "checkpoint.prefix_invalid" and self._replans < 3:
                self._replans += 1  # the logs or the numbering moved while measure ran: plan again (H12b)
                raise _Again() from None
            if e.code == "relay.too_large":
                raise PublishHeld("relay.too_large", "catch-up cannot split this backlog (a queued restore or one "
                                  "event's step above the cap)") from None  # fmt: skip
            raise
        self._replans = 0
        log.info("relay publish: catch-up minted %d checkpoint(s)", len(minted))
        return minted

    def _restore(self, H: VerifiedCheckpoint | None, entry: OutboxEntry, cap: int) -> _Pub:
        r = entry.restore
        assert r is not None
        src = r["source"]
        if _ref(src) != (None if H is None else H.hash):
            if src is not None and (H is None or self._job(lambda: self._store.relay_ancestor(H.checkpoint, src))):
                return self._plain(H, src, cap, None, anchor=True)  # the relay is behind the restore's source
            raise PublishHeld("restore_source_mismatch", f"restore n {entry.n}: the relay holds another source")
        if not r["publication_complete"]:
            raise PublishHeld("relay_restore_incomplete", f"restore n {entry.n}: the abandoned heads are not known")
        line, lg = r["event"], r["log"]
        runs = _restore_runs(src, entry.checkpoint, line, lg, r["acc_from"], evidence=r)
        return self._restore_pub(H, entry.checkpoint, line, lg, r, runs, cap, entry=entry, acc_from=r["acc_from"])

    def _restore_done(self, entry: OutboxEntry) -> _Pub:
        """The publication record of a restore entry the relay already holds (no set, no PUT)."""
        r = entry.restore
        assert r is not None
        bundle = {"line": r["event"], "cert": r["device_cert"], "authz": {"delegation": r["delegation"]},
                  "log": r["log"]}  # fmt: skip
        return _Pub("restore", entry.checkpoint, entry.n, entry.hash, None, restore=bundle, entry=entry, direct=True,
                    record_source=r["source"], acc_from=r["acc_from"])  # fmt: skip

    def _stored_restore(self, H: VerifiedCheckpoint | None, rec: dict[str, Any], cap: int) -> _Pub:
        body = rec["body"]
        try:
            target, bundle = body["checkpoint"], body["restore"]
            line, lg = bundle["line"], bundle["log"]
            ev = {"device_cert": bundle["cert"], "delegation": bundle["authz"]["delegation"]}
        except (KeyError, TypeError):
            raise StateError(f"the record of accepted restore {rec['n']} is malformed") from None
        runs = _restore_runs(rec["source"], target, line, lg, rec["acc_from"], evidence=None)
        return self._restore_pub(H, target, line, lg, ev, runs, cap, entry=None, acc_from=rec["acc_from"],
                                 source=rec["source"])  # fmt: skip

    def _restore_pub(
        self,
        H: VerifiedCheckpoint | None,
        target: dict[str, Any],
        line: dict[str, Any],
        lg: str,
        ev: Mapping[str, Any],
        runs: list[_Run],
        cap: int,
        *,
        entry: OutboxEntry | None,
        acc_from: str,
        source: dict[str, Any] | None = None,
    ) -> _Pub:
        n = target["o"]["n"]
        if len(canon.dumps(line)) + 1 > LINE_CAP:
            raise PublishHeld("relay.too_large", f"restore n {n}: the line is above {LINE_CAP} bytes")
        size = wire.set_bytes(_layout(runs))
        if size > cap:  # a restore cannot be split (P3-D37): never sent
            raise PublishHeld("relay.too_large", f"restore n {n}: its set is {size} > {cap} bytes")
        # H12d-3 (#555, §0.8.6 item 9.8): the one place every restore publication is built (outbox entries and stored
        # records alike). The level is read fresh each time (never cached here); a held restore stays queued, is
        # never lowered or dropped, and is checked again at the next step (hourly, or at once on wake()).
        level = self._current_level()
        if readers.restore_held(level, [line]):
            raise PublishHeld("relay_reader_old", f"restore n {n} carries a convenience event; relay reader level "
                              f"{level}")  # fmt: skip
        bundle = {"line": line, "cert": ev["device_cert"], "authz": {"delegation": ev["delegation"]}, "log": lg}
        src = source if source is not None else (entry.restore["source"] if entry and entry.restore else None)
        return _Pub("restore", target, n, canon.checkpoint_hash(target["o"]), H, runs=runs, restore=bundle,
                    entry=entry, record_source=src, acc_from=acc_from)  # fmt: skip

    # -- publishing ----------------------------------------------------------------------------------------------

    def _publish(self, pub: _Pub, H: VerifiedCheckpoint | None, cap: int) -> None:
        source_hash = None if H is None else H.hash
        if len(canon.dumps(_final_body(pub, source_hash))) > FINAL_CAP:  # review R7: held before anything is staged
            raise PublishHeld("relay.too_large", f"n {pub.n}: the final body is above {FINAL_CAP} bytes")
        if not pub.direct:
            self._stage(pub, source_hash, cap)
        ack = self._final(pub, source_hash)
        self._confirm(pub, ack)
        self._accepted(pub)

    def _base(self) -> str:
        return f"/v2/workspaces/{self._session.workspace_id}"

    def _stage(self, pub: _Pub, source_hash: str | None, cap: int) -> None:
        layout = _layout(pub.runs)
        size = wire.set_bytes(layout)
        if size > cap:
            raise _Again()  # C moved since planning (a lowering): plan again
        manifest = self._journal.load(pub, source_hash, size)
        if manifest is None:
            digests, expect = [], {(r.kind, r.log): r.after["acc"] for r in pub.runs}
            for spec in layout:  # pass 1: every page once, in order, for its digest; each run must join and fold
                _, digest, start, end = self._page(pub, spec)
                if start != expect[(spec.kind, spec.log)]:
                    raise PublishHeld("relay_staging_rebuild", f"{spec.log}: the pages of n {pub.n} do not join")
                expect[(spec.kind, spec.log)] = end
                digests.append(digest)
            for r in pub.runs:
                if r.count and expect[(r.kind, r.log)] != r.upto["acc"]:
                    raise PublishHeld("relay_staging_rebuild", f"{r.log}: the heads do not fold to the target")
            manifest = wire.build_manifest(layout, digests)
            if len(manifest) + sum(wire.page_bytes(s.kind, s.log, s.after_seq, s.count) for s in layout) != size:
                raise wire.WireError("too_large", "the exact counter disagrees with the built set")
            self._journal.save(pub, source_hash, size, cap, manifest)
        recorded = _manifest_digests(manifest, layout)
        base = f"{self._base()}/staging/{source_hash or 'null'}/{pub.hash}"
        for _ in range(MANIFEST_ROUNDS):
            missing = self._put_manifest(base, manifest, len(layout), size, pub)
            if not missing:
                return
            for i in missing:
                page, digest, _, _ = self._page(pub, layout[i])
                if digest != recorded[i]:
                    self._journal.clear()
                    raise PublishHeld("relay_staging_rebuild", f"page {i} of n {pub.n} no longer builds identically")
                self._put(f"{base}/pages/{i}", "PUT /v2/workspaces/{ws}/staging/{source}/{target}/pages/{i}", page,
                          size, pub)  # fmt: skip
        raise RelayMisbehaving("the staged set stays incomplete after every page was sent")

    def _page(self, pub: _Pub, spec: wire.PageSpec) -> tuple[bytes, str, str, str]:
        """One page built from its run: ``(body, digest, from_acc, acc after it)``; the last page of a run must end at
        the run's signed endpoint."""
        run = next(r for r in pub.runs if r.kind == spec.kind and r.log == spec.log)
        skip = spec.after_seq - int(run.after["seq"])
        if run.heads is not None:
            heads = run.heads[skip : skip + spec.count]
            acc = run.after["acc"]
            for h in run.heads[:skip]:
                acc = canon.log_acc(acc, h)
        else:
            seg = self._job(
                lambda: self._store.relay_segment(run.log, run.after, run.upto, skip=skip, limit=spec.count)
            )
            heads, acc = seg.heads, seg.from_acc
            if seg.after_seq != spec.after_seq:
                raise wire.WireError("malformed", "a segment from another position")
        if len(heads) != spec.count:
            raise wire.WireError("malformed", "a page with another number of heads")
        end = acc
        for h in heads:
            end = canon.log_acc(end, h)
        last = spec.after_seq + spec.count == int(run.upto["seq"])
        if last and (end != run.upto["acc"] or heads[-1] != run.upto["head"]):
            raise PublishHeld("relay_staging_rebuild", f"{spec.log}: the heads do not fold to the target of n {pub.n}")
        body, digest = wire.build_page(spec.kind, spec.log, spec.after_seq, acc, list(heads))
        return body, digest, acc, end

    def _put_manifest(self, base: str, manifest: bytes, pages: int, size: int, pub: _Pub) -> list[int]:
        obj = self._put(f"{base}/manifest", "PUT /v2/workspaces/{ws}/staging/{source}/{target}/manifest", manifest,
                        size, pub, keys={"complete", "missing"}, cap=LIST_RESPONSE_CAP)  # fmt: skip
        complete, missing = obj["complete"], obj["missing"]
        if (
            type(complete) is not bool
            or type(missing) is not list
            or any(type(i) is not int or not 0 <= i < pages for i in missing)
            or len(set(missing)) != len(missing)
            or complete != (not missing)
        ):
            raise RelayMisbehaving("manifest answer")
        return missing

    def _put(
        self,
        target: str,
        route: str,
        data: bytes,
        size: int,
        pub: _Pub,
        *,
        keys: set[str] | None = None,
        cap: int = STAGE_RESPONSE_CAP,
    ) -> dict[str, Any]:
        self._tick()
        try:
            return self._session.call(
                "PUT", target, route, raw=data, content_type="application/json", holder=True,
                request_cap=wire.PAGE_CAP, cap=cap, deadline_s=STAGE_DEADLINE_S,
                keys=keys if keys is not None else set(), transport=self._transport(),
            )  # fmt: skip
        except RelayRefused as e:
            if e.status == 413 or (e.code == "too_large" and e.status == 400):
                self._too_large(pub, size)
            if e.status == 409 and e.code == "staging_conflict":
                self._journal.clear()
                self._raise("relay_staging_conflict", f"n {pub.n}: the relay holds other bytes for this set")
                raise _Wait(STAGING_EXPIRY_S) from None
            raise

    def _final(self, pub: _Pub, source_hash: str | None) -> dict[str, Any]:
        body = _final_body(pub, source_hash)
        self._tick()
        try:
            obj = self._session.call(
                "PUT", f"{self._base()}/checkpoint", "PUT /v2/workspaces/{ws}/checkpoint", body=body, holder=True,
                request_cap=FINAL_CAP, cap=LIST_RESPONSE_CAP, deadline_s=SMALL_DEADLINE_S, keys=None,
                transport=self._transport(),
            )  # fmt: skip
        except RelayRefused as e:
            self._final_refused(pub, e)
            raise
        allowed = {"result", "n", "hash", "unpublished", "unpublished_refs"}
        if not {"result", "n", "hash"} <= set(obj) <= allowed:
            raise RelayMisbehaving("final answer field set")
        if obj["result"] not in ("accepted", "ignored") or obj["n"] != pub.n or obj["hash"] != pub.hash:
            raise RelayMisbehaving("the acknowledgement does not name this checkpoint")
        if obj["result"] == "ignored" and not pub.direct:
            raise RelayMisbehaving("a staged extension answered ignored")
        return obj

    def _confirm(self, pub: _Pub, ack: Mapping[str, Any]) -> None:
        """Verify the acknowledgement with a fresh verified read of H before anything is deleted."""
        genesis = self._job(lambda: self._store.genesis)
        H = self._reader().get_checkpoint(genesis=genesis or "")
        if H is None:
            raise RelayMisbehaving("acknowledged, but the relay holds no checkpoint")
        if ack["result"] == "accepted" and H.hash != pub.hash:
            raise RelayMisbehaving("acknowledged, but the relay holds another checkpoint")
        if ack["result"] == "ignored" and H.n < pub.n:
            raise RelayMisbehaving("ignored, but the relay holds a lower checkpoint")

    def _finish_held(self, pub: _Pub) -> None:
        """An entry the relay already holds, with no acknowledgement to read (it was lost, or the host stopped before
        the delete; review round 1 R1 and round 2 P1). It goes through the same order as a publication: the fresh
        verified read here IS the confirmation, then the history, then the delete. What the read must prove:

        * the relay's held checkpoint, read again now and verified (WSK signature, workspace, pinned genesis, the
          genesis pin), is exactly this entry's checkpoint; or
        * it is a later checkpoint that descends from this entry (``Store.relay_ancestor`` over the host's own
          logs and recorded ancestry), AND the relay serves this entry itself by its hash, verified (``GET
          checkpoint?hash=``): the relay must show it holds the entry, an ancestry claim alone is not enough.

        Anything else deletes nothing: an unprovable restore holds ``relay_restore_unconfirmed``, a held checkpoint on
        another branch holds ``restore_source_mismatch`` (a plain entry: ``relay_misbehaving``). The relay can only
        show checkpoints this host signed; that it keeps them afterwards is the threat model's limit (11A.9), as
        after an acknowledged publication."""
        genesis = self._job(lambda: self._store.genesis) or ""
        reader = self._reader()
        H = reader.get_checkpoint(genesis=genesis)
        if H is None:
            raise RelayMisbehaving("the relay holds no checkpoint after it held this entry")
        if H.hash != pub.hash:
            descends = H.n > pub.n and self._job(lambda: self._store.relay_ancestor(pub.target, H.checkpoint))
            if not descends:
                if pub.kind == "restore":
                    raise PublishHeld("restore_source_mismatch", f"restore n {pub.n}: the relay holds another branch")
                raise RelayMisbehaving("the relay no longer holds this entry or a checkpoint descending from it")
            try:
                own = reader.get_checkpoint(genesis=genesis, hash=pub.hash)
            except RelayRefused as e:
                if e.status != 410:
                    raise
                own = None  # checkpoint_gone: not provable from the relay any more
            if own is None or own.hash != pub.hash:
                raise PublishHeld("relay_restore_unconfirmed" if pub.kind == "restore" else "relay_unconfirmed",
                                  f"n {pub.n}: the relay does not serve this entry by its hash")  # fmt: skip
        self._accepted(pub)

    def _accepted(self, pub: _Pub) -> None:
        """The relay's acknowledgement is verified: the history first, then the outbox entry (11A.3, 11A.5)."""
        body = None
        if pub.kind == "restore":
            body = {"checkpoint": pub.target, "source_hash": _ref(pub.record_source), "restore": pub.restore}
        self.history.append(n=pub.n, hash=pub.hash, kind=pub.kind, at_ms=self._now_ms(), target=pub.target,
                            body=body, source=pub.record_source, acc_from=pub.acc_from)  # fmt: skip
        self._journal.clear()
        if pub.entry is not None:
            try:
                self._job(lambda: self._store.outbox_delete(pub.n, pub.hash))
            except StoreError as e:
                if e.code == "outbox.mismatch":
                    raise PublishHeld(
                        "relay_outbox_mismatch", f"outbox entry {pub.n} holds another checkpoint"
                    ) from None
                raise
        self.published.append((pub.n, pub.hash, pub.kind))
        log.info("relay publish: n %d accepted (%s)", pub.n, pub.kind)

    # -- refusals, alarms, waits ---------------------------------------------------------------------------------

    def _final_refused(self, pub: _Pub, e: RelayRefused) -> None:
        code = e.code
        if e.status == 413 or (e.status == 400 and code == "too_large"):
            self._too_large(pub, wire.set_bytes(_layout(pub.runs)))
        if e.status != 409:
            return
        if code == "staging_incomplete":
            self._journal.clear()
            self._incomplete += 1
            if self._incomplete >= INCOMPLETE_LIMIT:
                self._incomplete = 0
                raise RelayMisbehaving("staging stays incomplete after the set was staged") from None
            raise _Again() from None
        if code == "chain_diverged":
            self._diverged_claims += 1  # never evidence by itself: the next step re-reads H and classifies it
            if self._diverged_claims >= 2:
                raise RelayMisbehaving("chain_diverged for a publication the logs contain") from None
            raise _Again() from None
        if code == "checkpoint_time":
            self._time_refusals += 1
            if self._time_refusals >= CLOCK_SKEW_AFTER:
                self._raise("clock_skew", f"checkpoint_time refused {self._time_refusals} times")
            raise _Wait(self._backoff()) from None
        if code == "restore_source_mismatch":
            if pub.kind == "plain":
                self._mismatch_claims += 1  # H moved: plan again from the new H, but never in a hot loop (review R4)
                if self._mismatch_claims >= MISMATCH_LIMIT:
                    raise RelayMisbehaving("restore_source_mismatch for a source read from this relay") from None
                raise _Again() from None
            raise PublishHeld("restore_source_mismatch", f"restore n {pub.n}: re-read H; the owner signs again")
        if code == "restore_missing_revocation":
            raise PublishHeld("restore_missing_revocation", f"restore n {pub.n}: fetch the evidence; the owner signs "
                              "again")  # fmt: skip
        if code in ("restore_invalid", "restore_not_owner", "restore_reused", "genesis_mismatch", "no_genesis"):
            raise PublishHeld(f"relay_refused_{code}", f"n {pub.n}")

    def _too_large(self, pub: _Pub, size: int) -> None:
        """An unexpected ``too_large``: never retried unchanged (11A.5, P3-D37)."""
        self._journal.clear()
        if pub.kind == "restore":
            raise PublishHeld("relay.too_large", f"restore n {pub.n}: the relay refused its set of {size} bytes")
        if pub.direct or not pub.runs:  # no set was sent: the final body itself was refused (review R6)
            raise PublishHeld("relay.too_large", f"n {pub.n}: the relay refused the final body")
        now = self._mono()
        while self._lowerings and now - self._lowerings[0] > 3600.0:
            self._lowerings.popleft()
        if len(self._lowerings) >= LOWERINGS_PER_HOUR:
            raise PublishHeld("relay_too_large", "the relay's maximum is below link.json's; correct link.json")
        lowered = min(self.cap(), size // 2)
        if lowered < MIN_CAP:
            raise PublishHeld("relay_too_large", f"the relay refused {size} bytes; below {MIN_CAP} nothing is lowered")
        self._lowerings.append(now)
        self._lowered = lowered
        log.warning("relay publish: too_large for %d bytes; cap lowered to %d", size, self._lowered)
        raise _Again()

    def _refused(self, e: RelayRefused) -> float:
        if e.status == 429 and e.code == "quota_exceeded":
            if e.quota in PERMANENT_QUOTAS:
                self._raise("relay_quota", f"permanent quota {e.quota}")
                self._permanent = True
                return HELD_RECHECK_S
            self._quota += 1
            if self._quota > QUOTA_RETRIES:
                self._raise("relay_quota", f"quota_exceeded {self._quota} times")
                return QUOTA_WAIT_S
            return delay(self._quota - 1, e.retry_after, self._rng)
        if e.status >= 500 or (e.status == 429 and e.code == "rate_limited"):
            self._transient += 1
            return delay(self._transient - 1, e.retry_after, self._rng)
        self._raise("relay_publish_refused", f"{e.status} {e.code}")
        self._permanent = True
        return HELD_RECHECK_S  # a permanent answer is never retried unchanged at once (11A.7, review R8)

    def _store_error(self, e: StoreError, raise_held: bool) -> float:
        if e.code == "store.busy":
            return self._backoff()
        code = _store_alarm(e.code)
        self._raise(code, "")
        if raise_held:
            raise PublishHeld(code, "") from None
        return HELD_RECHECK_S

    def _backoff(self) -> float:
        self._transient += 1
        return delay(self._transient - 1, None, self._rng)

    def _settled(self) -> None:
        self._transient = self._quota = self._time_refusals = self._incomplete = self._diverged_claims = 0
        self._mismatch_claims = 0
        self._alarmed.clear()

    def _raise(self, code: str, detail: str) -> None:
        if code in self._alarmed:
            return
        self._alarmed.add(code)
        log.warning("relay publish alarm %s", code)
        self._alarm_cb(code, detail)

    # -- the relay's cap and readers -----------------------------------------------------------------------------

    def cap(self) -> int:
        """C: the smallest of ``link.json``'s maximum, ``relay-info``'s (read once per start), the relay's staging
        quota and an in-memory lowering."""
        if not self._info_read:
            self._read_info()
        caps = [self._link_cap, STAGE_CAP]
        if self._info_cap is not None:
            caps.append(self._info_cap)
        if self._lowered is not None:
            caps.append(self._lowered)
        return min(caps)

    def _read_info(self) -> None:
        try:
            obj = self._session.call("GET", "/v2/relay-info", "GET /v2/relay-info", auth=False, cap=INFO_CAP,
                                     deadline_s=SMALL_DEADLINE_S, keys=None, transport=self._transport())  # fmt: skip
        except RelayRefused as e:
            if e.status != 404:
                raise
            self._info_cap, self.reader_level = None, 0  # an older relay: level 0, no announced maximum
            self._info_read = True
            return
        level, mx = obj.get("reader"), obj.get("max_publication_bytes")
        if obj.get("v") != 1 or type(level) is not int or level < 0:
            raise RelayMisbehaving("relay-info")
        if mx is not None and (type(mx) is not int or mx < 1):
            raise RelayMisbehaving("relay-info max_publication_bytes")
        self._info_cap, self.reader_level = mx, level
        self._info_read = True
        if mx is not None and mx < self._link_cap:
            log.info("relay publish: relay-info maximum %d below link.json's %d", mx, self._link_cap)

    def _current_level(self) -> int | None:
        """The relay's last verified reader level for the H12d-3 hold: R5's ``relay_level()`` (the host's stored level,
        ``readerrecs.relay_level(state, ws, uid)``, which its login observer writes), read fresh at every call; a
        failing read counts as 0 (fail closed), and so does a publisher built without it (relay-info's level, an
        honest-operator statement, is never the hold's input)."""
        if self._relay_level is None:
            return 0  # review delta 2, F4: no host-recorded level is level 0 (relay-info is an honest-operator claim)
        try:
            return self._relay_level()
        except Exception:  # noqa: BLE001 - any doubt is the lowest level
            log.warning("relay publish: the reader level could not be read; counting 0")
            return 0

    def _transport(self) -> Transport | None:
        if self._conn is None and self._conn_factory is not None:
            self._conn = self._conn_factory()
        return self._conn

    def _reader(self) -> Directory:
        if self._dir is None:
            self._dir = Directory(self._session, owner_pk_pub=self._owner, wsk_pub=self._wsk,
                                  transport=self._transport())  # fmt: skip
        return self._dir


class _Wait(Exception):  # noqa: N818 - internal: wait this long, then step again
    def __init__(self, seconds: float) -> None:
        super().__init__(f"wait {seconds}")
        self.seconds = seconds


def _store_alarm(code: str) -> str:
    """A store refusal as a host alarm, never mistaken for a relay fork (review R9): ``chain.diverged`` from the
    store (no verified path, a diverged log) is ``relay_store_chain_diverged``, not R3's ``chain.diverged``."""
    if code == "store.torn_write":
        return "relay_outbox_torn"
    if code == "relay.too_large":
        return code
    return "relay_store_" + code.replace(".", "_")


def _final_body(pub: _Pub, source_hash: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"checkpoint": pub.target, "source_hash": source_hash}
    if pub.restore is not None:
        body["restore"] = pub.restore
    return body


# -- set layouts ---------------------------------------------------------------------------------------------------


def _layout(runs: list[_Run]) -> list[wire.PageSpec]:
    return wire.plan_pages([(r.kind, r.log, int(r.after["seq"]), r.count) for r in runs])


def _manifest_digests(manifest: bytes, layout: list[wire.PageSpec]) -> list[str]:
    obj = wire.loads_body(manifest, cap=wire.PAGE_CAP)
    pages = obj["pages"] if type(obj) is dict and type(obj.get("pages")) is list else None
    if pages is None or len(pages) != len(layout):
        raise StateError("publish-manifest.json does not match the planned set")
    out = []
    for spec, d in zip(layout, pages, strict=True):
        want = {"i": spec.i, "kind": spec.kind, "log": spec.log, "after_seq": spec.after_seq, "count": spec.count}
        if type(d) is not dict or {k: d.get(k) for k in want} != want:
            raise StateError("publish-manifest.json does not match the planned set")
        out.append(wire.check_hash(d.get("digest")))
    return out


def _extension_runs(source: Mapping[str, Any] | None, target: Mapping[str, Any]) -> list[_Run] | None:
    """The appended runs from ``source`` to ``target``, or None when ``target`` does not extend it (a log of the
    source missing or lower, or another entry at the same height)."""
    old, new = _entries(source), _entries(target)
    if old.keys() - new.keys():
        return None
    runs = []
    for lg, e in new.items():
        o = old.get(lg, EMPTY)
        if e["seq"] < o["seq"] or (e["seq"] == o["seq"] and e != o and o is not EMPTY):
            return None
        if e["seq"] > o["seq"]:
            runs.append(_Run("appended", lg, dict(o), dict(e)))
    return runs


def _restore_runs(
    source: Mapping[str, Any] | None,
    target: Mapping[str, Any],
    line: Mapping[str, Any],
    lg: str,
    acc_from: str,
    *,
    evidence: Mapping[str, Any] | None,
) -> list[_Run]:
    """The runs of a restore's set (proto §19.8 R3): on the restored log ``abandoned`` pages from the retained point
    (``from_seq``) up to the source's entry when the source is above it, else ``retained`` pages from the source's
    entry up to ``from_seq``; the line itself is folded by the relay; every other log ``appended`` from the source to
    the target. Heads come from the evidence (the outbox entry) or, for a republication, from the store. Every
    endpoint is checked against the signed target before anything is staged."""
    try:
        fs, head = line["from_seq"], line["head"]
        old_all, new_all = _entries(source), _entries(target)
        old = old_all.get(lg, EMPTY)
        retained = {"seq": fs, "head": head, "acc": acc_from}
        line_head = canon.event_head(dict(line))
        want = {"seq": fs + 1, "head": line_head, "acc": canon.log_acc(acc_from, line_head)}
        if new_all.get(lg) != want or line.get("seq") != fs + 1 or line.get("prev") != head:
            raise PublishHeld("relay_restore_invalid", "the target does not hold the restore line at from_seq + 1")
        if old_all.keys() - new_all.keys():
            raise PublishHeld("relay_restore_invalid", "the target drops a log of the source")
        runs: list[_Run] = []
        if fs < old["seq"]:
            heads = None if evidence is None else tuple(evidence["abandoned_heads"])
            runs.append(_Run("abandoned", lg, retained, dict(old), heads))
        elif fs > old["seq"]:
            heads = None if evidence is None else tuple(evidence["forward_heads"])
            runs.append(_Run("retained", lg, dict(old), retained, heads))
        elif old is not EMPTY and old["head"] != head:
            raise PublishHeld("relay_restore_invalid", "the retained point is not the source's entry")
        for x, e in new_all.items():
            if x == lg:
                continue
            o = old_all.get(x, EMPTY)
            if e["seq"] < o["seq"] or (e["seq"] == o["seq"] and o is not EMPTY and e != o):
                raise PublishHeld("relay_restore_invalid", f"{x}: the target does not extend the source")
            if e["seq"] > o["seq"]:
                heads = None if evidence is None else tuple(evidence["proofs"][x])
                runs.append(_Run("appended", x, dict(o), dict(e), heads))
    except (KeyError, TypeError, canon.HashError):
        raise PublishHeld("relay_restore_invalid", "the restore evidence is malformed") from None
    for r in runs:
        if r.heads is not None:
            acc = r.after["acc"]
            for h in r.heads:
                acc = canon.log_acc(acc, h)
            if len(r.heads) != r.count or acc != r.upto["acc"] or (r.heads and r.heads[-1] != r.upto["head"]):
                raise PublishHeld("relay_restore_invalid", f"{r.log}: the evidence does not fold to the target")
    return runs
