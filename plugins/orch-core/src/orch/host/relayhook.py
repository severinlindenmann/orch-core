"""R5, the host side of the relay worker (orch-v2-host §11A.4 "The hook", §11A.5 "For R3/R5/R6", §11A.6, §11A.7).

``orch.host`` is import-banned in ``orch.relay``, so everything that needs the host lives here; the worker itself is
:class:`orch.relay.worker.RelayWorker`.

* :func:`make_starter` builds ``service.relay_starter`` (the host sets it in ``Host.serve`` before the open).
  ``WorkspaceService.open`` ends, on the worker thread, with the hook: when ``link.json`` exists and
  :func:`orch.host.relaylink.start_blocker` names nothing (not stopped, a private link, the R6 gate open), the
  starter runs **under** ``service.relay_lock``. It never waits for a job of the workspace's worker thread: it
  builds the objects and hands off to the worker's start thread (the convenience check, then the first link while
  ``linking``, then the loop and the publisher).
* ONE :class:`~orch.relay.state.StateDir` (opened on the resolved state path from the host's trusted config, never
  a ``$TMPDIR`` one: §11A.3 R6-4) and ONE :class:`~orch.relay.session.Session` per workspace, shared by its threads.
* The 8-per-host cap (§11A.7): ``relay.link`` is refused above it (:func:`relaylink.link_reservation`); the starter
  also takes one of :data:`relaylink.MAX_LINKED` per-process slots, held until every thread of that worker ended (a
  stuck worker keeps its slot: fail closed).
* :class:`Seams`: where R3 (``publisher``), R4 (``unpublished``), R6 (``r6``: the ``EpochKeySource`` and the
  ``R6View``) and R6a with the person path (``link_source``: card, genesis pin, owner key) plug in. Unset, each
  refuses (:class:`orch.relay.worker.SeamMissing`) or does nothing (R4); with the R6 gate closed nothing starts.
* :func:`reserve_link`: 11A.5 step 1, the reader reservation of 11A.6, as a worker job:
  ``store.relay_link(link, guard=lambda: relaylink.link_reservation(service))`` (lock order: the store lock, then
  the guard's host-wide lock). Not exposed as a control op yet (judgement call, 11A.4 "As built in R5").
* :func:`status`: ``host.status.relay``. :func:`unlink_job`: ``orch host relay unlink``'s rename.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import HostError

if TYPE_CHECKING:
    from orch.relay.keys import EpochKeySource, R6View
    from orch.relay.worker import LinkSource, Publisher, RelayWorker, Unpublished

    from .workspace import WorkspaceService

__all__ = [
    "Seams",
    "convenience_block",
    "make_starter",
    "relay_state_path",
    "reserve_link",
    "running_slots",
    "start_worker",
    "status",
    "unlink_job",
]

LOG = logging.getLogger("orch.host.relayhook")

_OUTBOX_ENTRY = re.compile(r"\d{20}\.json")


@dataclass
class Seams:
    """The dependency seams (each ``None`` until its group lands):

    * ``r6(service) -> (EpochKeySource, R6View)``: R6 (11A.15.2), the real key source and member view;
    * ``publisher(service, worker) -> Publisher``: R3 (11A.5), the publisher thread and ``publish_first``;
    * ``unpublished(service, worker) -> Unpublished``: R4 (11A.9), one step on the aux thread;
    * ``link_source(service) -> LinkSource``: R6a and the genesis-pin person path (11A.5 steps 2 and 3);
    * ``conn(origin, link) -> Transport``: the transport factory (tests: a loopback relay); default
      :class:`orch.relay.http.RelayConn` with ``link.json``'s ``ca_file`` and ``proxy``."""

    r6: Callable[[WorkspaceService], tuple[EpochKeySource, R6View]] | None = None
    publisher: Callable[[WorkspaceService, RelayWorker], Publisher] | None = None
    unpublished: Callable[[WorkspaceService, RelayWorker], Unpublished] | None = None
    link_source: Callable[[WorkspaceService], LinkSource] | None = None
    conn: Callable[[str, Mapping[str, Any]], Any] | None = None


