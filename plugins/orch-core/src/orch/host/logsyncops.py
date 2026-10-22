"""The host's ``log_sync`` sender thread (V3 C14; hub contract 10.7): keeps the hub's WK_e view of this workspace's logs
current. Full mode only, and only after the join is complete (bound, ``wk_seal`` taken); never in selective mode.

One daemon thread per workspace. It runs a pass (``orch.hubclient.logsync.sync_logs``) when the workspace commits an
event (``wake``, debounced so a burst is one pass), once when it starts (a join or connect just finished, or the host
opened a workspace that is bound in full mode) and every ``IDLE_S`` as a cheap local check. A pass opens its own link
and closes it; nothing is sent when the local logs equal the hub's acknowledged position. The position is kept in
``hub/logsync.json`` (0600, per hub and binding) and saved after every accepted page.

It reads the store only through its read-only views (``events_after``, ``head_at``, ``log_seq``, ``log_marks``), never
appends, and never touches ``WK_e``: the key store hands out ``K_view(epoch)`` only (:meth:`hubops._Epochs.view_key`).
A rotation is followed, not made: pages use the epoch last sealed to the hub; sealing a new epoch stays the owner's
``orch hub connect`` (``reseal_needed`` in ``hub.status``). Failures back off and never kill the host; nothing here logs
line content or keys."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .workspace import WorkspaceService

__all__ = ["DEBOUNCE_S", "IDLE_S", "start", "stop", "wake"]

LOG = logging.getLogger("orch.host.logsync")
DEBOUNCE_S = 0.5
IDLE_S = 30.0
MAX_BACKOFF_S = 60.0
STOP_WAIT_S = 5.0  # how long a leave/switch/unbind waits for a pass that is on the network
RESEND_LIMIT = 6  # hub-forced resends from seq 1 per hour (L2-C14-3)
RESEND_WINDOW_MS = 3_600_000
_MU = threading.Lock()
#: the seam for the link: ``(service) -> object with call(op, body) and close()``; ``None``: the ws->ws path's TLS link
OPEN_LINK: Any = None
_RUNTIMES: dict[tuple[str, str], _Runtime] = {}


class _Runtime:
    def __init__(self, hub_id: str, binding_seq: int) -> None:
        self.hub_id, self.binding_seq = hub_id, binding_seq
        self.wake, self.stop = threading.Event(), threading.Event()
        self.thread: threading.Thread | None = None
        self.position: Any = None
        self.last_error: str | None = None


def _key(service: WorkspaceService) -> tuple[str, str]:
    return (str(service.state_dir), str(service.workspace_id))


class StoreSource:
    """:class:`orch.hubclient.LogSource` over the workspace store's read-only views."""

    def __init__(self, store: Any) -> None:
        self._s = store

    def logs(self) -> list[str]:
        return ["workspace", *sorted(k for k in self._s.log_marks() if k != "workspace")]

    def seq(self, log: str) -> int | None:
        return self._s.log_seq(log)

    def head_at(self, log: str, seq: int) -> str | None:
        return self._s.head_at(log, seq)

    def after(self, log: str, after: int, limit: int) -> list[dict[str, Any]]:
        return [e for e, _head in self._s.events_after(log, after, limit)]


def _eligible(state: Any) -> bool:
    """Full mode, bound, and a ``wk_seal`` the hub took: the join is complete. Any doubt is no."""
    return (
        state is not None
        and state.mode == "full"
        and state.status == "bound"
        and state.sealed is not None
        and state.rotate_after is None
    )


def _path(service: WorkspaceService) -> Path:
    from . import hubops

    return hubops._dir(service) / "logsync.json"  # noqa: SLF001 - the one place the hub's files live


def _load_position(service: WorkspaceService, state: Any) -> Any:
    from orch.hubclient import Position

    from .custody import read_json

    try:
        doc = read_json(_path(service), service.host_uid, limit=16 << 20)
    except Exception:  # noqa: BLE001 - unreadable: start from nothing, the hub takes a resend idempotently
        doc = None
    return Position.from_json(doc, hub_id=state.hub_id, binding_seq=state.binding_seq)


def _save_position(service: WorkspaceService, position: Any) -> None:
    from orch.store.fsio import write_atomic

    from . import hubops
    from .custody import ensure_private_dir

    d = hubops._dir(service)  # noqa: SLF001
    for level in (d.parent, d):
        ensure_private_dir(level, service.host_uid)
    write_atomic(_path(service), json.dumps(position.to_json(), sort_keys=True).encode(), mode=0o600)


def wake(service: WorkspaceService) -> None:
    """A commit happened (called from the store's commit hook, under its lock): set an event, nothing else."""
    rt = _RUNTIMES.get(_key(service))
    if rt is not None:
        rt.wake.set()


