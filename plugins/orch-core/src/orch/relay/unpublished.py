"""R4: the unpublished-evidence consumer (contract 11A.9 "Unpublished evidence", relay protocol §19.5 P3-D31).

The relay serves, as a derived view, the revocations of this workspace's ever-devices that its host has not
published yet (``GET /v2/workspaces/{ws}/unpublished``). The host MUST read them (obligation 1), verify each item on
its own (2), check its own association (3), store it durably and only then ack (4), and publish only through F1
§5.3 (5): the host never appends ``device.revoked`` or a restore by itself.

:class:`UnpublishedConsumer` is R5's :class:`orch.relay.worker.Unpublished` seam: :meth:`~UnpublishedConsumer.step`
runs on the aux thread after the lease renewal, at most ONE page per turn (one GET of at most 10 s, then the
holds as worker jobs, then the durable position, then one ack of at most 10 s):

* **When it reads.** From the start (no ``cursor``) after every new holder ``gen``, after ``cursor_ahead`` and at
  least once a day (``FULL_EVERY_MS``), page by page until ``more`` is false; otherwise from the stored position while
  the last poll's ``notices.unpublished`` is non-null and differs from the notice a completed read already answered.
* **Verify and hold.** Every item goes to ``hold(item)``, the host's worker job around
  ``Store.note_forwarded_revocation`` (H12b): it derives every fact from the signed objects (``pk_pub``, the
  certificate at its own ``created_ms``, the revocation, the digest), never from the item's outer fields, and checks
  the association under the store lock: ``held``, ``pending`` or ``dropped``. Nothing here trusts or rewrites an item.
  A store fault (an exception) means no result: nothing after it is stored or acked; the page is read again.
* **Order.** All items of the page have a result (``held``/``pending`` are durable in ``HostPins``), then the new
  position is written atomically (``unpublished.json``), and only then the page's ``cursor`` is acked
  (``POST unpublished/ack {cursor}``, exactly that field: the relay refuses any other). An ack is not publication.
  The ack is owed (``ack`` in the file) from the write until the relay accepted it: a failed one is retried first,
  later or after a restart.
* **Held evidence.** The host has no host-originated ``needs`` items yet (issue #572), so a ``held`` result raises
  the relay alarm ``unpublished_held`` naming the pair, and the pair joins ``held`` in ``unpublished.json``
  (bounded; every completed full read replaces it with the pairs that read held), which ``host.status.relay`` and
  ``orch host status`` show to the owner with what to do. THE INVARIANT: an item leaves that view ONLY through
  :meth:`UnpublishedConsumer._settle`, on the store's ``settled`` answer (a verified revocation of the exact pair at
  least as strong as the item's reason, judged inside ``note_forwarded_revocation``); every other answer about held
  evidence, ``weaker`` included, is a visible row and an alarm, whatever the relay's fields look like (they are
  reduced to closed forms, never rejected).
  SEAM (#572): promote this to a host-originated ``needs`` item ("publish this revocation") when those exist.
* **Failures.** ``cursor_ahead`` (a bug, a forgery or a relay restored from backup): restart from the start (the
  store deduplicates per pair). 429: wait ``Retry-After`` (else 60 s). Any other refusal or transport error: one
  alarm per class, 30 s back-off. The session's own errors (``Fenced``, ``SessionError``) are re-raised: the aux
  thread's guard stops the worker. Unverified data never changes host state.

Nothing here touches the store directly (``orch.store`` is reached only through ``hold``) or any key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from .http import TransportError
from .retry import RelayRefused
from .session import Session
from .state import StateDir, StateError

__all__ = ["FILE", "FULL_EVERY_MS", "HELD_MAX", "UnpublishedConsumer", "held_pairs"]

log = logging.getLogger("orch.relay.unpublished")

FILE = "unpublished.json"
FULL_EVERY_MS = 24 * 3600 * 1000  # obligation 1: at least once a day from the start
PAGE_LIMIT = 64  # the relay's default and maximum
PAGE_CAP = 4 << 20  # 11A.7: R4 pages read at most 4 MiB (the relay sends at most 256 KiB)
DEADLINE_S = 10.0  # 11A.7: 10 s per relay call on the aux thread
BACKOFF_S = 30.0
RATE_LIMIT_S = 60.0
SCAN_LONG_PAGES = 1500  # a full read beyond this many pages is slowed to one page per BACKOFF_S and alarmed once; it
# NEVER restarts from the start or gives up (D4): availability of the host is bounded by the rate, never by reach
HOLD_BUDGET_S = 20.0  # the aux thread waits at most this long on the worker's jobs per turn (F6)
FULL_EVERY_S = FULL_EVERY_MS / 1000.0
HELD_MAX = 256  # the held pairs shown to the owner; the store keeps the evidence itself (HostPins)
_POSITION = re.compile(r"[A-Za-z0-9_-]{1,512}")  # an opaque b64u position, put into a query string unescaped
_HEX32 = re.compile(r"[0-9a-f]{32}")
_ROW_KEYS = {"device_id", "person_id", "reason", "digest", "at_ms"}
_REASONS = ("compromised", "lost", "retired")  # the revokeReason enum; any other text is shown as "unknown"
ACK_REFUSALS_MAX = 5  # D5: a pending ack the relay refuses this often (non-transient) is dropped, once alarmed
_SIG = re.compile(r"[0-9a-f]{64}")


def _position(value: object) -> str:
    if type(value) is not str or not _POSITION.fullmatch(value):
        raise ValueError("position")
    return value


def _empty() -> dict[str, Any]:
    return {"v": 1, "cursor": None, "scan": None, "gen": None, "last_full_ms": 0, "notice": None, "held": [],
            "scan_held": [], "ack": None, "resume": None, "scan_pages": 0, "ack_refusals": 0}  # fmt: skip


class UnpublishedConsumer:
    """One workspace's R4 consumer. ``hold(item)`` is the host's worker job around the store's one answer, ``"held" |
    "weaker" | "settled" | "pending" | "dropped"`` (``settled``: the workspace's own state already revokes the pair
    at least as strongly; it raises on a store fault); ``notices()`` returns the last poll's ``notices`` object (or
    ``None``); ``alarm(code, detail)`` is the worker's alarm sink."""

    def __init__(
        self,
        *,
        workspace_id: str,
        session: Session,
        state: StateDir,
        hold: Callable[[Mapping[str, Any]], str],
        notices: Callable[[], Mapping[str, Any] | None],
        alarm: Callable[[str, str], None],
        now_ms: Callable[[], int],
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(workspace_id) is not str or not _HEX32.fullmatch(workspace_id):
            raise ValueError("workspace id")
        self.ws = workspace_id
        self._session, self._state, self._hold = session, state, hold
        self._notices, self._alarm, self._now_ms, self._mono = notices, alarm, now_ms, mono
        self._not_before = 0.0
        self._alarmed: set[str] = set()
        self._full_mono: float | None = None  # when this process last completed a full read (the wall clock may move)

    def __repr__(self) -> str:
        return f"UnpublishedConsumer(workspace={self.ws})"

    # -- the durable position ----------------------------------------------------------------------------------------

    def load(self) -> dict[str, Any]:
        """``unpublished.json`` (11A.3), or a fresh record. A record that does not parse starts over from the start
        (fail closed: the relay re-serves everything, the store deduplicates)."""
        try:
            obj = self._state.read_json(FILE)
        except StateError:
            obj = None
        st = _empty()
        if type(obj) is not dict:
            return st
        for key in ("cursor", "scan", "notice", "ack"):
            v = obj.get(key)
            if v is None or (type(v) is str and (v == "" or _POSITION.fullmatch(v))):
                st[key] = v
        if type(obj.get("gen")) is int and obj["gen"] >= 0:
            st["gen"] = obj["gen"]
        if type(obj.get("last_full_ms")) is int and obj["last_full_ms"] >= 0:
            st["last_full_ms"] = obj["last_full_ms"]
        r = obj.get("resume")
        if type(r) is dict and set(r) == {"pos", "n", "sig"} and type(r["pos"]) is str and type(r["n"]) is int:
            if (
                (r["pos"] == "" or _POSITION.fullmatch(r["pos"]))
                and 1 <= r["n"] < PAGE_LIMIT
                and _SIG.fullmatch(str(r["sig"]))
            ):
                st["resume"] = dict(r)
        for key in ("scan_pages", "ack_refusals"):
            if type(obj.get(key)) is int and 0 <= obj[key] <= 1 << 20:
                st[key] = obj[key]
        for key in ("held", "scan_held"):
            if type(obj.get(key)) is list:
                st[key] = [h for h in obj[key] if _held_ok(h)][-HELD_MAX:]
        return st

    def _save(self, st: dict[str, Any]) -> None:
        self._state.write_json(FILE, st)

    # -- one step ------------------------------------------------------------------------------------------------------

    def step(self) -> None:
        """At most one page (see the module docstring). Re-raises the session's own errors."""
        if self._mono() < self._not_before:
            return
        gen = self._session.gen
        if gen is None:
            return  # no holder generation: nothing is read before this holder's own host/hold
        st = self.load()
        now = self._now_ms()
        if st["gen"] != gen:
            # A new holder generation always starts over, whatever a crashed predecessor left mid-scan (the scan
            # position, its held pairs) or still owed (a pending ack: the new full read re-reads and re-acks every
            # position from the start, so an older generation's ack is dropped, never sent).
            self._restart(st)
            st["gen"] = gen
            self._save(st)
        elif st["scan"] is None and self._daily_due(st, now):
            self._restart(st)  # the daily full read from the start
            self._save(st)
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
        try:
            page = self._get(position)
        except RelayRefused as e:
            self._refused(e, st)
            return
        except TransportError as e:
            self._backoff("unpublished_transport", type(e).__name__, BACKOFF_S)
            return
        items, cursor, more = page
        sig = hashlib.sha256(json.dumps(items, sort_keys=True, default=str).encode()).hexdigest()
        key = position or ""
        done = st["resume"]["n"] if st["resume"] and (st["resume"]["pos"], st["resume"]["sig"]) == (key, sig) else 0
        started, seen = self._mono(), set()
        try:
            for n, item in enumerate(items):
                if n < done:
                    continue  # held durably in an earlier turn of this same page (checked by position and content)
                if n > done and self._mono() - started >= HOLD_BUDGET_S:
                    # F6: the aux thread does not wait on the worker without bound; the rest carries to the next turn
                    st["resume"] = {"pos": key, "n": n, "sig": sig}
                    self._save(st)
                    return
                ident = json.dumps(item, sort_keys=True, default=str)
                if ident in seen:
                    continue  # a page that repeats an item costs one job, not one per copy
                seen.add(ident)
                # a store fault raises: no result, nothing stored, nothing acked
                self._settle(st, item, self._hold(item), full)
        except BaseException:
            self._not_before = self._mono() + BACKOFF_S  # F4: a faulting store is not asked again on every tick
            raise
        st["resume"] = None
        if full:
            st["scan"] = cursor if more else None
            st["scan_pages"] += 1
            if not more:  # a completed full read: the held list is exactly what the relay still serves as held
                st["last_full_ms"], st["held"], st["scan_held"] = now, st["scan_held"], []
                st["scan_pages"] = 0
                self._full_mono = self._mono()
        if not full or not more:
            st["cursor"] = cursor  # a read from the stored position only moves forward; a full read ends past all
        if not more:
            st["notice"] = notice  # this notice is answered: read again only when it changes
        if full and more and st["scan_pages"] > SCAN_LONG_PAGES:
            # D4: a very long scan goes on from its stored position, slowly, with one alarm (not one per page)
            self._not_before = self._mono() + BACKOFF_S
            if "scan_long" not in self._alarmed:
                self._alarmed.add("scan_long")
                self._alarm("unpublished_scan_long", f"a full read passed {SCAN_LONG_PAGES} pages: continuing slowly")
        st["ack"] = cursor  # the ack is owed from here on, also across a restart, until the relay accepts it
        self._save(st)  # durable before the ack (obligation 4)
        self._send_ack(st)

    def _daily_due(self, st: dict[str, Any], now: int) -> bool:
        """Obligation 1 ("at least once a day"): by the wall clock, by this process's monotonic clock (a clock set back
        cannot suppress it), and at once when the stored time lies in the future."""
        if now - st["last_full_ms"] >= FULL_EVERY_MS or now < st["last_full_ms"]:
            return True
        return self._full_mono is not None and self._mono() - self._full_mono >= FULL_EVERY_S

    @staticmethod
    def _restart(st: dict[str, Any]) -> None:
        """A full read from the start: nothing of an earlier scan (position, held pairs, owed ack, partial page,
        counters) survives."""
        st.update(scan="", scan_held=[], ack=None, resume=None, scan_pages=0, ack_refusals=0)

    def _send_ack(self, st: dict[str, Any]) -> None:
        try:
            self._ack(st["ack"])
        except RelayRefused as e:
            if e.status != 429 and not (e.status == 409 and e.code == "cursor_ahead"):
                st["ack_refusals"] += 1
                if st["ack_refusals"] >= ACK_REFUSALS_MAX:
                    # D5: a permanently refused ack must not block reading for ever: drop it (the page is settled
                    # and durable; an ack only moves the notice), alarm once, and read on from the stored position
                    st["ack"], st["ack_refusals"] = None, 0
                    self._save(st)
                    self._alarm(
                        "unpublished_ack_dropped", f"the relay refused the ack {ACK_REFUSALS_MAX} times: dropped"
                    )
                    return
                self._save(st)
            self._refused(e, st)  # the retry-class table; the pending ack stays (except cursor_ahead)
            return
        except TransportError as e:
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

    def _refused(self, e: RelayRefused, st: dict[str, Any]) -> None:
        if e.status == 409 and e.code == "cursor_ahead":
            self._restart(st)  # obligation 1: restart from the start; the store deduplicates
            self._save(st)
            self._not_before = self._mono() + BACKOFF_S  # F4: the relay's answer does not set the pace
            self._alarm("unpublished_cursor_ahead", "the relay refused the stored position: reading from the start")
            return
        if e.status == 429:
            self._backoff("unpublished_rate_limited", str(e.code), float(e.retry_after or RATE_LIMIT_S))
            return
        self._backoff("unpublished_refused", f"{e.status} {e.code}", BACKOFF_S)

    def _backoff(self, code: str, detail: str, seconds: float) -> None:
        self._not_before = self._mono() + max(1.0, seconds)
        if code not in self._alarmed:
            self._alarmed.add(code)
            self._alarm(code, detail)
        log.info("unpublished: %s (%s), next read in %.0f s", code, detail, seconds)

    def _settle(self, st: dict[str, Any], item: Mapping[str, Any], result: str, full: bool) -> None:
        """THE invariant of R4 (11A.9): an item leaves the owner's view (held row, alarm, status not-ok) ONLY here,
        and ONLY when the store answered ``settled`` (inside ``note_forwarded_revocation``, under its one lock: the
        workspace holds a verified revocation of exactly this pair at least as strong as the item's). Every other
        answer about held evidence (``held``, ``weaker``) creates a visible row, whatever the relay's fields look like
        (they are sanitised, never rejected); ``pending`` and ``dropped`` are not held evidence; anything else is an
        error: no result, no ack."""
        if result == "settled":
            pair = (item.get("device_id"), item.get("person_id"))
            for key in ("held", "scan_held"):
                st[key] = [h for h in st[key] if (h["device_id"], h["person_id"]) != pair]
        elif result in ("held", "weaker"):
            self._held(st, item, full, weaker=result == "weaker")
        elif result not in ("pending", "dropped"):
            raise ValueError("note_forwarded_revocation answered no known result")

    def _held(self, st: dict[str, Any], item: Mapping[str, Any], full: bool, *, weaker: bool) -> None:
        """The interim owner notice (#572): the pair joins ``held`` and the alarm names it. Never rejects and never
        returns before the row exists: every field is reduced to a closed form (a hex id, a reason of the enum or
        ``unknown``, a digest shape), so the relay's text can only ever appear as such a token."""
        prov = item.get("provenance")
        digest = prov.get("digest") if isinstance(prov, Mapping) else None
        row = {
            "device_id": _hex(item.get("device_id")), "person_id": _hex(item.get("person_id")),
            "reason": _reason(item.get("reason")), "digest": _digest(digest), "at_ms": self._now_ms(), "weaker": weaker,
        }  # fmt: skip
        for key in ("held", "scan_held") if full else ("held",):
            rest = [h for h in st[key] if (h["device_id"], h["person_id"]) != (row["device_id"], row["person_id"])]
            st[key] = [*rest, row][-HELD_MAX:]  # one row per pair
        todo = (
            f"sign a device.revoked with the stronger reason {row['reason']} (F1 §5.3)"
            if weaker
            else f"sign a device.revoked, reason {row['reason']} (F1 §5.3)"
        )
        self._alarm("unpublished_held", f"d_{row['device_id']} of p_{row['person_id']}: {todo}")


def _hex(v: object) -> str:
    return v if type(v) is str and _HEX32.fullmatch(v) else "0" * 32


def _reason(v: object) -> str:
    return v if type(v) is str and v in _REASONS else "unknown"


def _digest(v: object) -> str:
    return v if type(v) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", v) else "sha256:" + "0" * 64


def _held_ok(h: object) -> bool:
    """A stored row (a row this module wrote always passes; a hand-edited or damaged file's row does not)."""
    if type(h) is not dict or set(h) not in (_ROW_KEYS, _ROW_KEYS | {"weaker"}):
        return False
    if "weaker" in h and type(h["weaker"]) is not bool:
        return False
    if type(h["device_id"]) is not str or not _HEX32.fullmatch(h["device_id"]):
        return False
    if type(h["person_id"]) is not str or not _HEX32.fullmatch(h["person_id"]):
        return False
    if h["reason"] not in (*_REASONS, "unknown"):
        return False
    return type(h["digest"]) is str and len(h["digest"]) <= 80 and type(h["at_ms"]) is int


def held_pairs(state_doc: object) -> list[dict[str, Any]]:
    """The held pairs of an ``unpublished.json`` document (for the host's status), validated; ``[]`` otherwise."""
    if type(state_doc) is not dict or type(state_doc.get("held")) is not list:
        return []
    return [dict(h) for h in state_doc["held"] if _held_ok(h)][-HELD_MAX:]
