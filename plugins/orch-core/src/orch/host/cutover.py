"""The H5 switch on the host: hub mode stops the direct relay path (V3 C10, design-c10 M1 to M4, spec D68/D69/D81).

**What it is.** The owner's signed full-mode ``hub.joined`` (C2b, whose review now says "your phones' direct pairing
ends; the hub becomes the only path") authorizes the cut-over; this module is the host's resumable state machine
(``HubState.direct``: ``open`` -> ``closing`` -> ``closed``), run by ``hub.connect`` after a full join or a mode switch
and resumed at every host start. There is no new signed verb: the owner's own signature is the only authority, and
neither an agent, the click signer, ``auth: "hub"`` nor the hub can start it or reverse it.

1. **preflight**: the link is bound, the hub card is held and a snapshot with at least one ``admin`` is pinned (else
   stay ``open`` with ``hub.not_ready``: never strand a workspace nobody can sign for).
2. **closing** (saved first). From here the host behaves as ``closed`` (fail closed): no relay thread starts
   (``relayhook``, ``relaylink.start_blocker``), every relay connection passes the :mod:`.directpath` gate, and the
   direct ops refuse with ``relay.cut_over``. A running relay worker is stopped.
3. **rotate**: one local epoch whose member list is EMPTY (``advance_local(cut_over=True, drop_members=True)``: no
   direct device is in the epoch that is sealed to the hub alone, I4), then ``wk_seal`` of it to the hub. Resumable:
   ``HubState.cut_epoch`` records the epoch made.
4. **relay** (only if the workspace is relay-linked): ``PUT /v2/workspaces/{ws}/cutover {v: 2, hub_id, state:
   "cut_over"}`` with the workspace's own session and ``Orch-Holder`` (design M2.4). ``unknown_hub`` leaves it
   ``closing`` and says so. A relay-less workspace has nothing to cut at a relay and is done after step 3.
5. **closed**. ``direct`` stays ``closed`` through a ``hub.left`` (C8 I6): only :func:`reopen` (the owner, after
   leaving) opens it again.

Deviation from design M2.3 (logged in the PR): no ``PUT membership`` is sent to the relay. The new epoch is local and
its list is empty, and the relay stops serving the workspace's devices with the ``cut_over`` itself.

Failures in steps 3 and 4 do not raise: the state stays ``closing`` with a short ``direct_note`` (``orch hub status``,
``orch doctor``) and :func:`start` retries with backoff (first after 30 s, doubling to an hour)."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import WorkspaceService

__all__ = ["TRANSPORT", "cutover_delete", "needs_run", "preflight", "reopen", "run", "start", "status_fields"]

LOG = logging.getLogger("orch.host.cutover")

#: tests only: ``(origin, link) -> transport`` for the relay session (as ``relayhook.make_starter(transport=)``)
TRANSPORT: Callable[[str, Any], Any] | None = None
_BACKOFF_FIRST_S, _BACKOFF_MAX_S = 30.0, 3600.0
_THREADS: dict[tuple[str, str], threading.Thread] = {}
_THREADS_MU = threading.Lock()


# -- the facts ---------------------------------------------------------------------------------------------------------


def _hubops() -> Any:
    from . import hubops

    return hubops


def _model(service: WorkspaceService) -> dict[str, Any]:
    """The model's hub record (worker thread's view; a copy)."""
    return service.submit(lambda: dict(service.store.state.workspace.hub)).result(timeout=60)


def _full_bound(model: dict[str, Any], state: Any) -> bool:
    return (
        state is not None
        and model.get("status") == "joined"
        and model.get("mode") == "full"
        and state.status == "bound"
        and state.mode == "full"
        and state.hub_id == model.get("hub_id")
        and state.binding_seq == model.get("binding_seq")
    )


def preflight(state: Any) -> str | None:
    """Why the cut-over may not start, or ``None``. The link must be authenticated (``bound``), the hub's card held and
    a snapshot pinned that names at least one ``admin``: without someone the hub can act for, the workspace would be
    stranded (D78: no local human signature exists once the path is closed)."""
    if state is None or state.status != "bound" or state.mode != "full":
        return "the hub link is not bound in full mode"
    if state.card is None:
        return "the hub card is not held"
    roles = ((state.snapshot or {}).get("o") or {}).get("roles")
    if not isinstance(roles, dict) or "admin" not in roles.values():
        return "no group snapshot with an admin is pinned yet: orch hub refresh"
    return None


def needs_run(service: WorkspaceService, model: dict[str, Any], state: Any) -> bool:
    """Should the host run (or resume) the cut-over now? ``closing``: yes. ``open`` set by a C10 connect: yes (retry
    the preflight). ``None`` (a join made before C10: its review never said the phones' pairing ends): only a dev
    workspace holding a dev hub card, so the e2e and demo workspaces move over by themselves; every other one waits
    for the owner (``orch hub status`` says so)."""
    if not _full_bound(model, state):
        return False
    if state.direct in ("closing", "open"):
        return True
    if state.direct == "closed":
        return False
    dev_card = ((state.card or {}).get("o") or {}).get("dev") is True
    return dev_card and _hubops().workspace_dev(service)


def status_fields(service: WorkspaceService, state: Any) -> dict[str, Any]:
    """The cut-over part of ``hub.status``: ``direct`` (``open`` | ``closing`` | ``closed``), the reason it is not done,
    the epoch the rotation made and the direct devices still listed (none once ``closed``)."""
    direct = "open" if state is None or state.direct is None else state.direct
    devices: list[str] = []
    try:
        epochs = _hubops().epochs_for(service)
        mk = getattr(epochs, "_mk", None)
        if mk is not None:
            devices = sorted({d for d, _p in mk.listed_devices() if d})
    except Exception:  # noqa: BLE001 - an unreadable key store lists nothing here; hub.status stays an answer
        devices = []
    return {
        "direct": direct,
        "direct_note": None if state is None else state.direct_note,
        "direct_devices": devices,
        "cut_epoch": None if state is None else state.cut_epoch,
        "direct_pre_c10": state is not None and state.direct is None and state.mode == "full",
    }


# -- the machine -------------------------------------------------------------------------------------------------------


def _relay_linked(service: WorkspaceService) -> bool:
    from .relaylink import link_path

    return os.path.lexists(link_path(service.state_dir, service.workspace_id))


def _note(service: WorkspaceService, state: Any, text: str | None) -> None:
    state.direct_note = None if text is None else text[:300]
    _hubops()._save_state(service, state)  # noqa: SLF001


def run(
    service: WorkspaceService,
    model: dict[str, Any] | None = None,
    *,
    hub_card: Any = None,
    force_start: bool = False,
) -> str:
    """Run or resume the cut-over; returns ``direct`` afterwards (``open`` | ``closing`` | ``closed``). Does not raise
    for a hub or relay that is down: the state stays ``closing`` with ``direct_note``. ``hub_card``: the card the owner
    just supplied (hub.connect), else the one held. ``force_start``: the caller (a C10 connect after a full join or
    mode switch) has the owner's new review text behind it, so a state with ``direct`` unset starts too."""
    h = _hubops()
    model = model if model is not None else _model(service)
    state = h._load_state(service)  # noqa: SLF001
    if not _full_bound(model, state):
        return "open" if state is None or state.direct is None else str(state.direct)
    if state.direct == "closed":
        return "closed"
    if state.direct is None and not force_start and not needs_run(service, model, state):
        return "open"
    if state.direct in (None, "open"):
        why = preflight(state)
        if why is not None:
            state.direct = state.direct or "open"
            _note(service, state, f"hub.not_ready: {why}")
            return "open"
        state.direct, state.direct_note = "closing", None
        h._save_state(service, state)  # noqa: SLF001 - saved before anything else: from here the host is closed
    try:
        _stop_relay(service)
        _rotate_and_seal(service, state, hub_card)
        state = h._load_state(service)  # noqa: SLF001
        _relay_cut(service, state)
    except HostError as exc:
        LOG.warning("cut-over of %s stays closing: %s", service.workspace_id, exc.code)
        _note(service, h._load_state(service), f"{exc.code}: {exc.message}")  # noqa: SLF001
        return "closing"
    except Exception as exc:  # noqa: BLE001 - a hub or relay that is down is not an error of the owner
        LOG.warning("cut-over of %s stays closing: %s", service.workspace_id, type(exc).__name__)
        _note(service, h._load_state(service), f"{type(exc).__name__}: the hub or the relay did not answer")  # noqa: SLF001
        return "closing"
    state = h._load_state(service)  # noqa: SLF001
    state.direct, state.direct_note = "closed", None
    h._save_state(service, state)  # noqa: SLF001
    LOG.info("the direct relay path of %s is closed", service.workspace_id)
    return "closed"


def _stop_relay(service: WorkspaceService) -> None:
    """The relay worker ends (its threads: poll, publisher, pairing, push). The persistent stop marker stays: a restart
    never brings a worker back, and ``relayhook`` refuses a start for a closed workspace anyway."""
    if service.relay is None and not service._stuck_alive():  # noqa: SLF001
        return
    result = service.relay_stop()
    if result == "stuck":
        raise HostError("relay.cut_over", "a relay thread of this workspace has not ended yet: retrying")


def _rotate_and_seal(service: WorkspaceService, state: Any, hub_card: Any) -> None:
    """Step 3: the cut-over's own epoch (no device in it) and its seal to the hub."""
    from orch.crypto.encoding import unb64u
    from orch.hubclient import HubRefused, seal_to_hub

    h = _hubops()
    epochs = h.epochs_for(service, create=True)
    if epochs is None or not hasattr(epochs, "rotate_cut_over"):
        raise HostError("hub.no_epoch", "the workspace key store is not available for the cut-over")
    if state.cut_epoch is None:
        epochs.ensure_epoch()
        epochs.rotate_cut_over()
        state.cut_epoch = epochs.current_epoch()
        h._save_state(service, state)  # noqa: SLF001
    if state.sealed is not None and state.sealed["epoch"] >= state.cut_epoch:
        return
    wsk_pub, sign = h._wsk_sign(service)  # noqa: SLF001
    dev = h.workspace_dev(service)
    link = {
        "addr": h._addr({}, state, dev), "spki": state.tls_spki, "hk_op": unb64u(state.hk_op_pub, 65),  # noqa: SLF001
        "wsk_pub": wsk_pub, "sign": sign, "mode": "full",
    }  # fmt: skip
    transport = h._transport(service, state.hub_id, link)  # noqa: SLF001
    try:
        seal_to_hub(
            state=state, hub_card=hub_card if hub_card is not None else state.card,
            workspace_id=service.workspace_id, sign_wsk=sign, dev_mode=dev,
            now_ms=lambda: int(service.clock() * 1000), transport=transport,
            save=lambda s: h._save_state(service, s), epochs=epochs,  # noqa: SLF001
        )  # fmt: skip
    except HubRefused as exc:
        raise HostError("hub.unreachable", f"the new key could not be sealed to the hub ({exc.code})") from None
    finally:
        getattr(transport, "close", lambda: None)()


def _relay_cut(service: WorkspaceService, state: Any) -> None:
    """Step 4. ``unknown_hub`` and every other refusal leave the state ``closing``."""
    if not _relay_linked(service):
        return  # a relay-less workspace has no direct path at a relay to close
    from orch.relay.retry import RelayRefused

    from . import relayhook

    ws = service.workspace_id
    path, route = f"/v2/workspaces/{ws}/cutover", "{ws}/cutover"
    try:
        with relayhook.cutover_session(service, transport=TRANSPORT) as session:
            session.hold()
            held = session.call("GET", path, f"GET /v2/workspaces/{route}")
            if held.get("state") == "cut_over":
                return  # a repeat after a crash between the relay's answer and ours
            session.call(
                "PUT", path, f"PUT /v2/workspaces/{route}",
                body={"v": 2, "hub_id": state.hub_id, "state": "cut_over"}, holder=True,
            )  # fmt: skip
    except RelayRefused as exc:
        if exc.code == "unknown_hub":
            raise HostError(
                "hub.cutover_unknown_hub", "the relay has no account for this hub: ask its operator to register it"
            ) from None
        raise HostError("relay.cut_over", f"the relay refused the cut-over ({exc.code}): will retry") from None


def cutover_delete(service: WorkspaceService) -> bool:
    """C8's seam (``breakglass.CUTOVER_DELETE``): at a leave, delete a *pending* naming at the relay so the hub can
    never confirm it. A row that is already ``cut_over`` is the owner's own closed door and stays closed (I6):
    nothing to delete, answered ``True``. A relay-less workspace has no row."""
    if not _relay_linked(service):
        return True
    from . import relayhook

    ws = service.workspace_id
    path = f"/v2/workspaces/{ws}/cutover"
    with relayhook.cutover_session(service, transport=TRANSPORT) as session:
        session.hold()
        held = session.call("GET", path, "GET /v2/workspaces/{ws}/cutover")
        if held.get("state") != "pending":
            return True
        session.call("DELETE", path, "DELETE /v2/workspaces/{ws}/cutover", holder=True)
    return True


def reopen(service: WorkspaceService) -> dict[str, Any]:
    """M4, the owner's reversal, only after leaving (the caller checked the model: ``hub.left`` is the newest hub
    record, owner, column P): delete the cut-over at the relay (fenced), set ``direct`` to ``open``, and start the relay
    link again. Phones must be paired again (their push registrations were purged after the drain)."""
    h = _hubops()
    state = h._load_state(service)  # noqa: SLF001
    if state is None or state.direct not in ("closing", "closed"):
        raise HostError("relay.not_cut_over", "the direct relay path is not closed")
    if _relay_linked(service):
        from . import relayhook

        ws = service.workspace_id
        with relayhook.cutover_session(service, transport=TRANSPORT) as session:
            session.hold()
            session.call("DELETE", f"/v2/workspaces/{ws}/cutover", "DELETE /v2/workspaces/{ws}/cutover", holder=True)
    state.direct, state.direct_note, state.cut_epoch = "open", None, None
    h._save_state(service, state)  # noqa: SLF001
    return {"direct": "open", "relay_linked": _relay_linked(service)}


# -- at host start: resume ---------------------------------------------------------------------------------------------


def start(service: WorkspaceService) -> None:
    """At the open of a served workspace (``workspace.open``, never fails it): when the cut-over is owed (``closing``,
    an ``open`` a C10 connect left behind, or a dev pre-C10 full join), a background thread resumes it with backoff.
    One thread per workspace; it ends when the state is ``closed``, nothing is owed, or the workspace closes."""
    key = (str(service.state_dir), service.workspace_id)
    try:  # on the worker thread (the open): read the model directly, a submit() here would wait for itself
        model = dict(service.store.state.workspace.hub)
        state = _hubops()._load_state(service)  # noqa: SLF001
        if not needs_run(service, model, state):
            return
    except Exception:  # noqa: BLE001 - never fail the open
        return
    with _THREADS_MU:
        old = _THREADS.get(key)
        if old is not None and old.is_alive():
            return
        t = threading.Thread(
            target=_loop, args=(service,), name=f"orch-cutover-{service.workspace_id[:8]}", daemon=True
        )
        _THREADS[key] = t
    t.start()


def _loop(service: WorkspaceService) -> None:
    wait = _BACKOFF_FIRST_S
    first = True
    while not getattr(service, "closing", False):
        try:
            model = _model(service)
            state = _hubops()._load_state(service)  # noqa: SLF001
            if not needs_run(service, model, state):
                return
            if run(service, model) == "closed":
                return
        except Exception as exc:  # noqa: BLE001
            LOG.warning("cut-over retry of %s: %s", service.workspace_id, type(exc).__name__)
        if first:  # the first attempt right away; later ones back off
            first = False
        if _sleep(service, wait):
            return
        wait = min(wait * 2, _BACKOFF_MAX_S)


def _sleep(service: WorkspaceService, seconds: float) -> bool:
    """Wait ``seconds`` in one-second steps; True when the workspace is closing (stop the loop)."""
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if getattr(service, "closing", False):
            return True
        time.sleep(min(1.0, end - time.monotonic()))
    return bool(getattr(service, "closing", False))
