"""The router (spec §4, §9; protocol.md §10.2 ``route_out`` / ``route_in``).

``route_out`` from workspace A: the router parses the envelope header, checks that A is the sender (``from_ws`` is the
link's workspace), that A's pinned ``WSK`` signed it, that the recipient is joined and bound to this hub and that
:class:`Authz` allows the route; then it queues the exact bytes for the recipient and, if the recipient's link is up,
pushes them as an h2w ``route_in`` request. The recipient's host answers with a w2h reply (same op, ``re`` = request
``seq``); only that acknowledgement deletes the queue entry, so delivery is at least once and in enqueue order (one
unacknowledged ``route_in`` per recipient link). The receiver deduplicates on ``(from_ws, id)`` (v2 §11.4).

The hub never decrypts, re-signs or alters an envelope, and never logs envelope bytes.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from ..contract.canon import b64u, unb64u
from ..contract.hub import Refused
from . import envelope as E
from .queue import RouteQueue

if TYPE_CHECKING:
    from ..link.registry import Registry


class Authz(Protocol):
    """May workspace ``sender_ws`` route to ``recipient_ws`` through this hub? D3's ``orch_hub.access`` provides the
    real one (roles/grants); fail closed on anything unknown."""

    def may_route(self, sender_ws: str, recipient_ws: str) -> bool: ...


class BoundOnly:
    """Phase-3 minimum rule until the grant model lands: both workspaces are joined and bound to this hub."""

    def __init__(self, registry: Registry) -> None:
        self._reg = registry

    def may_route(self, sender_ws: str, recipient_ws: str) -> bool:
        return self._reg.mode(sender_ws) is not None and self._reg.mode(recipient_ws) is not None


class AccessAuthz:
    """V3-D19: a route A->B is authorized as A's owner, the hub user the admin aliased to A's owner at join. It needs
    that alias, the sender currently bound, and a role of ``edit`` or better for that user in B (D3's ``Access.role_of``), and B in full mode (in a
    selective workspace hub roles grant nothing, protocol.md §7). Read live on every route, so a revoked grant stops
    the next one. Anything missing, unknown or failing is a refusal."""

    def __init__(self, registry: Registry, access: Any) -> None:
        self._reg, self._acc = registry, access

    def may_route(self, sender_ws: str, recipient_ws: str) -> bool:
        from ..access import role_rank

        try:
            alias = self._reg.owner_alias(sender_ws)
            if alias is None or self._reg.mode(sender_ws) is None or self._reg.mode(recipient_ws) != "full":
                return False
            return role_rank(self._acc.role_of(alias, recipient_ws)) >= role_rank("edit")
        except Exception:  # noqa: BLE001  # fail closed
            return False


class LinkPort(Protocol):
    """One live link, as the router sees it. ``send_request`` signs and writes an h2w request, returns its ``seq``;
    raises ``OSError``-like link errors (any ``Exception``) when the link is gone."""

    def send_request(self, op: str, body: dict[str, Any]) -> int: ...


SEND_WAIT_S = 1.0  # how long route_out waits for the push before answering "queued"


class _Live:
    def __init__(self, port: LinkPort) -> None:
        self.port = port
        self.lock = threading.Lock()
        self.inflight: tuple[int, int] | None = None  # (queue id, h2w seq)
        self.sent_max = 0  # highest queue id handed to this link
        self.dead = False
        self.cv = threading.Condition()  # sent_max moved / kick / dead
        self.kicked = False


class Router:
    def __init__(
        self,
        registry: Registry,
        queue: RouteQueue | None = None,
        authz: Authz | None = None,
        now_ms: Callable[[], int] | None = None,
        on_routed: Callable[[str, str], None] | None = None,
    ) -> None:
        self._reg = registry
        self._on_routed = on_routed  # (from_ws, to_ws) after an accepted hand-off: counts only (route_stats)
        self.queue = queue or RouteQueue()
        self._authz = authz or BoundOnly(registry)
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        self._live: dict[str, _Live] = {}

    # --- w2h route_out ------------------------------------------------------------------------------------------

    def route_out(self, sender_ws: str, raw: bytes) -> str:
        """Returns ``"forwarded"`` (handed to the recipient's live link) or ``"queued"``. Raises ``Refused``."""
        header, signed, sig = E.parse(raw)
        if header.from_ws != sender_ws:
            raise Refused("other_workspace")
        if header.to_ws == sender_ws:
            raise Refused("malformed")
        pin = self._reg.pin(sender_ws)
        if pin is None:
            raise Refused("unknown_workspace")
        E.verify(signed, sig, unb64u(pin["wsk_pub"], 65))
        # One answer for "not joined", "unbound" and "not allowed": a member learns nothing about who else is here.
        if self._reg.mode(header.to_ws) is None or not self._authz.may_route(sender_ws, header.to_ws):
            raise Refused("not_allowed")
        qid = self.queue.add(header.to_ws, sender_ws, header.envelope_id, raw, self._now())
        if self._on_routed is not None:
            try:
                self._on_routed(sender_ws, header.to_ws)
            except Exception:  # noqa: BLE001, S110 - a counter never stops a hand-off
                pass
        with self._lock:
            live = self._live.get(header.to_ws)
        if live is None:
            return "queued"
        self._kick(live)
        with live.cv:  # a stuck recipient must not stall the sender: wait briefly, else report "queued"
            live.cv.wait_for(lambda: live.sent_max >= qid or live.dead, SEND_WAIT_S)
        return "forwarded" if live.sent_max >= qid else "queued"

    # --- h2w route_in -------------------------------------------------------------------------------------------

    def send_event(self, ws: str, log: str, event: dict[str, Any]) -> int:
        """h2w ``event_deliver`` (protocol.md §10.2) to the workspace's live link: the custodial engine's sink. Written
        once, not queued: no live link, or a failed write, is ``unavailable`` and the caller (the engine) refuses.
        The host's answer is not awaited here; the host deduplicates on the event id. Returns the h2w ``seq`` of the
        write: the delivery queue matches the host's reply on it (§8.7)."""
        with self._lock:
            live = self._live.get(ws)
        if live is None or live.dead:
            raise Refused("unavailable")
        try:
            return live.port.send_request("event_deliver", {"log": log, "event": event})
        except Exception:  # noqa: BLE001  # the link is gone
            self._kill(live)
            raise Refused("unavailable") from None

    def attach(self, ws: str, port: LinkPort) -> _Live:
        """A recipient's link is up (authenticated, pinned): deliver what is queued. A newer link replaces an older."""
        live = _Live(port)
        with self._lock:
            old = self._live.get(ws)
            self._live[ws] = live
        if old is not None:
            self._kill(old)
        threading.Thread(target=self._worker, args=(ws, live), name="route-push", daemon=True).start()
        self._kick(live)
        return live

    def is_online(self, ws: str) -> bool:
        with self._lock:
            live = self._live.get(ws)
            return live is not None and not live.dead

    def detach(self, ws: str, live: _Live) -> None:
        self._kill(live)
        with self._lock:
            if self._live.get(ws) is live:
                del self._live[ws]

    def on_reply(self, ws: str, live: _Live, o: dict[str, Any]) -> None:
        """A w2h reply from the recipient's host. Only the acknowledgement of our in-flight ``route_in`` counts;
        anything else is ignored. A refusal from the host (``{"error"}``) also ends the entry: the receiver dropped
        it, and resending the same bytes cannot change that."""
        if o["op"] != "route_in":
            return
        with live.lock:
            if live.inflight is None or live.inflight[1] != o["re"]:
                return
            if "error" in o["body"]:
                self._reg.count_op(ws, "route_in", False)
            self.queue.delete(live.inflight[0])
            live.inflight = None
        self._kick(live)

    @staticmethod
    def _kick(live: _Live) -> None:
        with live.cv:
            live.kicked = True
            live.cv.notify_all()

    @staticmethod
    def _kill(live: _Live) -> None:
        with live.cv:
            live.dead = True
            live.cv.notify_all()

    def _worker(self, ws: str, live: _Live) -> None:
        """One pusher per live link: writes (which can block on a slow recipient) never run on a sender's thread."""
        while True:
            with live.cv:
                live.cv.wait_for(lambda: live.kicked or live.dead, 1.0)
                if live.dead:
                    return
                live.kicked = False
            self._drain(ws, live)

    def _drain(self, ws: str, live: _Live) -> None:
        with live.lock:
            while not live.dead and live.inflight is None:
                if self._reg.mode(ws) is None:  # unbound or unknown: hold the queue, deliver nothing
                    return
                head = self.queue.head(ws, self._now())
                if head is None:
                    return
                qid, raw = head
                try:
                    seq = live.port.send_request("route_in", {"envelope": b64u(raw)})
                except Exception:  # noqa: BLE001  # the link is gone: keep the entry, the next attach resends it
                    self._kill(live)
                    return
                self._reg.count_op(ws, "route_in", True)
                live.inflight = (qid, seq)
                with live.cv:
                    live.sent_max = max(live.sent_max, qid)
                    live.cv.notify_all()


__all__ = ["AccessAuthz", "Authz", "BoundOnly", "LinkPort", "Router"]
