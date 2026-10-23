"""R4: the unpublished-evidence consumer (contract 11A.9 "Unpublished evidence", relay protocol §19.5 P3-D31).

The relay serves, as a derived view, the revocations of this workspace's ever-devices that its host has not
published yet (``GET /v2/workspaces/{ws}/unpublished``). The host MUST read them (obligation 1), verify each item on
its own (2), check its own association (3), store it durably and only then ack (4), and publish only through F1
§5.3 (5): the host never appends ``device.revoked`` or a restore by itself.

:class:`UnpublishedConsumer` is R5's :class:`orch.relay.worker.Unpublished` seam: :meth:`~UnpublishedConsumer.step`
runs on the aux thread after the lease renewal, at most ONE page per turn (one GET of at most 10 s, then the
judging jobs, then the durable position, then one ack of at most 10 s):

* **When it reads.** From the start (no ``cursor``) after every new holder ``gen``, after ``cursor_ahead``, after a
  ledger loss and at least once a day (``FULL_EVERY_MS``), page by page until ``more`` is false, ALWAYS from the stored
  scan position (never restarted because a scan is long: beyond ``SCAN_LONG_PAGES`` it is slowed and alarmed once);
  otherwise from the stored position while the last poll's ``notices.unpublished`` is non-null and differs from the
  notice a completed read already answered.
* **THE LEDGER (the invariant).** The owner's view is a MONOTONE ledger (:class:`Ledger`): relay input can only ADD a
  row (keyed by the exact ``(device, person)`` pair) or RAISE its ``need``; no relay behaviour (a complete or empty
  read, an omitted or re-served or weaker item, a restart, a new generation, a lost file) removes or lowers anything. A
  row leaves ONLY through :meth:`Ledger.settle_from_store`, on the store's own verified state (``hold``/``judge`` are
  the host's worker jobs around ``Store.note_forwarded_revocation`` and ``Store.pair_standings``), by compare-and-set on
  the row's ``(need, ver)``. Every item gets a result: ``settled`` (no row needed), ``held`` (a row, persisted before
  the next item) or ``unverifiable`` (a sticky counter).
* **Order.** All items of the page have a result, then the new position is written atomically
  (``unpublished.json`` plus its sequence mirror, under one lock), and only then the page's ``cursor`` is acked
  (``POST unpublished/ack {cursor}``, exactly that field). The ack is owed (``ack`` in the file) from the write until
  the relay accepted it; 5 non-transient refusals of it drop it. A store fault (an exception) means no result for
  that item (the ones before it are already persisted), no ack and a back-off.
* **Loss.** A missing, damaged or OLDER ledger file puts the host into ``rebuilding`` (status not ok) until a complete
  read from the start finishes with every item given a result. At every start the ledger is also seeded from what the
  store already holds durably (``evidence``).
* **Failures.** ``cursor_ahead``: restart from the start (the ledger is kept). 429: wait ``Retry-After`` (else 60 s).
  Any other refusal or transport error: one alarm per class, 30 s back-off. The session's own errors (``Fenced``,
  ``SessionError``) are re-raised: the aux thread's guard stops the worker. Unverified data never changes host state.

Nothing here touches the store directly (``orch.store`` is reached only through ``hold``/``judge``/``evidence``) or
any key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from orch.net.http import TransportError
from orch.wsproto.state import StateDir, StateError

from .retry import RelayRefused
from .session import Session

__all__ = [
    "FILE", "FULL_EVERY_MS", "LEDGER_MAX", "MIRROR", "STANDINGS", "Ledger", "UnpublishedConsumer", "held_pairs",
    "ledger_view",
]  # fmt: skip

log = logging.getLogger("orch.relay.unpublished")

FILE = "unpublished.json"
MIRROR = "unpublished.seq"  # the ledger's sequence mirror: written after the ledger, under the same lock
FULL_EVERY_MS = 24 * 3600 * 1000  # obligation 1: at least once a day from the start
PAGE_LIMIT = 64  # the relay's default and maximum
PAGE_CAP = 4 << 20  # 11A.7: R4 pages read at most 4 MiB (the relay sends at most 256 KiB)
DEADLINE_S = 10.0  # 11A.7: 10 s per relay call on the aux thread
BACKOFF_S = 30.0
RATE_LIMIT_S = 60.0
SCAN_LONG_PAGES = 1500  # a full read beyond this many pages is slowed to one page per BACKOFF_S and alarmed once; it
# NEVER restarts from the start or gives up: availability of the host is bounded by the rate, never by reach
HOLD_BUDGET_S = 20.0  # the aux thread waits at most this long on the worker's jobs per turn
FULL_EVERY_S = FULL_EVERY_MS / 1000.0
LEDGER_MAX = 4096  # the relay's ever-device quota per workspace; beyond it: overflow (never an eviction)
PERSON_MAX = 256  # NON-ACTIONABLE rows per person (HostPins' per-person bound): one key cannot crowd out the others
NONACTIONABLE_MAX = 512  # rows in standing `unknown`/`other` (nothing the owner can sign): a separate budget, so they
# can never crowd out settle-able obligations; beyond a cap: a visible counter (`overflow`, `person_overflow`)
OVERFLOW_SEEN_MAX = 4096  # distinct unlisted pairs remembered (short digests) so a rescan does not recount them
PERSON_OVERFLOW_PERSONS = 256  # distinct persons tracked in `person_overflow` (the rest fold into `overflow`)
UNVERIFIABLE_CLEAN_SCANS = 2  # the sticky `unverifiable` counter clears after this many complete clean scans ...
UNVERIFIABLE_HOLD_MS = 7 * 86400 * 1000  # ... AND this long after it was last seen
STALE_MS = 2 * FULL_EVERY_MS  # a status older than this (no complete scan) is stale
FAILED_TURNS_STALE = 3  # ... and so are this many failed turns in a row since the last complete scan
ACK_REFUSALS_MAX = 5  # a pending ack the relay refuses this often (non-transient) is dropped, once alarmed
JUDGE_EVERY_S = 2.0  # the ledger is re-judged against the store at most this often, unless woken
JUDGE_BATCH = 256  # rows per judging job: one store-lock hold never covers the whole ledger
STANDINGS = ("settled", "stronger", "revoke", "other", "unknown")  # Store.pair_standings' closed answer set
_POSITION = re.compile(r"[A-Za-z0-9_-]{1,512}")  # an opaque b64u position, put into a query string unescaped
_HEX32 = re.compile(r"[0-9a-f]{32}")
_SIG = re.compile(r"[0-9a-f]{64}")
#: relay codes this module handles or can name; any other relay text reaches an alarm/status detail only as "other"
KNOWN_CODES = frozenset({
    "cursor_ahead", "rate_limited", "malformed", "bad_request", "unauthorized", "forbidden", "not_found", "conflict",
    "unavailable", "internal", "not_holder", "fenced", "too_large", "overloaded", "quota_exceeded",
})  # fmt: skip
_HEX16 = re.compile(r"[0-9a-f]{16}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REASONS = ("compromised", "lost", "retired")  # the revokeReason enum; any other text is shown as "unknown"
_ROW_KEYS = {"device_id", "person_id", "need", "ver", "digest", "at_ms", "state"}


def safe_code(code: object) -> str:
    """A relay refusal code as it may appear in an alarm or status detail: a member of ``KNOWN_CODES`` or ``other``."""
    return code if type(code) is str and code in KNOWN_CODES else "other"


def safe_status(status: object) -> int:
    return status if type(status) is int and 100 <= status <= 599 else 0


def _position(value: object) -> str:
    if type(value) is not str or not _POSITION.fullmatch(value):
        raise ValueError("position")
    return value


def _empty() -> dict[str, Any]:
    return {
        "v": 2, "cursor": None, "scan": None, "gen": None, "last_full_ms": 0, "notice": None, "ack": None,
        "resume": None, "scan_pages": 0, "ack_refusals": 0, "scan_unv": False,
        # the ledger (only `Ledger` writes these; `_save` bumps ledger_seq)
        "held": [], "unverifiable": {"n": 0, "first_ms": 0, "last_seen_ms": 0, "clean_scans": 0}, "overflow": 0,
        "person_overflow": {}, "overflow_seen": [], "first_scan_ms": 0, "rebuilding": False, "last_complete_ms": 0,
        "failed_turns": 0, "ledger_seq": 0,
    }  # fmt: skip


# -- the ledger: ONE write API ---------------------------------------------------------------------------------------


def _hex(v: object) -> str:
    return v if type(v) is str and _HEX32.fullmatch(v) else "0" * 32


def _need(v: object) -> str:
    return v if type(v) is str and v in _REASONS else "unknown"


def _digest(v: object) -> str:
    return v if type(v) is str and _DIGEST.fullmatch(v) else "sha256:" + "0" * 64


def row_of(item: Mapping[str, Any], at_ms: int) -> dict[str, Any]:
    """A ledger row from a relay item the store judged held: every field reduced to a closed form (a hex id, a reason
    of the enum or ``unknown``, a digest shape); relay text never appears as anything else, and a row ALWAYS results."""
    prov = item.get("provenance")
    return {
        "device_id": _hex(item.get("device_id")), "person_id": _hex(item.get("person_id")),
        "need": _need(item.get("reason", item.get("need"))), "ver": 1,
        "digest": _digest(prov.get("digest") if isinstance(prov, Mapping) else item.get("digest")),
        "at_ms": at_ms, "state": "revoke",
    }  # fmt: skip


class Ledger:
    """The owner's held view over a state record ``st``: the ONLY code that writes ``held``, ``unverifiable``,
    ``overflow``, ``rebuilding``, ``last_complete_ms`` or ``failed_turns`` (an AST test pins it). Relay input reaches it
    only through :meth:`add_or_raise` and :meth:`note_unverifiable`: it can add or raise, never remove or lower."""

    def __init__(self, st: dict[str, Any], stronger: Callable[[str, str], bool]) -> None:
        self._st, self._stronger = st, stronger

    def rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._st["held"]]

    def add_or_raise(self, row: Mapping[str, Any]) -> str:
        """``added`` | ``raised`` | ``same`` | ``overflow``. An equal or weaker item changes nothing."""
        for cur in self._st["held"]:
            if (cur["device_id"], cur["person_id"]) == (row["device_id"], row["person_id"]):
                if self._stronger(row["need"], cur["need"]):
                    cur.update(need=row["need"], digest=row["digest"], at_ms=row["at_ms"], ver=cur["ver"] + 1)
                    return "raised"
                return "same"
        held = self._st["held"]
        person = row["person_id"]
        quiet = row["state"] in ("other", "unknown")  # nothing the owner can sign: the only rows that are capped
        if (
            quiet
            and sum(1 for r in held if r["person_id"] == person and r["state"] in ("other", "unknown")) >= PERSON_MAX
        ):
            self._count_overflow(row, person)  # one person's quiet share is full: a visible per-person counter
            return "overflow"
        if len(held) >= LEDGER_MAX or (
            quiet and sum(1 for r in held if r["state"] in ("other", "unknown")) >= NONACTIONABLE_MAX
        ):
            self._count_overflow(row, None)
            return "overflow"
        self._st["held"].append(dict(row))
        return "added"

    def _count_overflow(self, row: Mapping[str, Any], person: str | None) -> None:
        """A pair that could not be listed counts ONCE (a rescan or a restart re-delivers the same pair): its short
        digest joins ``overflow_seen`` (bounded by ``OVERFLOW_SEEN_MAX``). The count is approximate: the text says
        "up to N", as it may overcount once that list is full or lost, or after a pair is later listed."""
        key = hashlib.sha256(f"{row['device_id']}:{row['person_id']}".encode()).hexdigest()[:16]
        seen = self._st["overflow_seen"]
        if key in seen:
            return
        if len(seen) < OVERFLOW_SEEN_MAX:
            seen.append(key)
        if person is not None:
            po = self._st["person_overflow"]
            if person in po or len(po) < PERSON_OVERFLOW_PERSONS:
                po[person] = po.get(person, 0) + 1
                return
        self._st["overflow"] += 1

    def settle_from_store(self, judged: Sequence[Mapping[str, Any]]) -> bool:
        """THE ONLY REMOVAL. ``judged`` rows carry the store's ``standing`` of the exact ``(device, person)`` pair and
        the ``(need, ver)`` it was judged at: a row goes only on ``settled`` and only if its current ``(need, ver)``
        still equals the judged one (compare-and-set: an answer computed before a raise cannot delete the raised
        requirement); any other standing only rewrites the derived ``state``. Returns whether anything changed."""
        changed = False
        for j in judged:
            for cur in list(self._st["held"]):
                if (cur["device_id"], cur["person_id"]) != (j["device_id"], j["person_id"]):
                    continue
                if (cur["need"], cur["ver"]) != (j["need"], j["ver"]):
                    continue
                if j["standing"] == "settled":
                    self._st["held"].remove(cur)
                    changed = True
                elif cur["state"] != j["standing"]:
                    cur["state"] = j["standing"]
                    changed = True
        return changed

    def note_unverifiable(self, n: int, now_ms: int) -> None:
        u = self._st["unverifiable"]
        u["n"] += n
        u["first_ms"] = u["first_ms"] or now_ms
        u["last_seen_ms"], u["clean_scans"] = now_ms, 0
        self._st["scan_unv"] = True

    def complete_scan(self, now_ms: int) -> None:
        """A COMPLETE full read finished with every item given a result: freshness, the end of ``rebuilding``, and the
        sticky ``unverifiable`` counter's expiry (>= 2 clean complete scans AND 7 days since it was last seen)."""
        st = self._st
        st["last_complete_ms"], st["failed_turns"], st["rebuilding"] = now_ms, 0, False
        u = st["unverifiable"]
        if u["n"] and not st["scan_unv"]:
            u["clean_scans"] += 1
            if u["clean_scans"] >= UNVERIFIABLE_CLEAN_SCANS and now_ms - u["last_seen_ms"] >= UNVERIFIABLE_HOLD_MS:
                st["unverifiable"] = _empty()["unverifiable"]
        st["scan_unv"] = False

    def note_first_scan(self, now_ms: int) -> None:
        """The first read attempt ever: the stale clock starts here when no scan ever completes."""
        if not self._st["first_scan_ms"]:
            self._st["first_scan_ms"] = now_ms

    def start_rebuild(self) -> None:
        self._st["rebuilding"] = True

    def note_failure(self) -> None:
        self._st["failed_turns"] += 1

    def note_success(self) -> None:
        self._st["failed_turns"] = 0


