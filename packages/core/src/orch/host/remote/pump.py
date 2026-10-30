"""The host thread that serves remote access stream frames from the shared hub link (R2, #1590; design note section 4).

:func:`pump` takes one h2w frame off the link (``LinkClient.next_stream``) and handles it, like
:func:`orch.host.hubdeliver.pump` does for ``event_deliver``:

- ``stream_open``: judged by :func:`gate.open` on the workspace's worker thread (the store belongs to it) and answered
  ``{}`` or the gate's code (``remote.version`` while no dashboard adapter is attached).
- ``stream_data`` of a stream the host does not hold is dropped (no reply exists, nothing is sent back); of an open
  stream it is checked against the stream's ``seq`` and byte budget, then handed to its :mod:`session`.
- ``stream_close``: the stream is forgotten (its revoker runs).

Before and after every frame the sessions are swept (handshake, life, idle), the data frames they queued are sent, then
the ``stream_close`` frames :class:`gate.StreamTable` queued (revocation, budget, bad ``seq``), all outside the store
lock, and ``remote.session_closed`` is appended for each opened session that ended. :func:`run` loops it on the
shared link (``peerops.shared_link``)."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from orch.crypto.encoding import EncodingError, unb64u
from orch.hubclient.tlslink.client import LinkError

from . import gate, session

if TYPE_CHECKING:
    from orch.host.workspace import WorkspaceService

_log = logging.getLogger("orch.host.remote.pump")


def flush_closing(service: WorkspaceService, link: Any) -> int:
    """Send the queued data frames, then the queued ``stream_close`` frames (the closes are taken FIRST, so a frame
    queued before its stream's close always goes out before it). A refused frame is dropped (the stream is closed
    either way)."""
    n = 0
    closing = gate.table(service).take_closing()
    session.flush_out(service, link)
    for stream_id, reason in closing:
        try:
            link.send_stream("stream_close", {"stream_id": stream_id, "reason": reason})
            n += 1
        except LinkError:
            _log.warning("remote.close_frame_refused")
    return n


_LINK_ATTR = "_remote_pump_link"


def link_changed(service: WorkspaceService, link: Any) -> bool:
    """F4 (R4): streams belong to the link that carried their ``stream_open``. When the link the pump is given is not
    the one it served last (a redial, a rebind), every stream of the old link is closed ``closed`` at once, freeing
    the per-device and per-workspace limits. NO frame is sent for them (the old link is gone, the hub closed its side
    too, and the new link must not carry ids it never opened); frames the sessions still owed are discarded."""
    prev = getattr(service, _LINK_ATTR, None)
    if prev is link:
        return False
    setattr(service, _LINK_ATTR, link)
    if prev is None:
        return False
    _drop_streams(service)
    return True


def _drop_streams(service: WorkspaceService) -> None:
    gate.table(service).close_all("closed")
    gate.table(service).take_closing()
    for s in session.registry(service).all():
        s.drain()


def pump(service: WorkspaceService, link: Any, wait: float = 0.0) -> bool:
    """Take one stream frame off ``link`` (if any) and handle it. ``True`` when one was handled."""
    now_ms = int(service.clock() * 1000)
    link_changed(service, link)
    overflow = getattr(link, "take_stream_overflow", None)  # F3: a full queue lost a frame of these streams
    for sid in overflow() if overflow is not None else ():
        gate.table(service).close(sid, "remote.busy")
    session.sweep(service, now_ms)
    flush_closing(service, link)  # a revocation that happened between two frames goes out now
    session.log_ended(service)
    got = link.next_stream(wait)
    if got is None:
        return False
    op, seq, body = got
    now_ms = int(service.clock() * 1000)
    if op == "stream_open":
        hub_id = getattr(link, "hub_id", None)
        code = service.submit(lambda: gate.open(service, body, now_ms, hub_id=hub_id)).result(timeout=120)
        link.answer_stream_open(seq, code)
    elif op == "stream_data":
        # unknown stream: dropped; bad seq or over budget: closed; otherwise the session reads the bytes
        if gate.table(service).inbound(body, now_ms) is None and gate.table(service).is_open(body["stream_id"]):
            try:
                raw = unb64u(body["bytes"])
            except EncodingError:
                gate.table(service).close(body["stream_id"], "malformed")
            else:
                session.feed(service, body["stream_id"], raw, now_ms)
    else:
        gate.table(service).peer_closed(body["stream_id"])
    flush_closing(service, link)
    session.log_ended(service)
    return True


def run(service: WorkspaceService, get_link: Any, stop: threading.Event, wait: float = 0.05) -> None:
    """Serve frames until ``stop`` is set. ``get_link()`` is ``peerops.shared_link`` (``None``: no hub link now)."""
    while not stop.is_set():
        try:
            link = get_link()
            if link is None and getattr(service, _LINK_ATTR, None) is not None:  # link gone: its streams too (F4)
                _drop_streams(service)
                setattr(service, _LINK_ATTR, None)
            if link is None or not pump(service, link, wait):
                stop.wait(0.05 if link is not None else 1.0)
        except Exception as exc:  # noqa: BLE001 - the link failed: the owner of the link redials; never die
            _log.warning("remote.pump_error %s", type(exc).__name__)
            stop.wait(1.0)
