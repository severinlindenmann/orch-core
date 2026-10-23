"""An in-process router with the semantics of hub contract 10.3, at the ``RouteLink`` seam: per-recipient FIFO queue,
one unacknowledged ``route_in`` per link, entries removed only by an acknowledgement, a link that can be down
(``OSError``). It does not parse or open envelopes beyond the 60-byte header (``from_ws`` / ``to_ws`` / id)."""

from __future__ import annotations

from orch.hubclient.tlslink.client import LinkError

MAX = 1 << 20


class RouteHub:
    def __init__(self, bound: set[str]) -> None:
        self.bound = set(bound)
        self.queues: dict[str, list[bytes]] = {w: [] for w in bound}
        self.seen: dict[tuple[str, str], bytes] = {}
        self.acks: list[tuple[str, str | None]] = []
        self.down: set[str] = set()
        self.denied: set[tuple[str, str]] = set()

    def link(self, ws: str) -> FakeLink:
        if ws in self.down:
            raise ConnectionError("hub down")
        return FakeLink(self, ws)


class FakeLink:
    def __init__(self, hub: RouteHub, ws: str) -> None:
        self.hub, self.ws = hub, ws
        self.out: bytes | None = None
        self.closed = False

    def _up(self) -> None:
        if self.closed or self.ws in self.hub.down:
            raise ConnectionError("link down")

    def route_out(self, envelope: bytes) -> str:
        self._up()
        if len(envelope) > MAX:
            raise LinkError("too_large")
        frm, to, eid = envelope[24:40].hex(), envelope[40:56].hex(), envelope[8:24].hex()
        if frm != self.ws:
            raise LinkError("other_workspace")
        if to not in self.hub.bound or (frm, to) in self.hub.denied:
            raise LinkError("not_allowed")
        old = self.hub.seen.get((frm, eid))
        if old is not None and old != envelope:
            raise LinkError("replay")
        self.hub.seen[(frm, eid)] = envelope
        if old is None:
            self.hub.queues[to].append(envelope)
        return "queued"

    def next_route_in(self, wait: float = 0.0) -> bytes | None:
        self._up()
        if self.out is not None:
            raise LinkError("malformed")
        q = self.hub.queues[self.ws]
        if not q:
            return None
        self.out = q[0]
        return self.out

    def ack_route_in(self, error: str | None = None) -> None:
        self._up()
        assert self.out is not None
        self.hub.queues[self.ws].pop(0)
        self.hub.acks.append((self.ws, error))
        self.out = None

    def close(self) -> None:
        self.closed = True
