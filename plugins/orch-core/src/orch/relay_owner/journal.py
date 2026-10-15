"""The primary's durable per-request journal (orch-v2-host 11A.15.9 "The primary's durable per-request journal", N7).

One state machine per cert request, each transition written **before** its effect:

* ``verified``: the immutable verified request bytes (``{o, sig}``), after ``check_cert_request`` and
  ``open_cert_request`` passed;
* ``challenged``: the immutable challenge bytes (with ``primary_nonce``), written before the challenge POST;
* ``decided``: either ``sign`` with the exact certificate bytes or ``reject`` with the exact rejection bytes, written
  before the upload;
* ``uploaded``: the relay's 2xx was seen;
* terminal without a decision: ``expired``, ``cancelled``, ``superseded``, and ``refused`` (a request id that failed
  §8.4 step 2.4 or later: burned, never judged fresh again; no relay bytes are kept for it).

Storage: ``<state>/person/primary/requests/<request_id>.json`` (:func:`journal_dir`), a private directory opened
through :class:`orch.relay.state.StateDir` (0700, files 0600, owned by this user, ``O_NOFOLLOW`` against the directory
fd, temp file + ``fsync`` + ``rename`` + directory ``fsync``; a symlink, a foreign owner or a wider mode is refused,
never repaired). **Concurrency:** every transaction holds the directory's exclusive ``flock`` (``StateDir`` takes it
for its lifetime) from the read of the current state to the durable write, so two owner clients can never both act on
one request; a second one waits up to ``lock_wait_s`` and then fails ``journal.busy``. **JUDGEMENT CALL:** the lock is
the directory's own ``flock`` (as every R1 state directory), not a separate ``.lock`` file: same exclusion, one fewer
file to check.

**Same request id with other bytes:** refused against the stored ``verified`` bytes with
:class:`RequestConflict` (alarm ``primary_request_conflict``). **Retention:** a record is pruned to a tombstone
(``{request_id, workspace_id, state, expires_ms}``) after its ``expires_ms`` plus 7 days, so the id stays terminal.

:meth:`JournalView.cert_journal` derives the pure :class:`orch.identity.certreq.CertJournal` the §8.4 step 2 checks
read: ``seen`` is every id here, ``open`` the live (``verified``/``challenged``) requests, ``closed`` the decided and
terminal ones.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from orch import canon
from orch.identity import certreq
from orch.relay.state import StateDir, StateError

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

STATES = ("verified", "challenged", "decided", "uploaded", "expired", "cancelled", "superseded", "refused")
LIVE = frozenset({"verified", "challenged"})
RETENTION_MS = 7 * 24 * 3600 * 1000
_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"verified", "refused"}),
    "verified": frozenset({"challenged", "decided", "expired", "cancelled", "superseded"}),
    "challenged": frozenset({"decided", "expired", "cancelled", "superseded"}),
    "decided": frozenset({"uploaded"}),
}
_FIELDS = frozenset(
    {"v", "request_id", "workspace_id", "expires_ms", "state", "request", "challenge", "decision", "decided",
     "tombstone"}
)  # fmt: skip
_HEX32 = re.compile(r"[0-9a-f]{32}")
_FILE = re.compile(r"([0-9a-f]{32})\.json")
MAX_ENTRIES = 4096


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
            if (self.request_cj, self.challenge_cj, self.decided_cj) != (None, None, None):
                raise JournalError("journal.corrupt", "a tombstone keeps no bytes")
            return
        need_request = self.state not in ("refused",)
        if need_request and self.request_cj is None:
            raise JournalError("journal.corrupt", f"a {self.state} record without its request")
        if self.state == "challenged" and self.challenge_cj is None:
            raise JournalError("journal.corrupt", "a challenged record without its challenge")
        if (self.state in ("decided", "uploaded")) != (self.decision is not None and self.decided_cj is not None):
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
    """The journal inside one transaction (the ``flock`` is held). Reads see this transaction's writes."""

    def __init__(self, state: StateDir) -> None:
        self._state = state
        self._entries: dict[str, Entry] = {}
        names = sorted(os.listdir(state.path))
        if len(names) > MAX_ENTRIES:
            raise JournalError("journal.corrupt", "too many records; prune the journal")
        for name in names:
            m = _FILE.fullmatch(name)
            if m is None:
                continue
            try:
                raw = state.read(name)
            except StateError as e:
                raise JournalError("journal.corrupt", f"record {m.group(1)}: {e}") from None
            if raw is None:  # removed between the listing and the read: not ours to explain
                continue
            entry = Entry.from_json(raw)
            if entry.request_id != m.group(1):
                raise JournalError("journal.corrupt", f"record {m.group(1)} names another request")
            self._entries[entry.request_id] = entry

    def get(self, request_id: str) -> Entry | None:
        return self._entries.get(request_id)

    def entries(self) -> list[Entry]:
        return [self._entries[k] for k in sorted(self._entries)]

    def cert_journal(self, *, excluding: str | None = None) -> certreq.CertJournal:
        """The pure journal of the §8.4 step 2 checks; ``excluding`` leaves one id out (a resume re-checks a request
        the journal already holds, against everything else)."""
        es = [e for e in self._entries.values() if e.request_id != excluding]
        return certreq.CertJournal(
            seen=frozenset(e.request_id for e in es),
            open=tuple((e.workspace_id, e.request_id, e.expires_ms) for e in es if e.live),
            closed=frozenset(e.request_id for e in es if not e.live and e.state != "refused"),
        )

    def put(self, entry: Entry) -> Entry:
        """Write ``entry`` durably, as a transition from the current record (or a creation). A ``verified`` record
        for an id the journal holds is :class:`RequestConflict` unless its bytes are the stored ones (then the stored
        record is returned and nothing is written). Immutable parts never change."""
        cur = self._entries.get(entry.request_id)
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

    def prune(self, now_ms: int) -> int:
        """Turn records past ``expires_ms + 7 days`` into tombstones (id, workspace, final state, expiry)."""
        n = 0
        for e in list(self._entries.values()):
            if not e.tombstone and now_ms > e.expires_ms + RETENTION_MS:
                self._write(replace(e, request_cj=None, challenge_cj=None, decided_cj=None, tombstone=True))
                n += 1
        return n

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