#: the seams the host uses (R3, R4 and R6 set theirs here when they land)
SEAMS = Seams()


# -- the per-process slots of the 8-per-host cap ----------------------------------------------------------------------

_SLOTS_LOCK = threading.Lock()
_SLOTS: dict[str, set[str]] = {}


def running_slots(state_dir: Path) -> set[str]:
    with _SLOTS_LOCK:
        return set(_SLOTS.get(os.path.realpath(state_dir), set()))


def _take_slot(state_dir: Path, workspace_id: str) -> Callable[[], None]:
    from .relaylink import MAX_LINKED

    key = os.path.realpath(state_dir)
    with _SLOTS_LOCK:
        held = _SLOTS.setdefault(key, set())
        if workspace_id in held:
            raise HostError("lock.busy", "a relay worker of this workspace still holds its slot")
        if len(held) >= MAX_LINKED:
            raise HostError("host.limit", f"this host already runs {len(held)} relay workers (at most {MAX_LINKED})")
        held.add(workspace_id)
    released = threading.Event()

    def release() -> None:
        if released.is_set():
            return
        released.set()
        with _SLOTS_LOCK:
            _SLOTS.get(key, set()).discard(workspace_id)

    return release


# -- building and starting the worker ---------------------------------------------------------------------------------


def relay_state_path(service: WorkspaceService) -> Path:
    """The relay state directory, from the host's trusted state path resolved once (§11A.3, R6-4): the components
    below it are appended unresolved, so a symlinked ``hosts/<ws>/relay`` is still refused by ``StateDir``."""
    return Path(os.path.realpath(service.state_dir)) / "hosts" / service.workspace_id / "relay"


def make_starter(backend: Any, seams: Seams | None = None) -> Callable[[WorkspaceService], None]:
    """``service.relay_starter``: :func:`start_worker` with the socket backend (``RelayEntry`` runs the same ops)."""

    def starter(service: WorkspaceService) -> None:
        start_worker(service, backend, seams if seams is not None else SEAMS)

    return starter


def _link_record(state: Any, workspace_id: str) -> dict[str, Any]:
    from orch.relay import wire

    link = state.read_json("link.json")
    if not isinstance(link, dict) or link.get("workspace_id", workspace_id) != workspace_id:
        raise HostError("relay.link_mismatch", "link.json is not this workspace's link record")
    origin = link.get("origin")
    try:
        wire.check_origin(origin)
    except (wire.WireError, TypeError):
        raise HostError("relay.link_mismatch", "link.json names no valid relay origin") from None
    if link.get("state") not in ("linking", "linked"):
        raise HostError("relay.link_mismatch", "link.json is in no known state")
    return link


def _link_state(state: Any) -> str | None:
    link = state.read_json("link.json")
    return link.get("state") if isinstance(link, dict) else None


def convenience_block(service: WorkspaceService) -> str | None:
    """§11A.6 at worker start and after a foreign append: ``convenience_events_present`` when the merged history (or
    the durable mark, or the flag) would refuse ``relay.link``; ``None`` otherwise. Under the store lock (worker
    thread)."""
    from .convenience import refuse_reader_op

    store = service.store
    if store is None:
        return "workspace_unavailable"
    try:
        with store.locked():
            refuse_reader_op(service, "relay.link", history=True)
    except HostError as exc:
        if exc.code == "sign.convenience_readers":
            return "convenience_events_present"
        raise
    return None


