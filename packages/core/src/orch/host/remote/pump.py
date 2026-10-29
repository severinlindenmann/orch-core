"""The host thread that serves remote access stream frames from the shared hub link (R2, #1590; design note section 4).

:func:`pump` takes one h2w frame off the link (``LinkClient.next_stream``) and handles it, like
:func:`orch.host.hubdeliver.pump` does for ``event_deliver``:

- ``stream_open``: judged by :func:`gate.open` on the workspace's worker thread (the store belongs to it) and answered
  ``{}`` or the gate's code. Until R3 a passing open is answered ``remote.version``.
- ``stream_data`` of a stream the host does not hold is dropped (no reply exists, nothing is sent back); of an open
  stream it is checked against the stream's ``seq`` and byte budget (R3 reads the bytes).
- ``stream_close``: the stream is forgotten (its revoker runs).

After every frame the ``stream_close`` frames :class:`gate.StreamTable` queued (revocation, budget, bad ``seq``) are
sent, outside the store lock. :func:`run` loops it on the shared link (``peerops.shared_link``)."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from orch.hubclient.tlslink.client import LinkError

from . import gate

if TYPE_CHECKING:
    from orch.host.workspace import WorkspaceService

_log = logging.getLogger("orch.host.remote.pump")


def flush_closing(service: WorkspaceService, link: Any) -> int:
    """Send the queued ``stream_close`` frames. A refused frame is dropped (the stream is closed either way)."""
    n = 0
    for stream_id, reason in gate.table(service).take_closing():
        try:
            link.send_stream("stream_close", {"stream_id": stream_id, "reason": reason})
            n += 1
        except LinkError:
            _log.warning("remote.close_frame_refused")
    return n


def pump(service: WorkspaceService, link: Any, wait: float = 0.0) -> bool:
    """Take one stream frame off ``link`` (if any) and handle it. ``True`` when one was handled."""
    flush_closing(service, link)  # a revocation that happened between two frames goes out now
    got = link.next_stream(wait)
    if got is None:
        return False
    op, seq, body = got
    now_ms = int(service.clock() * 1000)
    if op == "stream_open":
        code = service.submit(gate.open, service, body, now_ms).result(timeout=120)
        link.answer_stream_open(seq, code)
    elif op == "stream_data":
        gate.table(service).inbound(body, now_ms)  # unknown stream: dropped; bad seq or over budget: closed
    else:
        gate.table(service).peer_closed(body["stream_id"])
    flush_closing(service, link)
    return True


def run(service: WorkspaceService, get_link: Any, stop: threading.Event, wait: float = 0.5) -> None:
    """Serve frames until ``stop`` is set. ``get_link()`` is ``peerops.shared_link`` (``None``: no hub link now)."""
    while not stop.is_set():
        try:
            link = get_link()
            if link is None or not pump(service, link, wait):
                stop.wait(0.05 if link is not None else 1.0)
        except Exception as exc:  # noqa: BLE001 - the link failed: the owner of the link redials; never die
            _log.warning("remote.pump_error %s", type(exc).__name__)
            stop.wait(1.0)
