"""B10 Part B (phase 1): "approve on your phone". The HINT, the column-R read ``phone.requests``, the read
``gate.input`` and the host-side ``phone.request`` / ``phone.cancel`` / ``phone.status`` ops (wire contract:
``~/orch-dev/state/batch-10.md`` "Contract (Part B phase 1)"; orch-v2-host "As built in B10 Part B").

A hint is a host-local, UNSIGNED note "this person was asked, from the Mac, to decide this gate at this generation". It
carries NO authority and is not in the log: a phone signs a normal ``gate.approved`` / ``gate.changes_requested`` that
the model judges exactly as it judges any other (gate hash, ``gate_gen``, policy hash, eligibility, device at append).
What the file stores is only ``{id, n, person, uid, gate, gate_gen, base_seq, created, expires, cancelled}``; the
state a reader sees (``open | done | cancelled | expired | stale``) is DERIVED from the log on every read.

* ``phone.request`` / ``phone.cancel``: column P only (``person_write``; column G and N get ``op.not_allowed``), with an
  idem and an outcome record like ``reader.declare``. One open hint per (ticket, gate, person); at most
  :data:`MAX_OPEN` open hints per person (``rate.limited``).
* ``phone.status`` (a read, column P): the caller's own hints.
* ``phone.requests`` (a read, columns P and R): the viewer's own OPEN hints on tickets the viewer may see; nothing else.
* ``gate.input`` (a read, columns P and R): the gate-hash input G exactly as the host hashes it, so a phone can
  recompute the hash from the section texts of ``ticket.get``; the host's word until the phone has checked it.

Fail closed: an unreadable hint file is an error, never an empty list that hides a hint, and a hint file that does
not parse is never trusted partly.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .custody import ensure_private_dir, read_json, write_private
from .errors import HostError
from .visibility import Viewer

if TYPE_CHECKING:
    from .workspace import Call

__all__ = [
    "CAPS",
    "GATES",
    "HINT_ID",
    "KEEP_SECONDS",
    "MAX_OPEN",
    "PHONE_WRITE_OPS",
    "TTL_SECONDS",
    "HintStore",
    "derive",
    "gate_input",
    "new_hint_id",
    "open_after",
    "requests_for",
    "serve",
    "status_for",
]

LOG = logging.getLogger(__name__)
CAPS = ["phone/1"]
#: the gates a phone decides in phase 1; ``code`` and ``verify`` (host-stamped ``source_sha``) are phase 2
GATES = ("requirements", "plan")
TTL_SECONDS = 600
KEEP_SECONDS = 24 * 3600
MAX_OPEN = 5
PHONE_WRITE_OPS: dict[str, int] = {"phone.request": 1, "phone.cancel": 1}
HINT_ID = re.compile(r"ph_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_SCHEMAS = {
    "phone.request": {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "minLength": 1, "maxLength": 64},
            "gate": {"type": "string", "minLength": 1, "maxLength": 32},
            "gate_gen": {"type": "integer", "minimum": 0, "maximum": 2**53 - 1},
        },
        "required": ["ref", "gate", "gate_gen"],
        "additionalProperties": False,
    },
    "phone.cancel": {
        "type": "object",
        "properties": {"id": {"type": "string", "pattern": "^" + HINT_ID.pattern + "$"}},
        "required": ["id"],
        "additionalProperties": False,
    },
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}


def new_hint_id() -> str:
    from orch.identity import new_ulid

    return "ph_" + new_ulid()


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


def _int(v: Any) -> bool:
    return type(v) is int


# --- the file -------------------------------------------------------------------------------------------------------


class HintStore:
    """The hints of one workspace: ``<state>/hosts/<ws>/phone-hints.json`` (0600). Methods run on the workspace worker
    thread (file I/O); the lock only keeps the push planner's job and an op from interleaving a read-modify-write."""

    def __init__(self, state_dir: Path, workspace_id: str, host_uid: int, *, clock: Callable[[], float]) -> None:
        self.dir = Path(state_dir) / "hosts" / workspace_id
        self.path = self.dir / "phone-hints.json"
        self.host_uid = host_uid
        self.clock = clock
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        doc = read_json(self.path, self.host_uid)
        if doc is None:
            return {"v": 1, "n": 0, "hints": []}
        if type(doc) is not dict or doc.get("v") != 1 or not _int(doc.get("n")) or type(doc.get("hints")) is not list:
            raise HostError("internal", "the phone hint file does not parse")
        for h in doc["hints"]:
            ok = (
                type(h) is dict
                and type(h.get("id")) is str
                and HINT_ID.fullmatch(h["id"]) is not None
                and _int(h.get("n"))
                and all(type(h.get(k)) is str for k in ("person", "uid", "gate"))
                and all(_int(h.get(k)) for k in ("gate_gen", "base_seq", "created", "expires"))
                and (h.get("cancelled") is None or _int(h["cancelled"]))
                and h["gate"] in GATES
            )
            if not ok:
                raise HostError("internal", "the phone hint file does not parse")
        return doc

    def _write(self, doc: dict[str, Any]) -> None:
        ensure_private_dir(self.dir.parent, self.host_uid)
        ensure_private_dir(self.dir, self.host_uid)
        write_private(self.path, doc)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(h) for h in self._read()["hints"]]

    def get(self, hint_id: str) -> dict[str, Any] | None:
        return next((h for h in self.all() if h["id"] == hint_id), None)

    def add(self, hint: dict[str, Any], *, replace: str | None = None) -> dict[str, Any]:
        """Store ``hint`` (``n`` is assigned here, monotonic for the push planner), dropping ``replace`` (a hint of the
        same ticket and gate at an older generation) and every hint kept longer than :data:`KEEP_SECONDS`."""
        with self._lock:
            doc = self._read()
            now = self.clock()
            doc["n"] += 1
            hint = {**hint, "n": doc["n"]}
            keep = [
                h for h in doc["hints"]
                if h["id"] != replace and max(h["expires"], h.get("cancelled") or 0) + KEEP_SECONDS > now
            ]  # fmt: skip
            doc["hints"] = [*keep, hint]
            self._write(doc)
            return dict(hint)

    def cancel(self, hint_id: str) -> None:
        with self._lock:
            doc = self._read()
            for h in doc["hints"]:
                if h["id"] == hint_id and not h.get("cancelled"):
                    h["cancelled"] = int(self.clock())
            self._write(doc)

    def newer_than(self, n: int) -> list[dict[str, Any]]:
        """The hints with a counter above ``n``, oldest first (the push planner's cursor)."""
        return sorted((h for h in self.all() if h["n"] > n), key=lambda h: h["n"])


