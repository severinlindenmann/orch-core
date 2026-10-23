"""The primary's durable per-request journal (orch-v2-host 11A.15.9 "The primary's durable per-request journal", N7).

One state machine per cert request, each transition written **before** its effect:

* ``verified``: the immutable verified request bytes (``{o, sig}``), after ``check_cert_request`` and
  ``open_cert_request`` passed;
* ``challenged``: the immutable challenge bytes (with ``primary_nonce``), written before the challenge POST;
* ``decided``: either ``sign`` with the exact certificate bytes or ``reject`` with the exact rejection bytes, written
  before the upload;
* ``uploaded``: the relay's 2xx was seen; ``upload_failed``: a permanent refusal of the upload (``expired``,
  ``not_found``, ``cert_conflict``), or a decision the owner closed (review of #557, Codex P2);
* terminal without a decision: ``expired``, ``cancelled``, ``superseded``, and ``refused`` (a request id that failed
  §8.4 step 2.4 or later: burned, never judged fresh again; no relay bytes are kept for it).

Storage: ``<state>/person/primary/requests/<request_id>.json`` (:func:`journal_dir`), a private directory opened
through :class:`orch.wsproto.state.StateDir` (0700, files 0600, owned by this user, ``O_NOFOLLOW`` against the directory
fd, temp file + ``fsync`` + ``rename`` + directory ``fsync``; a symlink, a foreign owner or a wider mode is refused,
never repaired). **Concurrency:** every transaction holds the directory's exclusive ``flock`` (``StateDir`` takes it
for its lifetime) from the read of the current state to the durable write, so two owner clients can never both act on
one request; a second one waits up to ``lock_wait_s`` and then fails ``journal.busy``. **JUDGEMENT CALL:** the lock is
the directory's own ``flock`` (as every R1 state directory), not a separate ``.lock`` file: same exclusion, one fewer
file to check.

**Bounded, never a lockout (review of the first push, orch-52).** A hostile relay can at most make one request fail
closed: an unreadable record is treated as burned and replaced by a tombstone, not fatal; every record is capped at
:data:`MAX_RECORD`; :meth:`JournalView.prune` (run by every poll) ends live records past their expiry as ``expired``
(never a live tombstone), deletes tombstones after expiry + 14 days, and only live or unexpired records count against
:data:`MAX_ENTRIES`: a new id deletes the oldest expired terminal record on demand, and only a journal full of live or
unexpired requests refuses *new* ids (``journal.full``), recovering as they expire (review of #557).
The ``flock`` is released on every exit of a transaction (``finally``) and by the kernel when the process dies.
Fatal only: the directory itself failing its checks (custody tampering, not relay-reachable).

**Same request id with other bytes:** refused against the stored ``verified`` bytes with
:class:`RequestConflict` (alarm ``primary_request_conflict``). **Retention:** a record is pruned to a tombstone
(``{request_id, workspace_id, state, expires_ms}``) after its ``expires_ms`` plus 7 days, so the id stays terminal.

:meth:`JournalView.cert_journal` derives the pure :class:`orch.identity.certreq.CertJournal` the §8.4 step 2 checks
read: ``seen`` is every id here, ``open`` the live (``verified``/``challenged``) requests, ``closed`` the decided and
terminal ones.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from orch import canon
from orch.identity import certreq
from orch.wsproto.state import StateDir, StateError

__all__ = [
    "LIVE",
    "RETENTION_MS",
    "STATES",
    "Entry",
    "JournalError",
    "JournalView",
    "PrimaryJournal",
    "RequestConflict",
    "journal_dir",
]

STATES = (
    "verified", "challenged", "decided", "uploaded", "upload_failed", "expired", "cancelled", "superseded", "refused",
)  # fmt: skip
DECIDED = frozenset({"decided", "uploaded", "upload_failed"})
LIVE = frozenset({"verified", "challenged"})
RETENTION_MS = 7 * 24 * 3600 * 1000
_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"verified", "refused"}),
    "verified": frozenset({"challenged", "decided", "expired", "cancelled", "superseded"}),
    "challenged": frozenset({"decided", "expired", "cancelled", "superseded"}),
    "decided": frozenset({"uploaded", "upload_failed"}),
}
_FIELDS = frozenset(
    {"v", "request_id", "workspace_id", "expires_ms", "state", "request", "challenge", "decision", "decided",
     "tombstone"}
)  # fmt: skip
_HEX32 = re.compile(r"[0-9a-f]{32}")
_FILE = re.compile(r"([0-9a-f]{32})\.json")
MAX_ENTRIES = 4096  # soft cap: prune evicts expired terminal records above it; a new id above it is journal.full
HARD_MAX_FILES = 65_536  # only local tampering reaches this (the relay cannot mint WSK-signed ids)
MAX_RECORD = 32 * 1024  # one record (request, challenge, decision) is about 4 KiB
TOMBSTONE_MS = 2 * RETENTION_MS  # a tombstone is deleted after expiry + 14 days: check_cert_request refuses it anyway
_UNKNOWN_WS = "00" * 16
log = logging.getLogger("orch.relay_owner.journal")


class JournalError(Exception):
    """The journal cannot be used: ``code`` is ``journal.corrupt``, ``journal.busy``, ``journal.transition`` or
    ``primary_request_conflict``. Messages carry ids and state names only."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class RequestConflict(JournalError):
    """A request id the journal holds, presented with other bytes (alarm ``primary_request_conflict``)."""

    def __init__(self, request_id: str) -> None:
        super().__init__("primary_request_conflict", f"request {request_id} differs from the stored verified bytes")
        self.request_id = request_id


