"""ws->ws over the hub router (hub contract 10.3), host side: send a ticket, receive into the inbox, reply.

The envelope format and every receiver check stay exactly v2 (:mod:`orch.hubclient.wsenvelope`); this module only moves
the bytes over a :class:`RouteLink` (the TLS :class:`~orch.hubclient.tlslink.client.LinkClient` in production) and keeps
the order that makes delivery safe:

* **send**: the envelope is built and the ``sent`` record + the unsent bytes are saved BEFORE anything leaves the host;
  the same bytes are resent after a failure (idempotent: the receiver and the hub key on ``(from_ws, id)``). A hub
  refusal (``not_allowed``, ``queue_full``, ``too_large``, ``replay``) provably delivered nothing, so the record is
  removed and the code is raised.
* **receive**: the exact bytes the hub pushes go through :func:`~orch.hubclient.wsenvelope.receive`, the mailbox is
  saved, and only then is the hub acknowledged. A drop or a refusal is acknowledged WITH an error code (the hub then
  deletes the entry; an unpinned sender gets no answer at all), a stored ticket or reply is acknowledged plain. A
  refusal reply goes out through the outbox like any envelope.

Nothing here logs envelope bytes, keys or ticket text."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

from orch.crypto.encoding import b64u, unb64u

from . import wsenvelope as W
from .errors import HubRefused
from .tlslink.client import LinkError

__all__ = ["PeerService", "RouteLink"]


class RouteLink(Protocol):
    def route_out(self, envelope: bytes) -> str: ...
    def next_route_in(self, wait: float = 0.0) -> bytes | None: ...
    def ack_route_in(self, error: str | None = None) -> None: ...
    def close(self) -> None: ...


class PeerService:
    def __init__(
        self,
        *,
        me: str,
        sign: Callable[[bytes], bytes],
        wxk_for: Callable[[int], tuple[EllipticCurvePrivateKey, int | None] | None],
        wxk_current: int,
        mailbox: W.Mailbox,
        save: Callable[[W.Mailbox], None],
        connect: Callable[[], RouteLink],
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.me, self._sign, self._wxk_for, self._wxk_current = me, sign, wxk_for, wxk_current
        self.mb, self._save, self._connect, self._now = mailbox, save, connect, now_ms
        self._route_mu = threading.Lock()
        self._mu = threading.RLock()  # the mailbox; never held across a blocking read
        self._link: RouteLink | None = None

    # --- link -----------------------------------------------------------------------------------------------------

    def _ensure(self) -> RouteLink:
        if self._link is None:
            self._link = self._connect()
        return self._link

    def drop_link(self) -> None:
        link, self._link = self._link, None
        if link is not None:
            link.close()

    def close(self) -> None:
        self.drop_link()

    # --- sending --------------------------------------------------------------------------------------------------

    def send_ticket(self, to_ws: str, ticket: dict[str, Any], *, deadline_ms: int, depth: int = 0) -> dict[str, Any]:
        """Build, record, send. Returns ``{envelope_id, envelope_sha256, state}`` with state ``forwarded`` | ``queued``
        (the hub has it) or ``outbox`` (the hub is unreachable: kept, resent by :meth:`flush`). Raises
        :class:`~orch.hubclient.wsenvelope.EnvelopeError` (``unknown_peer`` / ``too_large`` / ``malformed``, nothing
        recorded or sent) or :class:`HubRefused` (the hub refused; nothing was delivered and the record is gone)."""
        body = W.new_ticket_body(ticket, deadline_ms=deadline_ms, depth=depth)
        return self._send(to_ws, body, sent={"deadline_ms": deadline_ms, "state": "waiting"})

    def send_result(self, in_reply_to: str, result: dict[str, Any]) -> dict[str, Any]:
        """Answer an inbox ticket (``reply``): a result envelope to its sender, once."""
        with self._mu:
            item = self.mb.inbox.get(in_reply_to)
            if item is None:
                raise W.EnvelopeError("unknown_item")
            if item["state"] == "replied":
                raise W.EnvelopeError("already_replied")
            to_ws, deadline = item["from_ws"], item["deadline_ms"]
            env_id = item["envelope_id"]
        body = W.new_result_body(env_id, result, deadline_ms=deadline)
        out = self._send(to_ws, body, sent=None, mark_replied=in_reply_to)
        return out

    def _send(
        self, to_ws: str, body: dict[str, Any], *, sent: dict[str, Any] | None, mark_replied: str | None = None
    ) -> dict[str, Any]:
        with self._mu:
            built = W.build_envelope(mailbox=self.mb, sign=self._sign, from_ws=self.me, to_ws=to_ws, body=body)
            if sent is not None:
                self.mb.sent[built.envelope_id] = {"to": to_ws, "consumed": [], "sha256": built.sha256, **sent}
            if mark_replied is not None:
                self.mb.inbox[mark_replied]["state"] = "replied"
            self.mb.outbox[built.envelope_id] = b64u(built.envelope)
            self._save(self.mb)
        state = "outbox"
        try:
            state = self._route(built.envelope_id)
        except HubRefused:
            with self._mu:
                self.mb.outbox.pop(built.envelope_id, None)
                self.mb.sent.pop(built.envelope_id, None)
                if mark_replied is not None:
                    self.mb.inbox[mark_replied]["state"] = "accepted"
                self._save(self.mb)
            raise
        except OSError:
            self.drop_link()
        return {"envelope_id": built.envelope_id, "envelope_sha256": built.sha256, "state": state}

    def _route(self, envelope_id: str) -> str:
        """One ``route_out`` of an outbox entry. Maps hub refusals to :class:`HubRefused`; on success the entry
        leaves the outbox. Transport errors propagate as ``OSError`` (the entry stays)."""
        with self._route_mu:  # the sender and the background flush never send the same entry twice
            with self._mu:
                held = self.mb.outbox.get(envelope_id)
            if held is None:
                return "queued"  # the other path already handed it to the hub
            try:
                state = self._ensure().route_out(unb64u(held))
            except LinkError as exc:
                self.drop_link()
                if exc.code in _NOT_DELIVERED:
                    raise HubRefused(exc.code) from None
                raise OSError(exc.code) from None  # an unverifiable hub or link-level failure: keep it, try later
            with self._mu:
                self.mb.outbox.pop(envelope_id, None)
                self._save(self.mb)
            return state

    def flush(self) -> list[str]:
        """Resend every unsent envelope unchanged. A hub refusal drops that entry (and its ``sent`` record: nothing
        was delivered); an unreachable hub stops the pass. Returns the ids the hub took."""
        done: list[str] = []
        with self._mu:
            ids = sorted(self.mb.outbox)
        for eid in ids:
            try:
                self._route(eid)
                done.append(eid)
            except HubRefused:
                with self._mu:
                    self.mb.outbox.pop(eid, None)
                    self.mb.sent.pop(eid, None)
                    self._save(self.mb)
            except OSError:
                self.drop_link()
                break
        return done

    # --- receiving ------------------------------------------------------------------------------------------------

    def pump(self, wait: float = 0.25) -> W.Outcome | None:
        """Take one pushed envelope, store it, acknowledge it. ``None`` when nothing arrived in ``wait`` seconds.
        Link failures raise ``OSError`` (the caller backs off and calls again; the hub redelivers the unacknowledged
        one)."""
        try:
            link = self._ensure()
            raw = link.next_route_in(wait)
            if raw is None:
                return None
            with self._mu:
                out = W.receive(
                    raw, mailbox=self.mb, me=self.me, wxk_for=self._wxk_for, wxk_current=self._wxk_current,
                    sign=self._sign, now_ms=self._now(),
                )  # fmt: skip
                if out.action == "refuse" and out.reply is not None:
                    self.mb.outbox["r-" + raw[24:40].hex() + "-" + raw[8:24].hex()] = b64u(out.reply)
                self._save(self.mb)  # stored BEFORE the acknowledgement
            link.ack_route_in(out.code if out.action in ("drop", "refuse") else None)
        except LinkError as exc:
            self.drop_link()
            raise OSError(exc.code) from None
        except OSError:
            self.drop_link()
            raise
        return out

    # --- reads / inbox verbs --------------------------------------------------------------------------------------

    def inbox(self) -> list[dict[str, Any]]:
        """``orch inbox --json`` items: ``id, from_ws, from_peer, envelope_sha256`` plus state and the deadline."""
        with self._mu:
            return [
                {
                    "id": i,
                    "envelope_id": v["envelope_id"],
                    "from_ws": v["from_ws"],
                    "from_peer": True,
                    "envelope_sha256": v["sha256"],
                    "state": v["state"],
                    "deadline_ms": v["deadline_ms"],
                    "depth": v["depth"],
                }
                for i, v in sorted(self.mb.inbox.items())
            ]  # fmt: skip

    def accept(self, item_id: str) -> dict[str, Any]:
        """Mark an inbox ticket accepted and return it (the ticket body is untrusted data, from a pinned peer)."""
        with self._mu:
            item = self.mb.inbox.get(item_id)
            if item is None:
                raise W.EnvelopeError("unknown_item")
            if item["state"] == "new":
                item["state"] = "accepted"
                self._save(self.mb)
            return dict(item)

    def sent_status(self, envelope_id: str) -> dict[str, Any] | None:
        """``waiting_on_peer`` until a final reply is taken; then ``peer_result{from_ws, summary}`` (or the refusal)."""
        with self._mu:
            s = self.mb.sent.get(envelope_id)
            if s is None:
                return None
            out: dict[str, Any] = {
                "status": "waiting_on_peer" if s["state"] == "waiting" else "answered",
                "to": s["to"],
            }
            r = s.get("reply")
            if r is not None:
                res = r.get("result") or {}
                out["peer_result"] = {"from_ws": r["from_ws"], "summary": res.get("summary"), "late": r["late"]}
                if "code" in r:
                    out["peer_result"]["refusal"] = r["code"]
            return out


#: hub refusals after which the hub provably holds nothing of this envelope
_NOT_DELIVERED = frozenset({"not_allowed", "queue_full", "too_large", "replay", "malformed", "other_workspace",
                            "bad_signature", "not_bound", "unknown_workspace", "wrong_mode"})  # fmt: skip