def start_worker(service: WorkspaceService, backend: Any, seams: Seams) -> None:
    """Build and start the worker; sets ``service.relay``. Runs under ``service.relay_lock`` (the caller's) and never
    waits for the workspace's worker thread. Any failure leaves nothing open (the slot, the state directory and the
    rid store are released) and raises."""
    from orch.custody import FileBackend
    from orch.relay import http
    from orch.relay import worker as relay_worker
    from orch.relay.bridge import Bridge, BridgeLoop
    from orch.relay.directory import Directory
    from orch.relay.keys import BridgeKeys
    from orch.relay.link import first_link
    from orch.relay.retry import Retrier
    from orch.relay.rids import RidStore
    from orch.relay.session import Session
    from orch.relay.sign import LinkContext, RelaySigner
    from orch.relay.state import FileHolderStore, FileSignLedger, StateDir
    from orch.store import BackendSigner

    from .relayentry import RelayEntry

    if not relay_worker.R6_READY:  # the host checked it (start_blocker); checked again before anything is opened
        raise relay_worker.SeamMissing(relay_worker.R6_GATE_REASON)
    if seams.r6 is None:
        raise relay_worker.SeamMissing("R6 (EpochKeySource, R6View) is not built")
    ws = service.workspace_id
    release = _take_slot(service.state_dir, ws)
    state: Any = None
    rids: Any = None
    try:
        state = StateDir(relay_state_path(service))
        link = _link_record(state, ws)
        origin = link["origin"]
        keys_dir = Path(service.state_dir) / "hosts" / ws / "keys"
        backend_keys = FileBackend(keys_dir) if keys_dir.is_dir() else None
        if backend_keys is None or not backend_keys.exists("wsk"):
            raise HostError("trust.wsk_missing", "the workspace signing key is not in the host's custody")
        wsk = BackendSigner(backend_keys, "wsk")
        epochs, view = seams.r6(service)
        bkeys = BridgeKeys(epochs, bytes.fromhex(ws))
        card_seq = link.get("card_seq") if link.get("state") == "linked" else 0
        signer = RelaySigner(
            wsk, LinkContext(ws, origin), epochs=bkeys, ledger=FileSignLedger(state),
            last_card_seq=card_seq if type(card_seq) is int and card_seq >= 0 else 0,
        )  # fmt: skip
        stop = threading.Event()

        def now_ms() -> int:
            return int(service.clock() * 1000)

        alarms = relay_worker.Alarms(now_ms=now_ms, persist=lambda snap: state.write_json("alarms.json", snap))

        def conn() -> Any:
            if seams.conn is not None:
                return seams.conn(origin, link)
            return http.RelayConn(origin, ca_file=link.get("ca_file"), proxy=link.get("proxy"))

        session = Session(conn(), signer, holder=FileHolderStore(state), alarm=alarms, stop=stop)

        holder: dict[str, RelayWorker] = {}

        def run_first_link() -> None:
            source = seams.link_source(service) if seams.link_source is not None else relay_worker.NoLinkSource()
            directory = Directory(session, owner_pk_pub=source.owner_pk_pub(), wsk_pub=wsk.public_key)
            retrier = Retrier(sleep=time.sleep, alarm=alarms, stop=stop)
            first_link(_LinkSteps(holder["w"], service, state, source, link, session, directory), session, directory,
                       retrier, store=state)  # fmt: skip

        worker = relay_worker.RelayWorker(
            workspace_id=ws, state=state, session=session, submit=service.submit, alarms=alarms, stop_event=stop,
            link_state=lambda: _link_state(state), precheck=lambda: convenience_block(service),
            first_link=run_first_link, on_ended=release,
        )  # fmt: skip
        holder["w"] = worker
        if seams.publisher is not None:
            worker.publisher = seams.publisher(service, worker)
        if seams.unpublished is not None:
            worker.unpublished = seams.unpublished(service, worker)
        rids = RidStore(state)

        def no_post(device: str, rid: str, envelope: bytes) -> str:
            raise RuntimeError("the loop installs the poster")  # replaced in the poll thread before any POST

        bridge = Bridge(
            workspace_id=ws, rids=rids, keys=bkeys, signer=signer, r6=view, port=RelayEntry(service, backend),
            job=worker.job, post=no_post, now_ms=now_ms, alarm=alarms,
        )  # fmt: skip
        worker.attach(BridgeLoop(bridge, session, conn=conn, workspace_id=ws), rids)
        worker.start()
    except BaseException:
        if rids is not None:
            rids.close()
        if state is not None:
            state.close()
        release()
        raise
    service.relay = worker