class UnpublishedConsumer:
    """One workspace's R4 consumer. ``hold(item)`` is the host's worker job around the store's one answer, ``"held" |
    "settled" | "dropped" | "unverifiable"``; ``judge(pairs)`` returns the store's standing of each ``(d_, p_,
    need)`` (``STANDINGS``) in one job; ``evidence()`` returns what the store already holds durably (seeding);
    ``stronger(new, old)`` compares two reasons (injected by the host, built on ``revoke_strength``: this module has
    no strength table); ``notices()`` returns the last poll's ``notices`` object (or ``None``); ``alarm(code,
    detail)`` is the worker's alarm sink."""

    def __init__(
        self,
        *,
        workspace_id: str,
        session: Session,
        state: StateDir,
        hold: Callable[[Mapping[str, Any]], str],
        stronger: Callable[[str, str], bool],
        notices: Callable[[], Mapping[str, Any] | None],
        alarm: Callable[[str, str], None],
        now_ms: Callable[[], int],
        judge: Callable[[list[tuple[str, str, str]]], list[str]] | None = None,
        evidence: Callable[[], list[dict[str, str]]] | None = None,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(workspace_id) is not str or not _HEX32.fullmatch(workspace_id):
            raise ValueError("workspace id")
        self.ws = workspace_id
        self._session, self._state, self._hold, self._stronger = session, state, hold, stronger
        self._judge, self._evidence = judge, evidence
        self._notices, self._alarm, self._now_ms, self._mono = notices, alarm, now_ms, mono
        self._not_before = 0.0
        self._alarmed: set[str] = set()
        self._full_mono: float | None = None  # when this process last completed a full read (the wall clock may move)
        self._seeded = False
        self._judged_at: float | None = None
        self._judge_next = 0  # the chunk the next judging turn starts at (a budget cut-off carries over)
        self._woken = True

    def __repr__(self) -> str:
        return f"UnpublishedConsumer(workspace={self.ws})"

    def wake(self) -> None:
        """A store commit that can revoke (``device.revoked``, ``member.removed``, a restore) asks for an immediate
        re-judging of the ledger on the next aux turn (never synchronous)."""
        self._woken = True

    # -- the durable ledger and position -------------------------------------------------------------------------------

    def load(self) -> tuple[dict[str, Any], bool]:
        """``(record, lost)``: ``unpublished.json`` (11A.3) or a fresh record, and whether the LEDGER was lost: the file
        missing while its mirror exists, the mirror missing or AHEAD of the file's ``ledger_seq`` (an older file),
        a file that does not parse, or a damaged row. A fresh install (neither file) is not a loss. The ledger keeps
        every valid row either way; a loss only forces a rebuilding read (never a smaller ledger)."""
        try:
            obj = self._state.read_json(FILE)
        except StateError:
            obj = False  # damaged
        try:
            mirror = self._state.read_json(MIRROR)
        except StateError:
            mirror = False
        mseq = mirror.get("seq") if type(mirror) is dict and type(mirror.get("seq")) is int else None
        st = _empty()
        if obj is None:
            return st, mirror is not None  # no file: a loss only if its mirror says there was one
        if type(obj) is not dict:
            return st, True
        lost = False
        for key in ("cursor", "scan", "notice", "ack"):
            v = obj.get(key)
            if v is None or (type(v) is str and (v == "" or _POSITION.fullmatch(v))):
                st[key] = v
        for key in ("gen", "last_full_ms", "last_complete_ms", "failed_turns", "overflow", "scan_pages",
                    "ack_refusals", "ledger_seq", "first_scan_ms"):  # fmt: skip
            if type(obj.get(key)) is int and 0 <= obj[key] <= 1 << 62:
                st[key] = obj[key]
        st["rebuilding"] = obj.get("rebuilding") is True
        st["scan_unv"] = obj.get("scan_unv") is True
        r = obj.get("resume")
        if type(r) is dict and set(r) == {"pos", "n", "sig"} and type(r["pos"]) is str and type(r["n"]) is int:
            if (
                (r["pos"] == "" or _POSITION.fullmatch(r["pos"]))
                and 1 <= r["n"] < PAGE_LIMIT
                and _SIG.fullmatch(str(r["sig"]))
            ):
                st["resume"] = dict(r)
        u = obj.get("unverifiable")
        if type(u) is dict and set(u) == set(st["unverifiable"]) and all(type(u[k]) is int and u[k] >= 0 for k in u):
            st["unverifiable"] = dict(u)
        else:
            lost = True
        seen = obj.get("overflow_seen")
        if (
            type(seen) is list
            and len(seen) <= OVERFLOW_SEEN_MAX
            and all(type(k) is str and _HEX16.fullmatch(k) for k in seen)
        ):
            st["overflow_seen"] = list(seen)
        elif "overflow_seen" in obj:
            lost = True
        po = obj.get("person_overflow")
        if (
            type(po) is dict
            and len(po) <= PERSON_OVERFLOW_PERSONS
            and all(type(k) is str and _HEX32.fullmatch(k) and type(v) is int and v > 0 for k, v in po.items())
        ):
            st["person_overflow"] = dict(po)
        elif "person_overflow" in obj:
            lost = True
        rows = obj.get("held")
        if type(rows) is list:
            good = [dict(h) for h in rows if _row_ok(h)]
            lost = lost or len(good) != len(rows) or len(good) > LEDGER_MAX
            st["held"] = good[:LEDGER_MAX]
        else:
            lost = True
        if mseq is None or mseq > st["ledger_seq"]:
            lost = True  # the mirror is missing, or AHEAD of the file: an older ledger (behind: a crash window, fine)
        return st, lost

    def _save(self, st: dict[str, Any]) -> None:
        """The ledger file, then its mirror, in ONE critical section (the state directory's lock): a crash between the
        two leaves the file ahead of the mirror, which is no loss; an older file next to a newer mirror is."""
        with self._state.lock:
            st["ledger_seq"] += 1
            self._state.write_json(FILE, st)
            self._state.write_json(MIRROR, {"seq": st["ledger_seq"]})

    # -- one step ------------------------------------------------------------------------------------------------------

    def step(self) -> None:
        """At most one page (see the module docstring). Re-raises the session's own errors."""
        gen = self._session.gen
        if gen is None:
            return  # no holder generation: nothing is read before this holder's own host/hold
        st, lost = self.load()
        led = Ledger(st, self._stronger)
        now = self._now_ms()
        dirty = False
        if lost:
            led.start_rebuild()  # a loss never shrinks the view: seeded + re-read, and status is not ok meanwhile
            self._restart(st)
            self._alarm_once(
                "unpublished_rebuilding", "the held ledger was lost or is older: rebuilding from the relay"
            )
            dirty = True
        if lost or not self._seeded:  # a loss mid-process is re-seeded too: the ledger is never smaller than HostPins
            self._seed(led, now)
            self._seeded = True
            dirty = True
        if st["gen"] != gen:
            # A new holder generation always starts over (the ledger is kept): whatever a crashed predecessor left
            # mid-scan, or an ack it still owed (the new full read re-reads and re-acks every position from the start).
            self._restart(st)
            st["gen"] = gen
            dirty = True
        elif st["scan"] is None and self._daily_due(st, now):
            self._restart(st)  # the daily full read from the start
            dirty = True
        if dirty:
            self._save(st)
        self._rejudge(st, led)  # the store's verified state alone removes rows; never behind the relay's back-off
        if self._mono() < self._not_before:
            return
        if st["ack"] is not None:
            self._send_ack(st)  # an ack owed from an earlier turn or run: retried first, nothing else this turn
            return
        notice = self._notice()
        if st["scan"] is not None:
            position, full = st["scan"] or None, True
        elif notice is not None and notice != st["notice"]:
            position, full = st["cursor"], False
        else:
            return
        led.note_first_scan(now)
        try:
            page = self._get(position)
        except RelayRefused as e:
            self._refused(e, st, led)
            return
        except TransportError as e:
            self._failed(st, led)
            self._backoff("unpublished_transport", type(e).__name__, BACKOFF_S)
            return
        items, cursor, more = page
        led.note_success()
        sig = hashlib.sha256(json.dumps(items, sort_keys=True, default=str).encode()).hexdigest()
        key = position or ""
        done = st["resume"]["n"] if st["resume"] and (st["resume"]["pos"], st["resume"]["sig"]) == (key, sig) else 0
        started, seen, page_unv = self._mono(), set(), 0
        try:
            for n, item in enumerate(items):
                if n < done:
                    continue  # judged durably in an earlier turn of this same page (checked by position and content)
                if n > done and self._mono() - started >= HOLD_BUDGET_S:
                    # the aux thread does not wait on the worker without bound; the rest carries to the next turn
                    st["resume"] = {"pos": key, "n": n, "sig": sig}
                    self._save(st)
                    return
                ident = json.dumps(item, sort_keys=True, default=str)
                if ident in seen:
                    continue  # a page that repeats an item costs one job, not one per copy
                seen.add(ident)
                result = self._hold(item)  # a store fault raises: no result for this item, nothing acked
                if self._ingest(led, item, result, now):
                    page_unv += result in ("dropped", "unverifiable")
                    self._save(st)  # durable before the next item: a failure at item k keeps 1..k-1
        except BaseException:
            self._not_before = self._mono() + BACKOFF_S  # a faulting store is not asked again on every tick
            raise
        if page_unv:
            self._alarm("unpublished_unverifiable", f"{page_unv} item(s) of this page could not be verified")
        st["resume"] = None
        if full:
            st["scan"] = cursor if more else None
            st["scan_pages"] += 1
            if not more:
                st["last_full_ms"] = now
                st["scan_pages"] = 0
                led.complete_scan(now)
                self._full_mono = self._mono()
        if not full or not more:
            st["cursor"] = cursor  # a read from the stored position only moves forward; a full read ends past all
        if not more:
            st["notice"] = notice  # this notice is answered: read again only when it changes
        if full and more and st["scan_pages"] > SCAN_LONG_PAGES:
            # a very long scan goes on from its stored position, slowly, with one alarm (not one per page)
            self._not_before = self._mono() + BACKOFF_S
            self._alarm_once("unpublished_scan_long", f"a full read passed {SCAN_LONG_PAGES} pages: continuing slowly")
        st["ack"] = cursor  # the ack is owed from here on, also across a restart, until the relay accepts it
        self._save(st)  # durable before the ack (obligation 4)
        self._rejudge(st, led, force=True)
        self._send_ack(st)

    def _ingest(self, led: Ledger, item: Mapping[str, Any], result: str, now: int) -> bool:
        """One item's result into the ledger (relay input: add, raise or count; never remove). Returns whether the
        record changed (and so must be persisted before the next item)."""
        if result == "held":
            row = row_of(item, now)
            standing = (
                self._judge([("d_" + row["device_id"], "p_" + row["person_id"], row["need"])])[0]
                if self._judge
                else "unknown"
            )
            if standing not in STANDINGS:
                raise ValueError("pair_standings answered no known standing")
            if standing == "settled":
                return False  # judged settled a moment after the store said held: no row needed
            row["state"] = standing
            out = led.add_or_raise(row)
            if out in ("added", "raised"):
                self._alarm("unpublished_held", f"d_{row['device_id']} of p_{row['person_id']}: reason {row['need']}")
            elif out == "overflow":
                self._alarm_once(
                    "unpublished_overflow",
                    "the held ledger, one person's share or the non-actionable budget is full: counted, not listed",
                )
            return out != "same"
        if result == "settled":
            return False
        if result in ("dropped", "unverifiable"):
            led.note_unverifiable(1, now)
            return True
        raise ValueError("note_forwarded_revocation answered no known result")

    def _seed(self, led: Ledger, now: int) -> None:
        """At every start the ledger takes in what the store already holds durably (union: adds, raises, never
        removes), so a lost or older file can never make it smaller than the host's own evidence."""
        if self._evidence is None:
            return
        for ev in self._evidence():
            led.add_or_raise(row_of(ev, now))

    def _rejudge(self, st: dict[str, Any], led: Ledger, *, force: bool = False) -> None:
        """Ask the store for the standing of every row and let :meth:`Ledger.settle_from_store` act on it. The rows go
        in chunks of ``JUDGE_BATCH`` (one worker job, so one store-lock hold, each), within the per-turn budget: the
        store lock is never held across the whole ledger and the aux thread never waits on it for long; what is left
        carries to the next turn. A store fault is no answer (nothing is removed); it backs off and reaches the aux
        guard."""
        rows = led.rows()
        if not rows or (
            not force and not self._woken and self._judged_at is not None
            and self._mono() - self._judged_at < JUDGE_EVERY_S
        ):  # fmt: skip
            return
        started, changed = self._mono(), False
        starts = list(range(0, len(rows), JUDGE_BATCH))
        first = self._judge_next if self._judge_next < len(starts) else 0
        for k in range(len(starts)):
            idx = (first + k) % len(starts)
            if k and self._mono() - started >= HOLD_BUDGET_S:
                self._judge_next = idx  # the rest of the ledger carries to the next turn
                if changed:
                    self._save(st)
                return
            chunk = rows[starts[idx] : starts[idx] + JUDGE_BATCH]
            pairs = [("d_" + r["device_id"], "p_" + r["person_id"], r["need"]) for r in chunk]
            try:
                standings = self._judge(pairs) if self._judge is not None else ["unknown"] * len(chunk)
            except BaseException:
                self._not_before = self._mono() + BACKOFF_S
                raise
            if (
                type(standings) is not list
                or len(standings) != len(chunk)
                or any(s not in STANDINGS for s in standings)
            ):
                raise ValueError("pair_standings answered no known standing")
            judged = [
                {"device_id": r["device_id"], "person_id": r["person_id"], "need": r["need"], "ver": r["ver"],
                 "standing": s}
                for r, s in zip(chunk, standings, strict=True)
            ]  # fmt: skip
            changed = led.settle_from_store(judged) or changed
        self._judged_at, self._woken, self._judge_next = self._mono(), False, 0
        if changed:
            self._save(st)

    def _alarm_once(self, code: str, detail: str) -> None:
        if code not in self._alarmed:
            self._alarmed.add(code)
            self._alarm(code, detail)

    def _daily_due(self, st: dict[str, Any], now: int) -> bool:
        """Obligation 1 ("at least once a day"): by the wall clock, by this process's monotonic clock (a clock set back
        cannot suppress it), and at once when the stored time lies in the future."""
        if now - st["last_full_ms"] >= FULL_EVERY_MS or now < st["last_full_ms"]:
            return True
        return self._full_mono is not None and self._mono() - self._full_mono >= FULL_EVERY_S

    @staticmethod
    def _restart(st: dict[str, Any]) -> None:
        """A full read from the start: nothing of an earlier scan (position, owed ack, partial page, counters) survives.
        The ledger does."""
        st.update(scan="", ack=None, resume=None, scan_pages=0, ack_refusals=0, scan_unv=False)

    def _failed(self, st: dict[str, Any], led: Ledger) -> None:
        led.note_failure()
        self._save(st)

    def _send_ack(self, st: dict[str, Any]) -> None:
        led = Ledger(st, self._stronger)
        try:
            self._ack(st["ack"])
        except RelayRefused as e:
            if e.status != 429 and not (e.status == 409 and e.code == "cursor_ahead"):
                st["ack_refusals"] += 1
                if st["ack_refusals"] >= ACK_REFUSALS_MAX:
                    # a permanently refused ack must not block reading for ever: drop it (the page is judged and
                    # durable; an ack only moves the notice), alarm once, and read on from the stored position
                    st["ack"], st["ack_refusals"] = None, 0
                    self._save(st)
                    self._alarm(
                        "unpublished_ack_dropped", f"the relay refused the ack {ACK_REFUSALS_MAX} times: dropped"
                    )
                    return
                self._save(st)
            self._refused(e, st, led)  # the retry-class table; the pending ack stays (except cursor_ahead)
            return
        except TransportError as e:
            self._failed(st, led)
            self._backoff("unpublished_transport", type(e).__name__, BACKOFF_S)
            return
        st["ack"], st["ack_refusals"] = None, 0  # only now: the relay accepted it
        self._save(st)

    # -- relay calls ---------------------------------------------------------------------------------------------------

    def _notice(self) -> str | None:
        n = self._notices()
        v = n.get("unpublished") if isinstance(n, Mapping) else None
        return v if type(v) is str and _POSITION.fullmatch(v) else None

    def _get(self, position: str | None) -> tuple[list[dict[str, Any]], str, bool]:
        q = f"limit={PAGE_LIMIT}" if position is None else f"cursor={_position(position)}&limit={PAGE_LIMIT}"
        obj = self._session.call(
            "GET", f"/v2/workspaces/{self.ws}/unpublished?{q}", "GET /v2/workspaces/{ws}/unpublished",
            holder=True, cap=PAGE_CAP, deadline_s=DEADLINE_S, keys={"items", "cursor", "more"},
        )  # fmt: skip
        items, cursor, more = obj["items"], obj["cursor"], obj["more"]
        if type(items) is not list or len(items) > PAGE_LIMIT or any(type(i) is not dict for i in items):
            raise RelayRefused(200, "malformed", route="GET /v2/workspaces/{ws}/unpublished")
        if type(more) is not bool or type(cursor) is not str or not _POSITION.fullmatch(cursor):
            raise RelayRefused(200, "malformed", route="GET /v2/workspaces/{ws}/unpublished")
        return items, cursor, more

    def _ack(self, cursor: str) -> None:
        self._session.call(
            "POST", f"/v2/workspaces/{self.ws}/unpublished/ack", "POST /v2/workspaces/{ws}/unpublished/ack",
            body={"cursor": _position(cursor)}, holder=True, deadline_s=DEADLINE_S, keys={"acked"},
        )  # fmt: skip

    def _refused(self, e: RelayRefused, st: dict[str, Any], led: Ledger) -> None:
        if e.status == 409 and e.code == "cursor_ahead":
            self._restart(st)  # restart from the start; the store deduplicates, the ledger is kept
            self._save(st)
            self._not_before = self._mono() + BACKOFF_S  # the relay's answer does not set the pace
            self._alarm("unpublished_cursor_ahead", "the relay refused the stored position: reading from the start")
            return
        self._failed(st, led)
        if e.status == 429:
            self._backoff("unpublished_rate_limited", safe_code(e.code), float(e.retry_after or RATE_LIMIT_S))
            return
        self._backoff("unpublished_refused", f"{safe_status(e.status)} {safe_code(e.code)}", BACKOFF_S)

    def _backoff(self, code: str, detail: str, seconds: float) -> None:
        self._not_before = self._mono() + max(1.0, seconds)
        if code not in self._alarmed:
            self._alarmed.add(code)
            self._alarm(code, detail)
        log.info("unpublished: %s (%s), next read in %.0f s", code, detail, seconds)


def _row_ok(h: object) -> bool:
    """A stored row (a row this module wrote always passes; a hand-edited or damaged file's row does not)."""
    if type(h) is not dict or set(h) != _ROW_KEYS:
        return False
    if type(h["device_id"]) is not str or not _HEX32.fullmatch(h["device_id"]):
        return False
    if type(h["person_id"]) is not str or not _HEX32.fullmatch(h["person_id"]):
        return False
    if h["need"] not in (*_REASONS, "unknown") or h["state"] not in STANDINGS[1:]:
        return False
    if type(h["ver"]) is not int or h["ver"] < 1 or type(h["at_ms"]) is not int:
        return False
    return type(h["digest"]) is str and bool(_DIGEST.fullmatch(h["digest"]))


def held_pairs(state_doc: object) -> list[dict[str, Any]]:
    """The held rows of an ``unpublished.json`` document (for the host's status), validated; ``[]`` otherwise."""
    if type(state_doc) is not dict or type(state_doc.get("held")) is not list:
        return []
    return [dict(h) for h in state_doc["held"] if _row_ok(h)][:LEDGER_MAX]


def ledger_view(state_doc: object, now_ms: int) -> dict[str, Any]:
    """What ``orch host status`` shows (11A.9): the held rows and every other reason the view is not ok, from an
    ``unpublished.json`` document: ``{held, unverifiable, overflow, rebuilding, stale, age_s, failed_turns}``."""
    doc = state_doc if type(state_doc) is dict else {}
    unv = doc.get("unverifiable")
    n = unv.get("n") if type(unv) is dict and type(unv.get("n")) is int else 0
    last = doc.get("last_complete_ms") if type(doc.get("last_complete_ms")) is int else 0
    failed = doc.get("failed_turns") if type(doc.get("failed_turns")) is int else 0
    overflow = doc.get("overflow") if type(doc.get("overflow")) is int else 0
    po = doc.get("person_overflow")
    persons = (
        {k: v for k, v in po.items() if type(k) is str and _HEX32.fullmatch(k) and type(v) is int}
        if type(po) is dict
        else {}
    )
    overflow += sum(persons.values())
    first = doc.get("first_scan_ms") if type(doc.get("first_scan_ms")) is int else 0
    # stale: failed turns, or no COMPLETE scan for 48 h (counted from the last one, else from the first attempt):
    # a scan that advances but never completes is stale too
    stale = (
        failed >= FAILED_TURNS_STALE
        or (last > 0 and now_ms - last > STALE_MS)
        or (last == 0 and first > 0 and now_ms - first > STALE_MS)
    )
    rows = [{**r, "actionable": r["state"] in ("revoke", "stronger")} for r in held_pairs(doc)]
    return {
        "held": rows, "unverifiable": n, "overflow": overflow, "rebuilding": doc.get("rebuilding") is True,
        "stale": stale, "age_s": (now_ms - last) // 1000 if last else None, "failed_turns": failed,
        "overflow_persons": sorted(persons)[:5],
    }  # fmt: skip
