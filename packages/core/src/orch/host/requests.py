"""Human requests on disk (§0.8.1): ``<state>/hosts/<ws>/requests/<request id>`` (0600), one JSON file each.

A request is prepared by ``human.prepare``, bound to a signing device, signed by the client and submitted. It survives
a host restart. Its record holds everything the later steps need and nothing they could take from a caller:

* ``prepared``: the ``Prepared`` body (``op``, ``workspace_id``, ``log``, ``events``, ``material``,
  ``review_digest``, ``fingerprint``, ``origin``); the events are updated once, at binding (``actor.device``,
  ``auth``);
* ``extras``: per event, the section texts and artifact bytes (base64) the append needs, and the D58 source check;
* ``result``: the inner op's result as its handler built it at prepare (data, key, hints);
* ``submit``: the signed events and the outcome key of the ``human.submit`` that is appending them, written (fsync)
  **before** the first append, so a crash between two appends is rolled forward at start;
* ``outcome``: the terminal answer of the submission (``committed`` with its result, or ``failed`` with its error).

A terminal record keeps no file bytes: :meth:`RequestStore.write` strips ``extras[*].artifacts`` and ``body`` when
the state is terminal (they are needed only to submit), then calls :attr:`RequestStore.on_terminal`, which releases the
request's blob-quota reservation (H4a2, :mod:`orch.host.blobs`). An in-memory index of each record's person, state,
expiry and file bytes (built once at open, kept by every write) serves :meth:`RequestStore.pending` without reading
the records.

States: ``prepared``, ``bound``, ``submitted``, ``cancelled``, ``expired`` (the table of §0.8.1). A request that is
neither submitted nor cancelled is ``expired`` 10 minutes after prepare. At most 20 are pending per person. Terminal
states are kept 24 h. A hand-off request carries its code (H5b, :func:`orch.host.human.new_code`); others ``null``.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .custody import ensure_private_dir, read_json, write_private
from .errors import HostError

__all__ = ["EXPIRE_SECONDS", "GRANT_EXPIRE_SECONDS", "KEEP_SECONDS", "MAX_PENDING", "RequestStore", "new_request_id"]

LOG = logging.getLogger(__name__)
EXPIRE_SECONDS = 600
#: a grant request expires sooner: F1 refuses a grant.issued whose issued_at (the prepare time) is more than 300 s
#: away from its append
GRANT_EXPIRE_SECONDS = 240
KEEP_SECONDS = 24 * 3600
MAX_PENDING = 20
REQUEST_ID = re.compile(r"hr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
PENDING = frozenset({"prepared", "bound"})
TERMINAL = frozenset({"submitted", "cancelled", "expired"})


def new_request_id() -> str:
    from orch.identity import new_ulid

    return "hr_" + new_ulid()


class RequestStore:
    """The requests of one workspace. Call it on the workspace worker thread (its methods do file I/O)."""

    def __init__(self, state_dir: Path, workspace_id: str, host_uid: int, *, clock: Callable[[], float]) -> None:
        self.dir = Path(state_dir) / "hosts" / workspace_id / "requests"
        self.host_uid = host_uid
        self.clock = clock
        self._lock = threading.Lock()
        #: request id -> (person, state, expires, submitting, principal, file bytes)
        self._index: dict[str, tuple[Any, str, float, bool, str, int]] | None = None
        #: called with the request id when a record is written terminal (its file bytes stripped)
        self.on_terminal: Callable[[str], None] | None = None

    def open(self) -> None:
        """The directories, then one pass over the records: the index, and any terminal record that still holds file
        bytes is stripped."""
        ensure_private_dir(self.dir.parent.parent, self.host_uid)
        ensure_private_dir(self.dir.parent, self.host_uid)
        ensure_private_dir(self.dir, self.host_uid)
        self._index = None
        self._indexed()
        for rid, entry in list(self._indexed().items()):
            if entry[1] in TERMINAL and entry[5]:
                record = self.read(rid)
                if record is not None:
                    self.write(record)

    def _indexed(self) -> dict[str, tuple[Any, str, float, bool, str, int]]:
        if self._index is None:
            index = {}
            for rid in self.ids():
                try:
                    record = self.read(rid)
                except HostError:
                    continue
                if record is not None:
                    index[rid] = self._entry(record)
            self._index = index
        return self._index

    @staticmethod
    def _entry(record: dict[str, Any]) -> tuple[Any, str, float, bool, str, int]:
        return (
            record.get("person"),
            str(record.get("state")),
            float(record.get("expires", 0)),
            bool(record.get("submit")),
            str(record.get("principal", "")),
            file_bytes(record),
        )

    def open_count(self) -> int:
        """How many requests are not terminal and not expired (a person's prepared sign request still open)."""
        now = self.clock()
        return sum(
            1
            for _who, state, expires, submitting, _principal, _bytes in list(self._indexed().values())
            if state in PENDING and (submitting or expires > now)
        )

    def held(self) -> Iterator[tuple[str, str, int]]:
        """(request id, principal, file bytes) of every non-terminal record that holds file bytes (rebuilt
        reservations at open)."""
        for rid, entry in list(self._indexed().items()):
            if entry[1] not in TERMINAL and entry[5]:
                yield rid, entry[4], entry[5]

    def _path(self, request_id: str) -> Path:
        if not REQUEST_ID.fullmatch(request_id):
            raise HostError("not_found", "no such request")
        return self.dir / request_id

    def read(self, request_id: str) -> dict[str, Any] | None:
        return read_json(self._path(request_id), self.host_uid)

    def write(self, record: dict[str, Any]) -> None:
        terminal = record.get("state") in TERMINAL
        if terminal:
            record = strip(record)
        with self._lock:
            write_private(self._path(record["id"]), record)
            if self._index is not None:
                self._index[record["id"]] = self._entry(record)
        if terminal and self.on_terminal is not None:
            self.on_terminal(record["id"])

    def ids(self) -> list[str]:
        try:
            return sorted(n for n in os.listdir(self.dir) if REQUEST_ID.fullmatch(n))
        except FileNotFoundError:
            return []

    def load(self, request_id: str, principal: str) -> dict[str, Any]:
        """The request ``request_id`` of ``principal``, its expiry applied; ``not_found`` for none, another
        principal's, or another workspace's (each workspace has its own directory)."""
        record = self.read(request_id) if REQUEST_ID.fullmatch(request_id) else None
        if record is None or record.get("principal") != principal:
            raise HostError("not_found", "no such request")
        return self.expire(record)

    def lapsed(self, record: dict[str, Any], grace: float = 0.0) -> bool:
        """A pending request past its expiry (plus ``grace`` seconds), the one rule of :meth:`expire`. A request whose
        signed events are being appended (``submit`` set) never lapses. ``expires`` must be a finite number (not a
        bool): anything else is a ``ValueError``, never a silent lapse."""
        if record["state"] not in PENDING or record.get("submit"):
            return False
        expires = record["expires"]
        if type(expires) not in (int, float) or not math.isfinite(expires):
            raise ValueError("expires is not a finite number")
        return self.clock() >= expires + grace

    def expire(self, record: dict[str, Any]) -> dict[str, Any]:
        """A pending request older than 10 minutes (a grant: 4) becomes ``expired`` (written). A request whose signed
        events are being appended (``submit`` set) never expires: it is rolled forward."""
        if self.lapsed(record):
            record = {**record, "state": "expired", "done_at": self.clock()}
            self.write(record)
        return record

    def pending(self, person: str) -> int:
        """The person's pending requests, from the index; only a record due to expire is read (and expired)."""
        n, now = 0, self.clock()
        for rid, (who, state, expires, submitting, _principal, _bytes) in list(self._indexed().items()):
            if who != person or state not in PENDING:
                continue
            if not submitting and now >= expires:
                record = self.read(rid)
                if record is None or self.expire(record)["state"] not in PENDING:
                    continue
            n += 1
        return n

    def admit(self, person: str) -> None:
        if self.pending(person) >= MAX_PENDING:
            raise HostError("quota.host", f"at most {MAX_PENDING} human requests are pending per person")

    def unfinished(self) -> list[dict[str, Any]]:
        """Requests whose signed events were being appended when the host stopped (``submit`` without ``outcome``)."""
        out = []
        for rid in self.ids():
            record = self.read(rid)
            if record is not None and record.get("submit") and record.get("outcome") is None:
                out.append(record)
        return out

    def sweep(self) -> None:
        """Expire pending requests past 10 minutes; remove terminal ones older than 24 h."""
        now = self.clock()
        for rid in self.ids():
            try:
                record = self.read(rid)
            except HostError:
                continue
            if record is None or (record.get("submit") and record.get("outcome") is None):
                continue  # a submission being appended: start-up recovery owns it
            try:
                record = self.expire(record)
            except ValueError as exc:  # an invalid or missing expiry: skipped, so one bad record never stops the sweep
                LOG.warning("request %s skipped by the sweep: %s", rid, exc)
                continue
            except KeyError:
                LOG.warning("request %s skipped by the sweep: no expiry", rid)
                continue
            if record["state"] in TERMINAL and now - float(record.get("done_at", now)) > KEEP_SECONDS:
                try:
                    os.unlink(self._path(rid))
                except FileNotFoundError:
                    pass
                if self._index is not None:
                    self._index.pop(rid, None)


def file_bytes(record: dict[str, Any]) -> int:
    """The decoded file bytes a record's ``extras`` hold (the artifacts the inner op captured)."""
    total = 0
    for extra in record.get("extras") or []:
        for b64 in ((extra or {}).get("artifacts") or {}).values():
            total += len(b64) // 4 * 3 - (len(b64) - len(b64.rstrip("=")))
    return total


def strip(record: dict[str, Any]) -> dict[str, Any]:
    """A terminal record without the file bytes and section texts it needed only to submit."""
    extras = record.get("extras")
    if not extras or not any(isinstance(e, dict) and (e.get("artifacts") or e.get("body")) for e in extras):
        return record
    out = [{k: v for k, v in e.items() if k not in ("artifacts", "body")} if isinstance(e, dict) else e for e in extras]
    return {**record, "extras": out}