class _LinkSteps:
    """R1's :class:`orch.relay.link.LinkSteps` over the host (11A.5 first link): ``link_state`` from the worker's
    state directory, the card and the pin from the link source (R6a, person path), the first publication from the
    publisher (R3), and ``mark_linked`` as ``store.relay_mark_linked(expect)`` in a worker job, where ``expect`` is the
    exact record the worker was started for (a link changed in between is ``relay.link_mismatch``)."""

    def __init__(
        self, worker: RelayWorker, service: WorkspaceService, state: Any, source: LinkSource,
        expect: Mapping[str, Any], session: Any, directory: Any,
    ) -> None:  # fmt: skip
        self.worker, self.service, self.state, self.source = worker, service, state, source
        self.expect, self.session, self.directory = dict(expect), session, directory

    def link_state(self) -> str:
        return str(_link_state(self.state))

    def card(self) -> Mapping[str, Any]:
        return self.source.card()

    def genesis(self) -> tuple[Mapping[str, Any], Mapping[str, Any], str]:
        return self.source.genesis()

    def publish(self, session: Any, directory: Any) -> None:
        self.worker.publisher.publish_first(session, directory)

    def mark_linked(self) -> None:
        service, expect = self.service, self.expect

        def mark() -> None:
            if service.store is None:
                raise HostError("workspace.unavailable", "the workspace is not open")
            service.store.relay_mark_linked(expect)

        self.worker.job(mark)


# -- the reader reservation (11A.5 step 1, 11A.6) ---------------------------------------------------------------------


