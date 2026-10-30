"""The runner of a workspace's dedicated stream link (R4 follow-up, #1592; design note r4-selective-link-design.md).

Remote access streams travel on ONE link per workspace that is not shared with anything else: it is dialled with the
ALPN ``orch-link-streams/1`` (:data:`STREAM_LINK_ALPN`), which tells the hub to attach it to its stream router and to
accept on it only stream frames, ``stream_open`` replies and the ``hub_where`` keepalive. The same link serves both hub
modes. Short per-op links (status, shares, approvals, ...) never carry streams and never steal the stream route.

One :class:`Runner` per workspace (registry keyed like ``peerops``). It runs while the workspace is bound to a hub AND
(remote access is on and its end date is not reached, OR an enrollment code is outstanding): the enroll stream is
allowed while remote is off, so the link must be up then too. What the hub learns from this is that a workspace is
remote-reachable (the link exists only then), plus its timing; no new bytes beyond frames it already carries.

The inputs (``on``, ``until_ms``, the binding, the latest code end) are computed by :func:`refresh` from the commit
hook, :func:`remote.attach` (workspace open) and ``enrollment.issue``, all under the store lock, and cached on the
runner: the runner thread never reads the store for them. ``refresh`` only sets them and starts the thread; it never
blocks.

When the want turns false the runner sends what is queued (so an open session ends ``close remote.off`` at the
client, not ``closed``), closes the link and exits. ``workspace.close`` calls :func:`stop`."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from orch.hubclient.tlslink.client import STREAM_LINK_ALPN, LinkClosed, LinkError, LinkTimeout

from . import policy, pump, session

if TYPE_CHECKING:
    pass

__all__ = ["KEEPALIVE_S", "Runner", "STREAM_LINK_ALPN", "current", "refresh", "stop"]

_log = logging.getLogger("orch.host.remote.linkrun")

#: the host sends a ``hub_where`` this often on an otherwise quiet stream link (the hub drops a link after 300 s)
KEEPALIVE_S = 120.0
BACKOFF_FIRST_S = 0.5
BACKOFF_MAX_S = 10.0
STOP_WAIT_S = 5.0
_PUMP_WAIT_S = 0.05
_IDLE_RECHECK_S = 1.0

_RUNNERS: dict[tuple[str, str], Runner] = {}
_MU = threading.Lock()


def _key(service: Any) -> tuple[str, str]:
    return (str(service.state_dir), str(service.workspace_id))


class Runner:
    """The inputs and the thread of one workspace's stream link."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.mu = threading.Lock()
        self.on = False
        self.until_ms: int | None = None
        self.code_until_ms = 0
        self.binding: tuple[str, int] | None = None  # (hub_id, binding_seq) of a joined workspace
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.link: Any = None
        self.link_binding: tuple[str, int] | None = None
        self.last_error: str | None = None

    # -- the inputs ------------------------------------------------------------------------------------------------

    def want(self, now_ms: int) -> bool:
        with self.mu:
            if self.binding is None:
                return False
            if self.on and (self.until_ms is None or now_ms < self.until_ms):
                return True
            return now_ms < self.code_until_ms

    def _binding(self) -> tuple[str, int] | None:
        with self.mu:
            return self.binding

    def set_inputs(self, on: bool, until_ms: int | None, code_until_ms: int, binding: tuple[str, int] | None) -> None:
        with self.mu:
            self.on, self.until_ms, self.binding = on, until_ms, binding
            self.code_until_ms = code_until_ms

    # -- the thread ------------------------------------------------------------------------------------------------

    def ensure_started(self) -> None:
        """Start the thread if it is wanted now and none runs. Never blocks."""
        if self.stop_event.is_set() or not self.want(self._now()):
            return
        with self.mu:
            if self.thread is not None and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._run, name=f"orch-streams-{self.service.workspace_id[:8]}")
            self.thread.daemon = True
            self.thread.start()

    def _now(self) -> int:
        return int(self.service.clock() * 1000)

    def _run(self) -> None:
        backoff = BACKOFF_FIRST_S
        last_keepalive = 0.0
        try:
            while not self.stop_event.is_set():
                if not self.want(self._now()):
                    if self._idle_exit():
                        return
                    continue
                binding = self._binding()
                if self.link is not None and binding != self.link_binding:  # rebound: the old link ends
                    self._drop_link(streams=True)
                if self.link is None:
                    if not self._dial(binding):
                        self.stop_event.wait(backoff)
                        backoff = min(backoff * 2, BACKOFF_MAX_S)
                        continue
                    backoff = BACKOFF_FIRST_S
                    last_keepalive = time.monotonic()
                try:
                    handled = pump.pump(self.service, self.link, _PUMP_WAIT_S)
                    if time.monotonic() - last_keepalive >= KEEPALIVE_S:
                        last_keepalive = time.monotonic()
                        self._keepalive()
                    if not handled:
                        self.stop_event.wait(0.01)
                except (LinkClosed, LinkError, LinkTimeout, OSError) as exc:  # the link failed: redial
                    self.last_error = type(exc).__name__
                    self._drop_link(streams=False)  # the next pump sees a new link (F4) and closes the old streams
                except Exception as exc:  # noqa: BLE001 - never die: a bug in one frame must not end remote access
                    _log.warning("remote.streams_error %s", type(exc).__name__)
                    self.last_error = type(exc).__name__
                    self.stop_event.wait(1.0)
        finally:
            self._close_link()

    def _dial(self, binding: tuple[str, int] | None) -> bool:
        from orch.host import peerops

        try:
            link = peerops._open_link(self.service, alpn=STREAM_LINK_ALPN)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 - unreachable, refused, not negotiated: back off and retry
            self.last_error = type(exc).__name__
            return False
        if self.stop_event.is_set() or not self.want(self._now()):
            link.close()
            return True
        self.link, self.link_binding, self.last_error = link, binding, None
        return True

    def _keepalive(self) -> None:
        try:
            self.link.call("hub_where", {})
        except LinkError:
            if not self.link.is_open:
                raise  # a signed refusal (the test hub has no addresses) keeps the link; anything else ends it

    def _idle_exit(self) -> bool:
        """Not wanted: send what is queued, close the link. The thread exits unless a wake wanted it again meanwhile."""
        self._drain_close()
        with self.mu:
            if self.thread is threading.current_thread() and not self._wants_locked():
                self.thread = None
                return True
        return False

    def _wants_locked(self) -> bool:
        if self.binding is None:
            return False
        now = self._now()
        return (self.on and (self.until_ms is None or now < self.until_ms)) or now < self.code_until_ms

    def _drain_close(self) -> None:
        link = self.link
        if link is not None:
            try:
                pump.flush_closing(self.service, link)  # remote.disabled's close_all("remote.off") reaches the client
                session.log_ended(self.service)
            except Exception as exc:  # noqa: BLE001 - the link may be gone; the streams end either way
                _log.warning("remote.streams_drain_failed %s", type(exc).__name__)
        self._drop_link(streams=True)

    def _drop_link(self, *, streams: bool) -> None:
        link, self.link, self.link_binding = self.link, None, None
        if link is not None:
            link.close()
        if streams:
            pump._drop_streams(self.service)  # noqa: SLF001
            setattr(self.service, pump._LINK_ATTR, None)  # noqa: SLF001

    def _close_link(self) -> None:
        link, self.link = self.link, None
        if link is not None:
            link.close()

    def stop(self, wait: float = STOP_WAIT_S) -> None:
        self.stop_event.set()
        t = self.thread
        if t is not None and t is not threading.current_thread():
            t.join(wait)


