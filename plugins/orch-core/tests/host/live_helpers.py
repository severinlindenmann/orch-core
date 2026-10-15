"""H6a test helpers: a served workspace with a second member, a connection that sorts ``res`` from ``evt``, and person
events appended through the host's own store (what ``human.submit`` does once H5a serves it)."""

from __future__ import annotations

import errno
import itertools
import time
from typing import Any

import pytest

from orch import canon, crypto
from orch.host.control_client import new_idem
from orch.host.journal import peek_cursor
from orch.identity import new_ulid
from tests.host.dispatch_helpers import SESSION, Served, ok
from tests.host.lifecycle_helpers import UID, Lab
from tests.store.helpers import GRANT_SECRET, ts

__all__ = ["SESSION", "Conn", "World", "jpos_of", "ok"]


def jpos_of(cursor: str) -> int:
    body = peek_cursor(cursor)
    assert body is not None, cursor
    return body["j"]


class World(Served):
    """A served workspace whose test UID is mapped to ``person`` (default: the owner). ``quinn`` is a second member
    (with a deciding device unless ``quinn_scopes`` says otherwise), added before the host starts."""

    def __init__(self, lab: Lab, *, map_to: str = "owner", quinn_scopes: tuple[str, ...] | None = None, **kw: Any):
        self.quinn = lab.add_member(**({"scopes": quinn_scopes} if quinn_scopes else {}))
        person = {"owner": lab.owner, "quinn": self.quinn.ref}[map_to]
        self.lab = lab
        self.h = lab.host(clock=lambda: lab.ws.clock[0], **kw)
        self.h.__enter__()
        self.control = self.h.client()
        self.wid = ok(lab.register(self.control, peers=lab.peers(person)))["data"]["workspace_id"]
        self.service = self.h.host.services[self.wid]
        self.bus = self.h.host.bus
        self.clients = []

    def conn(self) -> Conn:
        c = Conn(self.client())
        return c

    # -- writes through the host's own store (H5a's human.submit appends the same way)
    def host_append(self, make: Any) -> dict[str, Any]:
        """``make(ws)`` returns ``(event, log)``; it is appended on the workspace worker by the host's store."""
        service, ws = self.service, self.lab.ws
        ws.tick()

        def run() -> dict[str, Any]:
            store = service.store
            old_store, ws.store = ws.store, store
            old_check, store.append_check = store.append_check, None
            try:
                event, log = make(ws)
                if log != "workspace":
                    store.ticket(log)
                return store.append(event, log=log).event
            finally:
                store.append_check, ws.store = old_check, old_store
                service.sync()

        return self.h.run(service.call(run))

    def person_event(self, typ: str, log: str = "workspace", who: Any = None, **payload: Any) -> dict[str, Any]:
        return self.host_append(lambda ws: (ws.person_event(who or ws.owner, log, typ, **payload), log))

    def restrict(self, uid: str, people: list[str]) -> None:  # type: ignore[override]
        self.person_event("visibility.changed", uid, visibility={"restricted": sorted(set(people))})

    def write(self, key: str, text: str = "note") -> dict[str, Any]:
        """A ticket event through the host (``log``, an unattended op, by the owner's grant)."""
        if not hasattr(self, "_writer"):
            self._writer = self.client()
        self.lab.ws.tick()
        return ok(self._writer.req("log", {"ref": key, "text": text}, grant=self.grant, session=SESSION, idem=True))

    def ticket(self, title: str = "A ticket") -> tuple[str, str]:
        self.lab.ws.tick()
        return super().ticket(title)

    def grant_for(self, p: Any, hours: int = 1) -> str:
        """A grant issued by ``p`` for themselves (a member's: workable); returns the ``auth.grant`` value."""
        gid = "gr_" + new_ulid()
        issued = self.lab.ws.clock[0] + 1
        self.person_event(
            "grant.issued",
            who=p,
            grant=gid,
            scope="workable",
            verbs="agent",
            issued_at=ts(issued),
            hours=hours,
            expires_at=ts(issued + 3600 * hours),
            secret_hash=canon.grant_secret_hash(GRANT_SECRET),
            label="quinn",
        )
        return f"{gid}.{crypto.b64u(GRANT_SECRET)}"

    def owner_grant(self, hours: int = 1) -> str:
        """A further grant of the owner (scope all); returns its id."""
        gid = "gr_" + new_ulid()
        issued = self.lab.ws.clock[0] + 1
        self.person_event(
            "grant.issued",
            grant=gid,
            scope="all",
            verbs="agent",
            issued_at=ts(issued),
            hours=hours,
            expires_at=ts(issued + 3600 * hours),
            secret_hash=canon.grant_secret_hash(GRANT_SECRET),
            label="owner",
        )
        return gid

    def snapshot_cursor(self, principal: str) -> str:
        """The ``sub_cursor`` a snapshot read takes on the worker (the hook H6b's reads call)."""
        return self.h.run(self.service.call(lambda: self.service.sub_cursor(principal)))

    def check_journals(self) -> None:
        self.h.run(self.h.host.check_journals())