def start(service: WorkspaceService) -> bool:
    """Start (or keep, or replace after a rebinding) the sender for this workspace if it is eligible. Never raises."""
    from . import hubops

    try:
        state = hubops._load_state(service)  # noqa: SLF001
        k = _key(service)
        with _MU:
            rt = _RUNTIMES.get(k)
            if not _eligible(state):
                if rt is not None:
                    _retire(k, rt)
                return False
            if rt is not None and (rt.hub_id, rt.binding_seq) == (state.hub_id, state.binding_seq):
                rt.wake.set()
                return True
            if rt is not None:
                _retire(k, rt)
            rt = _Runtime(state.hub_id, state.binding_seq)
            rt.position = _load_position(service, state)
            rt.thread = threading.Thread(target=_loop, args=(service, rt), name=f"orch-logsync-{k[1][:8]}", daemon=True)
            _RUNTIMES[k] = rt
            rt.wake.set()  # the first pass: link connect
            rt.thread.start()
        return True
    except Exception:  # noqa: BLE001 - never fails the open or the hub op that called it
        LOG.warning("logsync of %s did not start", service.workspace_id)
        return False


def stop(service: WorkspaceService, wait: float = 0.0) -> None:
    """End the sender. With ``wait`` (a leave, a mode switch or an unbind, BEFORE its op is sent) also wait up to that
    many seconds for a pass that is on the network; the pass checks ``stop`` before every page, so once this returns
    (or the wait ran out) no further page goes out (L2-C14-1)."""
    k = _key(service)
    with _MU:
        rt = _RUNTIMES.get(k)
        if rt is not None:
            _retire(k, rt)
    if rt is not None and wait > 0 and rt.thread is not None and rt.thread is not threading.current_thread():
        rt.thread.join(wait)


def _retire(k: tuple[str, str], rt: _Runtime) -> None:
    """Forget ``rt`` and ask its thread to end (the caller holds ``_MU``; the thread is not joined here: a pass may be
    on the network, and it checks ``stop`` before every page; :func:`stop` can wait)."""
    if _RUNTIMES.get(k) is rt:
        del _RUNTIMES[k]
    rt.stop.set()
    rt.wake.set()


def status(service: WorkspaceService) -> dict[str, Any] | None:
    rt = _RUNTIMES.get(_key(service))
    if rt is None:
        return None
    return {"running": not rt.stop.is_set(), "logs": {k: s for k, (s, _h) in rt.position.logs.items()},
            "last_error": rt.last_error}  # fmt: skip


def _loop(service: WorkspaceService, rt: _Runtime) -> None:
    backoff = 1.0
    while not rt.stop.is_set():
        rt.wake.wait(IDLE_S)
        if rt.stop.is_set():
            return
        rt.stop.wait(DEBOUNCE_S)  # a burst of commits is one pass
        rt.wake.clear()
        if rt.stop.is_set():
            return
        try:
            _pass(service, rt)
            rt.last_error = None
            backoff = 1.0
        except Exception as exc:  # noqa: BLE001 - the hub is down or refused: back off, never die
            code = getattr(exc, "code", None)
            rt.last_error = code if isinstance(code, str) else type(exc).__name__
            if code == "resend_limited":  # the hub keeps forcing full resends: slow down to the longest pause, say why
                LOG.warning("logsync of %s: the hub forced too many resends; backing off", service.workspace_id)
                rt.stop.wait(MAX_BACKOFF_S)
                backoff = MAX_BACKOFF_S
                rt.wake.set()
                continue
            if code in ("not_joined", "wrong_mode", "not_bound"):
                with _MU:  # left, unbound or switched: no more sending (a new connect starts a new runtime)
                    _retire(_key(service), rt)
                return
            rt.stop.wait(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)
            rt.wake.set()


def _pass(service: WorkspaceService, rt: _Runtime) -> None:
    from orch.hubclient import HubRefused, pending, sync_logs

    from . import hubops, peerops

    state = hubops._load_state(service)  # noqa: SLF001 - every pass: a leave or a rebinding ends this runtime
    if not _eligible(state) or (state.hub_id, state.binding_seq) != (rt.hub_id, rt.binding_seq):
        raise HubRefused("not_joined")
    store = service.store
    epochs = hubops.epochs_for(service)
    if store is None or epochs is None:
        raise HubRefused("no_epoch")
    source = StoreSource(store)
    if not pending(source, rt.position):
        return
    epoch = int(state.sealed["epoch"])

    def live() -> bool:
        """Before every page, under the hub-state lock on freshly read state: still full, joined, the same hub and
        binding, the same sealed epoch, and this runtime not stopped (L2-C14-1)."""
        if rt.stop.is_set():
            return False
        with hubops._MU:  # noqa: SLF001
            cur = hubops._load_state(service)  # noqa: SLF001
        return (
            _eligible(cur)
            and (cur.hub_id, cur.binding_seq) == (rt.hub_id, rt.binding_seq)
            and int(cur.sealed["epoch"]) == epoch
            and not rt.stop.is_set()
        )

    def allow_resend() -> bool:
        now = int(service.clock() * 1000)
        recent = [t for t in rt.position.forced if now - t < RESEND_WINDOW_MS]
        if len(recent) >= RESEND_LIMIT:
            rt.position.forced = recent
            return False
        rt.position.forced = [*recent, now]
        _save_position(service, rt.position)
        return True

    link = (OPEN_LINK or peerops._open_link)(service)  # noqa: SLF001 - the same dial as the ws->ws link
    try:
        sync_logs(
            state=state, workspace_id=service.workspace_id, call=link.call, source=source,
            view_key=epochs.view_key, position=rt.position, save=lambda p: _save_position(service, p),
            live=live, allow_resend=allow_resend,
        )  # fmt: skip
    finally:
        link.close()