# -- the registry and the entry points -----------------------------------------------------------------------------


def current(service: Any) -> Runner | None:
    with _MU:
        return _RUNNERS.get(_key(service))


def _runner(service: Any) -> Runner:
    with _MU:
        r = _RUNNERS.get(_key(service))
        if r is None or r.service is not service:
            r = _RUNNERS[_key(service)] = Runner(service)
        return r


def refresh(service: Any) -> None:
    """Recompute the inputs from the model and the outstanding codes, then start the thread when it is wanted. Called
    with the store lock held (commit hook, attach, ``enrollment.issue``). Never raises; never blocks."""
    if getattr(service, "_linkrun_stopped", False):
        return  # the workspace is closing: no runner may start again
    try:
        from orch.store.render import thaw

        from . import enrollment

        ws = service.store.state.workspace
        hub = dict(thaw(ws.hub) or {})
        remote = dict(thaw(ws.remote) or {})
        now = int(service.clock() * 1000)
        binding = (
            (hub["hub_id"], int(hub["binding_seq"]))
            if hub.get("status") == "joined" and type(hub.get("hub_id")) is str and "binding_seq" in hub
            else None
        )
        codes = enrollment.outstanding(service, now)
        r = _runner(service)
        pol = policy.effective(remote, now)
        r.set_inputs(pol.on, pol.until_ms, max(codes.values(), default=0), binding)
        r.ensure_started()
    except Exception:  # noqa: BLE001
        _log.exception("remote link runner refresh failed")


def stop(service: Any, wait: float = STOP_WAIT_S) -> None:
    """``workspace.close``: end the runner (its link too), before the store goes."""
    service._linkrun_stopped = True  # noqa: SLF001
    with _MU:
        r = _RUNNERS.pop(_key(service), None)
    if r is not None:
        r.stop(wait)


def _reset_for_tests() -> None:
    with _MU:
        runners = list(_RUNNERS.values())
        _RUNNERS.clear()
    for r in runners:
        r.stop(1.0)