def world(tmp_path: Any, **kw: Any) -> World:
    lab = Lab(tmp_path)
    try:
        return World(lab, **kw)
    except OSError as exc:  # pragma: no cover
        lab.close()
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise


class Conn:
    """One connection: requests by id, events kept apart, in arrival order (``frames`` keeps both)."""

    _ids = itertools.count()

    def __init__(self, client: Any) -> None:
        self.client = client
        self.ws = client.ws
        self.frames: list[dict[str, Any]] = []
        self.evts: list[dict[str, Any]] = []
        self.res: dict[str, dict[str, Any]] = {}

    def send(self, op: str, args: dict[str, Any] | None = None, *, grant: str | None = None, **kw: Any) -> str:
        rid = f"q{next(self._ids)}"
        extra: dict[str, Any] = {}
        auth = {k: v for k, v in (("grant", grant), ("session", kw.pop("session", None))) if v is not None}
        if auth:
            extra["auth"] = auth
        if kw.pop("idem", False):
            extra["opts"] = {"idem": new_idem()}
        self.ws.send(rid, op, args or {}, **extra)
        return rid

    def _pump(self, timeout: float) -> bool:
        try:
            frame = self.ws.read(timeout)
        except TimeoutError:
            return False
        self.frames.append(frame)
        if frame["t"] == "evt":
            self.evts.append(frame)
        elif frame["t"] == "res":
            self.res[frame["id"]] = frame
        return True

    def answer(self, rid: str, timeout: float = 15) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while rid not in self.res:
            assert time.monotonic() < deadline, f"no answer to {rid}"
            self._pump(0.2)
        return self.res.pop(rid)

    def call(self, op: str, args: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        return self.answer(self.send(op, args, **kw))

    def open(self, topics: list[str], after: str | None = None, **kw: Any) -> dict[str, Any]:
        args: dict[str, Any] = {"topics": topics}
        if after is not None:
            args["after"] = after
        return ok(self.call("sub.open", args, **kw))["data"]

    def evt(self, sub: str, timeout: float = 10) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            for frame in self.evts:
                if frame["sub"] == sub:
                    self.evts.remove(frame)
                    return frame
            assert time.monotonic() < deadline, f"no evt for {sub}"
            self._pump(0.2)

    def items_until(self, sub: str, jpos: int, timeout: float = 10) -> tuple[list[dict[str, Any]], str]:
        """Every item of ``sub`` until an evt whose cursor reaches ``jpos``; returns (items, last cursor)."""
        items: list[dict[str, Any]] = []
        while True:
            frame = self.evt(sub, timeout)
            assert "end" not in frame, frame
            items.extend(frame["items"])
            if jpos_of(frame["cursor"]) >= jpos:
                return items, frame["cursor"]

    def quiet(self, sub: str | None = None, wait: float = 0.5) -> None:
        """No evt (for ``sub``, or any) arrives within ``wait`` seconds."""
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            self._pump(0.05)
        got = [f for f in self.evts if sub is None or f["sub"] == sub]
        assert not got, got


def principal_of(uid: int = UID, grant: str | None = None) -> str:
    """The §0.5 principal of a request (``g:<grant id>@<uid>`` for a verified grant)."""
    return f"g:{grant.partition('.')[0]}@{uid}" if grant else f"uid:{uid}"
