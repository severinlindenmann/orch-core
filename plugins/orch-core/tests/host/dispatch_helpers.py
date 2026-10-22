"""H4a1 test helpers: a registered workspace served by the real dispatcher (``RegistryBackend``), and a client."""

from __future__ import annotations

import itertools
from typing import Any

from orch.host.control_client import new_idem
from tests.host.lifecycle_helpers import UID, Lab
from tests.host.outcome_helpers import WsClient
from tests.ops.helpers import SESSION

__all__ = ["SESSION", "UID", "Served", "err", "ok"]


def ok(res: dict[str, Any]) -> dict[str, Any]:
    assert res["t"] == "res" and res["ok"] is True, res.get("error", res)
    return res["result"]


def err(res: dict[str, Any]) -> str:
    assert res["t"] == "res" and res["ok"] is False, res
    return res["error"]["code"]


class Client:
    """One connection with request helpers: ``auth``, ``opts`` and ``files`` by keyword."""

    _ids = itertools.count()

    def __init__(self, ws: WsClient) -> None:
        self.ws = ws

    @property
    def welcome(self) -> dict[str, Any]:
        return self.ws.welcome

    def req(
        self,
        op: str,
        args: dict[str, Any] | None = None,
        *,
        grant: str | None = None,
        session: str | None = None,
        idem: str | bool | None = None,
        dry_run: bool | None = None,
        files: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        auth = {k: v for k, v in (("grant", grant), ("session", session)) if v is not None}
        if auth:
            extra["auth"] = auth
        opts: dict[str, Any] = {}
        if idem is True:
            opts["idem"] = new_idem()
        elif idem:
            opts["idem"] = idem
        if dry_run is not None:
            opts["dry_run"] = dry_run
        if timeout_ms is not None:
            opts["timeout_ms"] = timeout_ms
        if opts:
            extra["opts"] = opts
        if files is not None:
            extra["files"] = files
        return self.ws.call(op, args or {}, request_id or f"r{next(self._ids)}", **extra)

    def close(self) -> None:
        self.ws.close()


class Served:
    """A Lab workspace registered with a host whose workspace sockets run the real dispatcher."""

    def __init__(self, lab: Lab, **kw: Any) -> None:
        self.lab = lab
        self.h = lab.host(clock=lambda: lab.ws.clock[0], **kw)
        self.h.__enter__()
        self.control = self.h.client()
        self.wid = ok(lab.register(self.control))["data"]["workspace_id"]
        self.service = self.h.host.services[self.wid]
        self.clients: list[Client] = []

    @property
    def grant(self) -> str:
        return self.lab.ws.grant

    def client(self, hello_ops: dict[str, int] | None = None) -> Client:
        if hello_ops is None:
            ws = WsClient(self.lab.run / self.wid / "host.sock", self.wid, UID)
        else:
            from orch.host import frames
            from orch.host.client import Pin, handshake_pinned

            ws = WsClient.__new__(WsClient)
            hello = {"t": "hello", "proto": [1], "client": "test", "caps": [], "ops": hello_ops}
            ws.sock, ws.welcome = handshake_pinned(Pin(UID, self.wid, self.lab.run / self.wid / "host.sock"), hello)
            ws.decoder, ws.pending = frames.Decoder(), []
        c = Client(ws)
        self.clients.append(c)
        return c

    # -- the workspace, written from outside the host (a person's signed events)
    def append(self, fn: Any) -> Any:
        store = self.lab.ws.other()
        self.lab.ws.store = store
        try:
            return fn(store)
        finally:
            store.close()
            self.lab.ws.store = None

    def ticket(self, title: str = "A ticket") -> tuple[str, str]:
        """A new ticket, created through the host by the owner's agent (``new``); returns (uid, key). Tickets are made
        through the host: a ticket another process creates is a foreign append, which H6a picks up (F11)."""
        if not hasattr(self, "_maker"):
            self._maker = self.client()
        res = ok(self._maker.req("new", {"title": title}, grant=self.grant, session=SESSION, idem=True))
        return self.lab.ws.uid(res["key"]), res["key"]

    def restrict(self, uid: str, people: list[str] | None) -> None:
        """Restrict the ticket to ``people`` (``None``: back to workspace-visible)."""
        ws = self.lab.ws
        ws.tick()
        visibility: Any = "workspace" if people is None else {"restricted": people}

        def run(store: Any) -> None:
            store.ticket(uid)
            store.append(ws.person_event(ws.owner, uid, "visibility.changed", visibility=visibility), log=uid)

        self.append(run)

    def close(self) -> None:
        for c in self.clients:
            c.close()
        self.control.close()
        self.h.stop()
