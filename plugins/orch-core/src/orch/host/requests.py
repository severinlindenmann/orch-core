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

States: ``prepared``, ``bound``, ``submitted``, ``cancelled``, ``expired`` (the table of §0.8.1). A request that is
neither submitted nor cancelled is ``expired`` 10 minutes after prepare. At most 20 are pending per person. Terminal
states are kept 24 h. Hand-off codes (H5b) are ``null`` here; the record keeps the field for them.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .custody import ensure_private_dir, read_json, write_private
from .errors import HostError

__all__ = ["EXPIRE_SECONDS", "GRANT_EXPIRE_SECONDS", "KEEP_SECONDS", "MAX_PENDING", "RequestStore", "new_request_id"]

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

    def open(self) -> None:
        ensure_private_dir(self.dir.parent.parent, self.host_uid)
        ensure_private_dir(self.dir.parent, self.host_uid)
        ensure_private_dir(self.dir, self.host_uid)

    def _path(self, request_id: str) -> Path:
        if not REQUEST_ID.fullmatch(request_id):
            raise HostError("not_found", "no such request")
        return self.dir / request_id

    def read(self, request_id: str) -> dict[str, Any] | None:
        return read_json(self._path(request_id), self.host_uid)

    def write(self, record: dict[str, Any]) -> None:
        with self._lock:
            write_private(self._path(record["id"]), record)

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

    def expire(self, record: dict[str, Any]) -> dict[str, Any]:
        """A pending request older than 10 minutes (a grant: 4) becomes ``expired`` (written). A request whose signed
        events are being appended (``submit`` set) never expires: it is rolled forward."""
        if record["state"] in PENDING and not record.get("submit") and self.clock() >= record["expires"]:
            record = {**record, "state": "expired", "done_at": self.clock()}
            self.write(record)
        return record

    def pending(self, person: str) -> int:
        n = 0
        for rid in self.ids():
            record = self.read(rid)
            if record is not None and record.get("person") == person and self.expire(record)["state"] in PENDING:
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
            record = self.expire(record)
            if record["state"] in TERMINAL and now - float(record.get("done_at", now)) > KEEP_SECONDS:
                try:
                    os.unlink(self._path(rid))
                except FileNotFoundError:
                    pass