@dataclass(frozen=True)
class Entry:
    """One request's record. ``request``, ``challenge`` and ``decided`` are stored as ``cj`` bytes (immutable once
    written; every read is a fresh copy)."""

    request_id: str
    workspace_id: str
    expires_ms: int
    state: str
    request_cj: bytes | None = field(default=None, repr=False)
    challenge_cj: bytes | None = field(default=None, repr=False)
    decision: str | None = None
    decided_cj: bytes | None = field(default=None, repr=False)
    tombstone: bool = False

    def __post_init__(self) -> None:
        ok = (
            type(self.request_id) is str
            and _HEX32.fullmatch(self.request_id)
            and type(self.workspace_id) is str
            and _HEX32.fullmatch(self.workspace_id)
            and type(self.expires_ms) is int
            and 0 <= self.expires_ms <= certreq.MAX_SAFE_INT
            and self.state in STATES
            and self.decision in (None, "sign", "reject")
            and all(x is None or type(x) is bytes for x in (self.request_cj, self.challenge_cj, self.decided_cj))
        )
        if not ok:
            raise JournalError("journal.corrupt", "a record field is malformed")
        if self.tombstone:
            if self.state in LIVE:
                raise JournalError("journal.corrupt", "a live record is never a tombstone")
            if (self.request_cj, self.challenge_cj, self.decided_cj) != (None, None, None):
                raise JournalError("journal.corrupt", "a tombstone keeps no bytes")
            return
        need_request = self.state not in ("refused",)
        if need_request and self.request_cj is None:
            raise JournalError("journal.corrupt", f"a {self.state} record without its request")
        if self.state == "challenged" and self.challenge_cj is None:
            raise JournalError("journal.corrupt", "a challenged record without its challenge")
        if (self.state in DECIDED) != (self.decision is not None and self.decided_cj is not None):
            raise JournalError("journal.corrupt", "a decision without its bytes, or bytes without a decision")

    @property
    def live(self) -> bool:
        return self.state in LIVE

    @property
    def request(self) -> dict[str, Any] | None:
        return None if self.request_cj is None else canon.loads_strict(self.request_cj)

    @property
    def challenge(self) -> dict[str, Any] | None:
        return None if self.challenge_cj is None else canon.loads_strict(self.challenge_cj)

    @property
    def decided(self) -> dict[str, Any] | None:
        return None if self.decided_cj is None else canon.loads_strict(self.decided_cj)

    def to_json(self) -> bytes:
        def obj(b: bytes | None) -> Any:
            return None if b is None else canon.loads_strict(b)

        return canon.dumps(
            {
                "v": 1, "request_id": self.request_id, "workspace_id": self.workspace_id,
                "expires_ms": self.expires_ms, "state": self.state, "request": obj(self.request_cj),
                "challenge": obj(self.challenge_cj), "decision": self.decision, "decided": obj(self.decided_cj),
                "tombstone": self.tombstone,
            }
        )  # fmt: skip

    @classmethod
    def from_json(cls, raw: bytes) -> Entry:
        try:
            d = canon.loads_strict(raw)
        except canon.JcsError:
            raise JournalError("journal.corrupt", "a record is not canonical JSON") from None
        if type(d) is not dict or set(d) != _FIELDS or d["v"] != 1 or type(d["tombstone"]) is not bool:
            raise JournalError("journal.corrupt", "a record has the wrong field set")

        def cj(x: Any, what: str) -> bytes | None:
            if x is None:
                return None
            if type(x) is not dict:
                raise JournalError("journal.corrupt", f"{what} is not an object")
            return canon.dumps(x)

        return cls(
            request_id=d["request_id"], workspace_id=d["workspace_id"], expires_ms=d["expires_ms"],
            state=d["state"], request_cj=cj(d["request"], "request"), challenge_cj=cj(d["challenge"], "challenge"),
            decision=d["decision"], decided_cj=cj(d["decided"], "decided"), tombstone=d["tombstone"],
        )  # fmt: skip