def reserve_link(
    service: WorkspaceService,
    *,
    origin: str,
    max_publication_bytes: int,
    ca_file: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    """11A.5 step 1, on the worker thread: ``link.json`` (state ``linking``) through ``store.relay_link`` with
    :func:`orch.host.relaylink.link_reservation` as the guard: the store lock, then the host-wide lock, then the
    ``sign.convenience_readers`` and ``host.limit`` refusals, held across the write; ``relay.link_exists`` for an
    existing link. Returns the record written. Nothing is sent: the network steps run in the worker."""
    from orch.relay import wire

    from .relaylink import link_reservation

    try:
        wire.check_origin(origin)
    except (wire.WireError, TypeError):
        raise HostError("invalid.input", "the relay origin is not a valid origin (scheme://host[:port])") from None
    if type(max_publication_bytes) is not int or not 1 << 20 <= max_publication_bytes <= 1 << 30:
        raise HostError("invalid.input", "max_publication_bytes is an integer between 1 MiB and 1 GiB")
    store = service.store
    if store is None:
        raise HostError("workspace.unavailable", "the workspace is not open")
    link: dict[str, Any] = {
        "v": 1, "workspace_id": service.workspace_id, "origin": origin, "max_publication_bytes": max_publication_bytes,
        "linked_at": int(service.clock() * 1000), "card_seq": 1,
    }  # fmt: skip
    if ca_file is not None:
        link["ca_file"] = ca_file
    if proxy is not None:
        link["proxy"] = proxy
    store.relay_link(link, guard=lambda: link_reservation(service))
    return {**link, "state": "linking"}


# -- unlink (11A.6: the reservation is permanent) ---------------------------------------------------------------------


def unlink_job(service: WorkspaceService) -> str:
    """On the worker thread, holding ``service.relay_cond`` (no worker can start in between) and the store lock (the
    outbox turns off with the rename): ``link.json`` becomes ``unlinked-<linked_at>.json`` (``os.link`` then
    ``unlink``: never over an existing file). ``relay.not_linked`` without a link; ``lock.busy`` while a worker runs."""
    from orch.store.fsio import fsync_dir

    from .custody import read_json
    from .relaylink import link_path, relay_dir

    store = service.store
    if store is None:
        raise HostError("workspace.unavailable", "the workspace is not open")
    with service.relay_cond:
        if service.relay is not None:
            raise HostError("lock.busy", "a relay worker runs; stop it first")
        with store.locked():
            path = link_path(service.state_dir, service.workspace_id)
            if not os.path.lexists(path):
                raise HostError("relay.not_linked", "the workspace has no relay link")
            link = read_json(path, service.host_uid)
            at = link.get("linked_at") if isinstance(link, dict) else None
            stamp = at if type(at) is int and at >= 0 else int(service.clock() * 1000)
            d = relay_dir(service.state_dir, service.workspace_id)
            for i in range(1000):
                name = f"unlinked-{stamp}.json" if i == 0 else f"unlinked-{stamp}-{i}.json"
                try:
                    os.link(path, d / name, follow_symlinks=False)
                    break
                except FileExistsError:
                    continue
            else:
                raise HostError("internal", "no free unlinked-<linked_at> name")
            os.unlink(path)
            fsync_dir(d)
    return name


# -- host.status.relay ------------------------------------------------------------------------------------------------


def _link_summary(service: WorkspaceService) -> str:
    from .custody import read_json
    from .relaylink import link_path

    path = link_path(service.state_dir, service.workspace_id)
    if not os.path.lexists(path):
        return "none"
    try:
        link = read_json(path, service.host_uid)
    except (HostError, OSError):
        return "unreadable"
    state = link.get("state") if isinstance(link, dict) else None
    return state if state in ("linking", "linked") else "unreadable"


def _outbox_pending(service: WorkspaceService) -> int | None:
    d = Path(service.state_dir) / "hosts" / service.workspace_id / "outbox"
    try:
        return sum(1 for name in os.listdir(d) if _OUTBOX_ENTRY.fullmatch(name))
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def _stored_alarms(service: WorkspaceService) -> list[dict[str, Any]]:
    from .custody import read_json
    from .relaylink import relay_dir

    try:
        doc = read_json(relay_dir(service.state_dir, service.workspace_id) / "alarms.json", service.host_uid)
    except (HostError, OSError):
        return [{"code": "relay_alarms_unreadable", "count": 1}]
    rows = doc.get("alarms") if isinstance(doc, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and type(row.get("code")) is str:
            out.append({k: row[k] for k in ("code", "count", "first_ms", "last_ms", "detail") if k in row})
    return out[:64]


def status(service: WorkspaceService) -> dict[str, Any] | None:
    """``host.status.relay`` (classes P and G; ``None``, so no field, for a workspace that never had a relay
    directory and runs no worker): the link state, the worker (``running``, ``stopping``, ``stuck`` or
    ``off``) and whether a thread is alive, why none runs, the stop marker, the R6 gate, the outbox entries queued for
    publication and the open alarms. Never a secret, a token, a link or relay bytes."""
    from orch.relay import worker as relay_worker

    from .relaylink import is_stopped, relay_dir

    with service.relay_cond:
        worker = service.relay
        stuck = service.relay_stuck_alive()
        stopping = service.relay_stopping_count()
        reason = service.relay_reason
    if worker is None and not stuck and not stopping:
        if not os.path.lexists(relay_dir(service.state_dir, service.workspace_id)):
            return None
    out: dict[str, Any] = {
        "link": _link_summary(service),
        "stopped": is_stopped(service.state_dir, service.workspace_id, service.host_uid),
        "gate": {"r6_ready": relay_worker.R6_READY is True,
                 "reason": None if relay_worker.R6_READY is True else relay_worker.R6_GATE_REASON},
        "outbox_pending": _outbox_pending(service),
    }  # fmt: skip
    if worker is not None:
        st = worker.status() if callable(getattr(worker, "status", None)) else {}
        if st.get("stopping"):
            state = "stopping"
        elif st.get("serving", True):
            state = "running"
        elif st.get("alive", True) and not st.get("error"):
            state = "starting"
        else:
            state = "failed"  # the loop stopped itself (fenced, session lost) or the start failed: stop, then start
        out.update(worker=state, alive=bool(st.get("alive", True)))
        out["reason"] = st.get("error")
        out["alarms"] = list(st.get("alarms") or [])
        if st.get("gen") is not None:
            out["gen"] = st["gen"]
        return out
    if stuck:
        out.update(worker="stuck", alive=True, reason="relay_thread_stuck")
    elif stopping:
        out.update(worker="stopping", alive=True, reason=reason)
    else:
        out.update(worker="off", alive=False, reason=reason)
    with contextlib.suppress(Exception):
        out["alarms"] = _stored_alarms(service) if out["link"] != "none" else []
    out.setdefault("alarms", [])
    return out