# --- derived state --------------------------------------------------------------------------------------------------


def derive(service: Any, hint: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """``(state, outcome)`` of ``hint`` from the log now (the caller holds the store lock and has refreshed).

    ``done``: a decision of the hint's PERSON on its gate AT its generation, appended after the hint was made (any
    device, any way: the hint is not what authorises it). Then ``cancelled``, ``expired``, ``stale`` (the gate moved to
    another generation, or the ticket is gone), else ``open``."""
    store = service.store
    view = store.state.tickets.get(hint["uid"])
    if view is None:
        return "stale", None
    g = view.gates.get(hint["gate"])
    if g is None:
        return "stale", None
    for d in reversed(g.decisions):
        if d.person != hint["person"] or d.gen != hint["gate_gen"] or d.kind not in ("approve", "changes"):
            continue
        ev = store.logged_event(hint["uid"], d.id)
        if ev is not None and ev["seq"] > hint["base_seq"]:
            kind = "approved" if d.kind == "approve" else "changes_requested"
            return "done", {"kind": kind, "seq": ev["seq"], "event_id": d.id, "at": ev["at"]}
    if hint.get("cancelled"):
        return "cancelled", None
    if service.clock() >= hint["expires"]:
        return "expired", None
    if not g.applies or g.gen != hint["gate_gen"]:
        return "stale", None
    return "open", None


def _row(service: Any, hint: dict[str, Any], state: str, outcome: dict[str, Any] | None) -> dict[str, Any]:
    view = service.store.state.tickets.get(hint["uid"])
    row: dict[str, Any] = {
        "id": hint["id"],
        "ref": view.key if view is not None else None,
        "uid": hint["uid"],
        "gate": hint["gate"],
        "gate_gen": hint["gate_gen"],
        "created_at": _iso(hint["created"]),
        "expires_at": _iso(hint["expires"]),
        "state": state,
    }
    if outcome is not None:
        row["outcome"] = outcome
    return row


def status_for(service: Any, viewer: Viewer, hint_id: str | None) -> dict[str, Any]:
    """``phone.status``: the viewer's own hints, newest first (or the one ``hint_id``, else ``not_found``)."""
    store = service.store
    with store.locked():
        store.refresh()
        mine = [h for h in service.phone_hints.all() if h["person"] == viewer.person]
        if hint_id is not None:
            mine = [h for h in mine if h["id"] == hint_id]
            if not mine:
                raise HostError("not_found", "no such phone request")
        rows = []
        for h in sorted(mine, key=lambda x: x["n"], reverse=True):
            state, outcome = derive(service, h)
            rows.append(_row(service, h, state, outcome))
    return {"hints": rows}


def requests_for(service: Any, viewer: Viewer) -> dict[str, Any]:
    """``phone.requests`` (columns P and R): the viewer's own OPEN hints whose ticket the viewer may still see."""
    store = service.store
    out = []
    with store.locked():
        store.refresh()
        for h in sorted(service.phone_hints.all(), key=lambda x: x["n"], reverse=True):
            if viewer.person is None or h["person"] != viewer.person:
                continue
            view = store.state.tickets.get(h["uid"])
            if view is None or not viewer.sees(view):
                continue
            state, _ = derive(service, h)
            if state != "open":
                continue
            out.append(
                {
                    "id": h["id"],
                    "uid": h["uid"],
                    "ref": view.key,
                    "title": view.title,
                    "gate": h["gate"],
                    "gate_gen": h["gate_gen"],
                    "created_at": _iso(h["created"]),
                    "expires_at": _iso(h["expires"]),
                }
            )
        now = _iso(service.clock())
    return {"requests": out, "now": now}


def gate_input(service: Any, viewer: Viewer, ref: str, gate: str) -> dict[str, Any]:
    """``gate.input``: G of ticket ``ref``'s ``gate`` as the host hashes it, with hash, policy hash, gen."""
    from orch.store.render import thaw

    store = service.store
    with store.locked():
        store.refresh()
        view = viewer.ticket(store, ref)
        g = view.gates.get(gate)
        if g is None:
            raise HostError("invalid.input", f"gate.input: {gate} is not a gate")
        if not g.applies:
            raise HostError("gate.not_applicable", f"{gate} does not apply to this ticket")
        if g.hash is None or g.input is None or g.blocked:
            raise HostError("invalid.input", f"the {gate} gate cannot be hashed right now")
        return {
            "ref": view.uid, "gate": gate, "gen": g.gen, "hash": g.hash, "policy_hash": g.policy_hash,
            "input": thaw(g.input),
        }  # fmt: skip


# --- the writes -----------------------------------------------------------------------------------------------------


def _phones(state: Any, person: str) -> int:
    """The person's current paired phone devices: not removed or revoked, with a certificate that can decide and never
    more than look + decide (the phone profile, 14.8)."""
    return sum(
        1
        for d in state.workspace.devices.values()
        if d.person == person and not d.removed and not d.revoked
        and "decide" in d.scopes and set(d.scopes) <= {"look", "decide"}
    )  # fmt: skip


def create(service: Any, viewer: Viewer, args: dict[str, Any]) -> dict[str, Any]:
    """``phone.request`` under the store lock on fresh state."""
    gate = args["gate"]
    if gate not in GATES:
        raise HostError("invalid.input", f"phone.request: {gate} cannot be decided on the phone yet")
    store = service.store
    with store.locked():
        store.refresh()
        view = viewer.ticket(store, args["ref"])
        g = view.gates[gate]
        if not g.applies:
            raise HostError("gate.not_applicable", f"{gate} does not apply to this ticket")
        if viewer.person not in g.eligible:
            raise HostError("gate.not_eligible", f"{viewer.person} may not decide {gate}")
        if g.gen != args["gate_gen"]:
            raise HostError("gate.stale", f"gate_gen {args['gate_gen']} is not current ({g.gen})")
        if g.hash is None or g.blocked:
            raise HostError("invalid.input", f"the {gate} gate cannot be hashed right now")
        hints = service.phone_hints
        phones = _phones(store.state, viewer.person)
        replace = None
        open_n = 0
        for h in hints.all():
            if h["person"] != viewer.person:
                continue
            state, _ = derive(service, h)
            if state != "open":
                continue
            if h["uid"] == view.uid and h["gate"] == gate:
                if h["gate_gen"] == g.gen:
                    return {**_row(service, h, "open", None), "duplicate": True, "phones": phones}
                replace = h["id"]
                continue
            open_n += 1
        if open_n >= MAX_OPEN:
            raise HostError("rate.limited", f"at most {MAX_OPEN} open phone requests")
        now = int(service.clock())
        hint = hints.add(
            {
                "id": new_hint_id(), "person": viewer.person, "uid": view.uid, "gate": gate, "gate_gen": g.gen,
                "base_seq": store.log_seq(view.uid) or 0, "created": now, "expires": now + TTL_SECONDS,
                "cancelled": None,
            },
            replace=replace,
        )  # fmt: skip
        out = {**_row(service, hint, "open", None), "duplicate": False, "phones": phones}
    return out


def cancel(service: Any, viewer: Viewer, hint_id: str) -> dict[str, Any]:
    store = service.store
    with store.locked():
        store.refresh()
        h = service.phone_hints.get(hint_id)
        if h is None or h["person"] != viewer.person:
            raise HostError("not_found", "no such phone request")
        state, _ = derive(service, h)
        if state == "open":
            service.phone_hints.cancel(hint_id)
            state = "cancelled"
        return {"id": hint_id, "state": state}


def serve(call: Call) -> dict[str, Any]:
    """``phone.request`` and ``phone.cancel`` on the worker thread (after the column): args, idem, the person."""
    from .auth import Redactor
    from .outcomes import request_idem
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        raise HostError("invalid.input", f"{op}: args are not valid")
    idem = request_idem(op, call.frame.get("opts"))
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    viewer = Viewer.of(call)
    if viewer.person is None or call.column.name != "P" or call.relay is not None:
        raise HostError("op.not_allowed", f"{op} is a person's own, from the host")

    def apply(_intent: Any) -> dict[str, Any]:
        result = create(service, viewer, args) if op == "phone.request" else cancel(service, viewer, args["id"])
        return ok_result(result)

    return service.outcomes.execute(
        call.principal, str(idem), op, dict(args), apply, planned=None,
        session=(call.frame.get("auth") or {}).get("session"),
        redactor=Redactor((call.frame.get("auth") or {}).get("grant")),
    )  # fmt: skip


def open_after(service: Any, n: int) -> tuple[int, list[dict[str, Any]]]:
    """The push planner's source (a worker job, under the store lock): ``(the highest hint counter, the hints above
    ``n`` that are still open)``. A hint that is done, cancelled, expired or stale by the time the planner looks is
    skipped; the cursor still moves past it."""
    store = service.store
    with store.locked():
        store.refresh()
        newer = service.phone_hints.newer_than(n)
        if not newer:
            return n, []
        return newer[-1]["n"], [h for h in newer if derive(service, h)[0] == "open"]