def journal_dir(state_dir: str | os.PathLike[str]) -> Path:
    """``<person state>/person/primary/requests`` (next to the primary's ``keys``)."""
    from orch.custody.primary import keys_dir

    return keys_dir(state_dir).parent / "requests"


class JournalView:
    """The journal inside one transaction (the ``flock`` is held). Reads see this transaction's writes.

    **One bad record never locks the owner out** (review of the pushed journal, orch-52): a record that does not read
    (not canonical JSON, the wrong fields, over :data:`MAX_RECORD`, a symlink, a wider mode, another owner, another
    request's id) is logged, counted in :attr:`corrupt` and treated as **burned** (seen and closed: that id is never
    acted on), and :meth:`prune` replaces it with a ``refused`` tombstone. Only the directory itself failing its
    checks (or more than :data:`HARD_MAX_FILES` names, local tampering) is fatal."""

    def __init__(self, state: StateDir) -> None:
        self._state = state
        self._entries: dict[str, Entry] = {}
        self.corrupt: set[str] = set()
        self._now: int | None = None  # the clock of the last prune (capacity counts records unexpired at it)
        names = sorted(os.listdir(state.path))
        if len(names) > HARD_MAX_FILES:
            raise JournalError("journal.corrupt", "the journal directory holds too many files")
        for name in names:
            m = _FILE.fullmatch(name)
            if m is None:
                continue
            rid = m.group(1)
            try:
                raw = state.read(name)
                if raw is None:  # removed between the listing and the read
                    continue
                if len(raw) > MAX_RECORD:
                    raise JournalError("journal.corrupt", "record too large")
                entry = Entry.from_json(raw)
                if entry.request_id != rid:
                    raise JournalError("journal.corrupt", "the record names another request")
            except (StateError, JournalError):
                log.warning("primary journal: record %s is unreadable; treated as burned", rid)
                self.corrupt.add(rid)
                continue
            self._entries[rid] = entry

    def get(self, request_id: str) -> Entry | None:
        return self._entries.get(request_id)

    def entries(self) -> list[Entry]:
        return [self._entries[k] for k in sorted(self._entries)]

    def cert_journal(self, *, excluding: str | None = None) -> certreq.CertJournal:
        """The pure journal of the §8.4 step 2 checks; ``excluding`` leaves one id out (a resume re-checks a request
        the journal already holds, against everything else)."""
        es = [e for e in self._entries.values() if e.request_id != excluding]
        bad = self.corrupt - {excluding}
        return certreq.CertJournal(
            seen=frozenset(e.request_id for e in es) | bad,
            open=tuple((e.workspace_id, e.request_id, e.expires_ms) for e in es if e.live),
            closed=frozenset(e.request_id for e in es if not e.live and e.state != "refused") | bad,
        )

    def put(self, entry: Entry) -> Entry:
        """Write ``entry`` durably, as a transition from the current record (or a creation). A ``verified`` record
        for an id the journal holds is :class:`RequestConflict` unless its bytes are the stored ones (then the stored
        record is returned and nothing is written). Immutable parts never change."""
        if entry.request_id in self.corrupt:
            raise RequestConflict(entry.request_id)
        if len(entry.to_json()) > MAX_RECORD:
            raise JournalError("journal.too_large", f"request {entry.request_id}: record over the cap")
        cur = self._entries.get(entry.request_id)
        if cur is None:
            self._make_room()
        if entry.state == "verified" and cur is not None:
            if cur.request_cj is None or cur.request_cj != entry.request_cj:
                raise RequestConflict(entry.request_id)
            return cur
        frm = None if cur is None else cur.state
        if entry.state not in _TRANSITIONS.get(frm, frozenset()) or entry.tombstone:
            raise JournalError("journal.transition", f"request {entry.request_id}: {frm} -> {entry.state}")
        if cur is not None:
            same = (cur.workspace_id, cur.expires_ms, cur.request_cj) == (
                entry.workspace_id, entry.expires_ms, entry.request_cj
            )  # fmt: skip
            kept = cur.challenge_cj is None or cur.challenge_cj == entry.challenge_cj
            decided = cur.decided_cj is None or (cur.decided_cj, cur.decision) == (entry.decided_cj, entry.decision)
            if not (same and kept and decided):
                raise JournalError("journal.transition", f"request {entry.request_id}: stored bytes would change")
        self._write(entry)
        return entry

    def _expired_terminal(self, now_ms: int) -> list[Entry]:
        """Records capacity pressure may delete: not live, expired, and not a ``decided`` record whose upload is
        still pending (``finish_uploads`` resends it; Opus delta D2 on #557). A tombstoned ``decided`` record keeps no
        bytes and is never resent, so it is evictable (Codex, same round). Oldest first."""
        return sorted(
            (
                e
                for e in self._entries.values()
                if not e.live and (e.state != "decided" or e.tombstone) and now_ms >= e.expires_ms
            ),
            key=lambda e: (e.expires_ms, e.request_id),
        )

    def _make_room(self) -> None:
        """Capacity for one new id (review of #557): only live, unexpired or still-to-upload records
        count against :data:`MAX_ENTRIES`; when the files reach it, the oldest evictable record is deleted on demand,
        never a ``decided`` one (with nothing evictable: ``journal.full``)."""
        now = self._now
        old = self._expired_terminal(now) if now is not None else []
        if len(self._entries) - len(old) >= MAX_ENTRIES:
            raise JournalError("journal.full", "the primary journal is full of unexpired requests")
        while len(self._entries) >= MAX_ENTRIES and old:
            self._delete(old.pop(0).request_id)

    def prune(self, now_ms: int) -> int:
        """Bound the journal (run at the start of every poll): an unreadable record becomes a ``refused`` tombstone
        (expiry = now); records past ``expires_ms + 7 days`` become tombstones (id, workspace, final state, expiry);
        tombstones past ``expires_ms + 14 days`` are deleted (``check_cert_request`` refuses such an id as
        ``expired`` anyway); above :data:`MAX_ENTRIES`, expired terminal records are evicted oldest first. A live or
        unexpired record is never evicted (it may still be re-presented). Returns the number of records changed."""
        self._now = now_ms
        n = 0
        for e in list(self._entries.values()):  # a live record past its expiry ends expired first (N1)
            if e.live and now_ms >= e.expires_ms:
                self._write(replace(e, state="expired"))
                n += 1
        for rid in sorted(self.corrupt):
            self.corrupt.discard(rid)
            self._write(Entry(rid, _UNKNOWN_WS, now_ms, "refused", tombstone=True))
            n += 1
        for e in list(self._entries.values()):
            if e.tombstone and now_ms > e.expires_ms + TOMBSTONE_MS:
                self._delete(e.request_id)
                n += 1
            elif not e.tombstone and now_ms > e.expires_ms + RETENTION_MS:
                self._write(replace(e, request_cj=None, challenge_cj=None, decided_cj=None, tombstone=True))
                n += 1
        if len(self._entries) > MAX_ENTRIES:
            old = self._expired_terminal(now_ms)
            for e in old[: len(self._entries) - MAX_ENTRIES]:
                self._delete(e.request_id)
                n += 1
        return n

    def _delete(self, request_id: str) -> None:
        try:
            self._state.remove(f"{request_id}.json")  # relative to the locked directory fd (N2)
        except (OSError, StateError) as e:
            raise JournalError("journal.corrupt", f"record {request_id}: cannot delete ({e})") from None
        self._entries.pop(request_id, None)

    def _write(self, entry: Entry) -> None:
        try:
            self._state.write(f"{entry.request_id}.json", entry.to_json())
        except StateError as e:
            raise JournalError("journal.corrupt", f"record {entry.request_id}: {e}") from None
        self._entries[entry.request_id] = entry


class PrimaryJournal:
    """The journal directory. Use one :meth:`transaction` per transition (read, check, write)."""

    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        lock_wait_s: float = 10.0,
        mono: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.directory = Path(directory)
        self._wait, self._mono, self._sleep = lock_wait_s, mono, sleep

    @contextlib.contextmanager
    def transaction(self) -> Iterator[JournalView]:
        end = self._mono() + self._wait
        while True:
            try:
                state = StateDir(self.directory)
                break
            except StateError as e:
                if "in use" not in str(e):
                    raise JournalError("journal.corrupt", str(e)) from None
                if self._mono() >= end:
                    raise JournalError("journal.busy", "another owner client holds the primary journal") from None
                self._sleep(0.05)
        try:
            yield JournalView(state)
        finally:
            state.close()
