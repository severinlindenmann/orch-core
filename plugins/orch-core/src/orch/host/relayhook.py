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
import dataclasses
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import directpath
from .devmode import gated as devmode_gated
from .errors import HostError

if TYPE_CHECKING:
    from orch.relay.keys import EpochKeySource, R6View
    from orch.relay.worker import LinkSource, Publisher, RelayWorker, Unpublished

    from .workspace import WorkspaceService

__all__ = [
    "SEAMS",
    "Seams",
    "convenience_block",
    "install_seams",
    "link_step1",
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


@dataclass(frozen=True)
class Seams:
    """The dependency seams (each ``None`` until its group lands). With the gate open, a worker starts only when
    ALL are set (:meth:`missing`) and each object they build conforms to its protocol (``_conform``: the protocol and
    every member callable). None of these factories may wait for a job of the workspace's worker thread: the starter
    calls them under ``relay_lock`` (re-entrant), on the open path on the worker thread itself.

    * ``r6(service) -> (EpochKeySource, R6View)``: R6 (11A.15.2), the real key source and member view;
    * ``publisher(service, worker) -> Publisher``: R3 (11A.5), the publisher thread and ``publish_first``;
    * ``unpublished(service, worker) -> Unpublished``: R4 (11A.9), one step on the aux thread;
    * ``link_source(service) -> LinkSource``: R6a and the genesis-pin person path (11A.5 steps 2 and 3);
    * ``push(service, worker, keys, signer, view)``: H13a, the push thread (a worker companion, optional).

    The transport is not a seam here: production always uses :class:`orch.relay.http.RelayConn` with ``link.json``'s
    ``ca_file`` and ``proxy``; only a test passes ``transport=`` to :func:`make_starter` (an AST rule refuses it in
    ``src/``)."""

    r6: Callable[[WorkspaceService], tuple[EpochKeySource, R6View]] | None = None
    publisher: Callable[[WorkspaceService, RelayWorker], Publisher] | None = None
    unpublished: Callable[[WorkspaceService, RelayWorker], Unpublished] | None = None
    link_source: Callable[[WorkspaceService], LinkSource] | None = None
    #: H13a (11A.16.4): ``push(service, worker, keys, signer, view) -> companion`` (the push thread). Unset, no push
    #: thread runs (the refusing default): not part of :meth:`missing`, a worker is complete without it
    push: Callable[..., Any] | None = None

    def missing(self) -> list[str]:
        return [n for n in ("r6", "publisher", "unpublished", "link_source") if getattr(self, n) is None]


#: the seams the host uses (frozen: R3, R4 and R6 replace it with ``dataclasses.replace`` when they land)
SEAMS = Seams()


def install_seams() -> Seams:
    """Land the production seams at host start (B1; ``Host.serve`` calls it before the first starter is built):
    ``r6`` = :func:`relayr6.r6`, ``link_source`` = :func:`relayr6.link_source`, ``publisher`` =
    :func:`relayr3.publisher`, ``unpublished`` = :func:`relayr4.unpublished`, ``push`` =
    :func:`relaypush.push` (H13a). A seam already set (a test's) is
    kept, and calling it again changes nothing. The R6 gate (``relay_worker.R6_READY``) is a separate switch: with it
    closed no worker starts whatever the seams are. Also sets the R6b membership seam,
    :data:`orch.host.relaypair.MEMBERSHIP`, when it is unset."""
    global SEAMS
    from . import relaypush, relayr3, relayr4, relayr6

    SEAMS = dataclasses.replace(
        SEAMS,
        r6=SEAMS.r6 or relayr6.r6,
        publisher=SEAMS.publisher or relayr3.publisher,
        unpublished=SEAMS.unpublished or relayr4.unpublished,
        link_source=SEAMS.link_source or relayr6.link_source,
        push=SEAMS.push or relaypush.push,
    )
    from . import relaypair, relayr6b

    if relaypair.MEMBERSHIP is None:
        relaypair.MEMBERSHIP = relayr6b.membership
    return SEAMS


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


def make_starter(
    backend: Any, seams: Seams | None = None, *, transport: Callable[[str, Mapping[str, Any]], Any] | None = None
) -> Callable[[WorkspaceService], None]:
    """``service.relay_starter``: :func:`start_worker` with the socket backend (``RelayEntry`` runs the same ops).
    ``transport(origin, link)`` is for tests only (a loopback relay)."""

    def starter(service: WorkspaceService) -> None:
        start_worker(service, backend, seams if seams is not None else SEAMS, transport=transport)

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
    from orch.store.readerrecs import read_relay_level

    from .convenience import refuse_reader_op

    store = service.store
    if store is None:
        return "workspace_unavailable"
    try:
        with store.locked():
            # the relay's last verified reader level (H12d-3 9.8, stored at link and every R1 login): a relay of
            # level 1 passes with convenience history (§11A.17.4, the dev sandbox's worker restart); None counts as 0
            level = read_relay_level(service.state_dir, service.workspace_id, service.host_uid)
            refuse_reader_op(service, "relay.link", history=True, relay_level=level)
    except HostError as exc:
        if exc.code == "sign.convenience_readers":
            return "convenience_events_present"
        raise
    return None


def _conform(obj: Any, kind: type, label: str) -> Any:
    """A seam object conforms: the protocol (``worker.check_seam``) and every required member callable (review
    Low 2: ``runtime_checkable`` checks presence only)."""
    from orch.relay import worker as relay_worker

    relay_worker.check_seam(obj, kind, label)
    if not all(callable(getattr(obj, n, None)) for n in relay_worker.SEAM_METHODS[kind]):
        raise relay_worker.SeamMissing(f"the {label} seam has a member that is not callable")
    return obj


def _check_r6(epochs: Any, view: Any) -> None:
    from orch.relay.worker import SeamMissing

    for obj, names in ((epochs, ("current_epoch", "wk", "seal_state", "refusal_key")), (view, ("member_list",))):
        if not all(callable(getattr(obj, n, None)) for n in names):
            raise SeamMissing("the r6 seam does not provide an EpochKeySource and an R6View")


def start_worker(
    service: WorkspaceService,
    backend: Any,
    seams: Seams,
    *,
    transport: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> None:
    """Build and start the worker. Runs under ``service.relay_lock`` (the caller's) and never waits for the
    workspace's worker thread. ``service.relay`` is set to the worker BEFORE its start thread exists, so a foreign
    append that the service audits while the start thread runs always sees it (review R1); a failure clears it again.
    Any failure leaves nothing open (the slot, the state directory and the rid store are released) and raises."""
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

    from . import relaylink, relaypair
    from .relayentry import RelayEntry

    if directpath.closed(service.state_dir, service.workspace_id, service.host_uid):
        # C10 M3: a cut-over (or closing) workspace dials the direct relay with no thread at all, whoever asked
        raise HostError("relay.cut_over", "the direct relay path of this workspace is closed (it is served by a hub)")
    if not relay_worker.r6_ready():  # the host checked it (start_blocker); checked again before anything is opened
        raise relay_worker.SeamMissing(relay_worker.R6_GATE_REASON)
    missing = seams.missing()
    if missing:
        raise relay_worker.SeamMissing(f"seams not set: {', '.join(missing)}")
    assert seams.r6 is not None and seams.publisher is not None and seams.unpublished is not None
    assert seams.link_source is not None
    ws = service.workspace_id
    release = _take_slot(service.state_dir, ws)
    state: Any = None
    rids: Any = None
    worker: Any = None
    pusher: Any = None
    try:
        state = StateDir(relay_state_path(service))
        link = _link_record(state, ws)
        origin = link["origin"]
        keys_dir = Path(service.state_dir) / "hosts" / ws / "keys"
        backend_keys = FileBackend(keys_dir) if keys_dir.is_dir() else None
        if backend_keys is None or not backend_keys.exists("wsk"):
            raise HostError("workspace.unavailable", "the workspace signing key is not in the host's custody")
        wsk = BackendSigner(backend_keys, "wsk")
        epochs, view = seams.r6(service)
        _check_r6(epochs, view)
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
            if transport is not None:
                return transport(origin, link)
            return http.RelayConn(
                origin, ca_file=link.get("ca_file"), proxy=link.get("proxy"),
                allowed=directpath.gate(service.state_dir, ws, service.host_uid),
            )  # fmt: skip

        def observe(level: int) -> None:
            # H12d-3 9.8: every login re-reads the relay's reader level; stored only while link.json still names THIS
            # origin, exactly as link.json stores it (#555 F2), under relay-link.lock (a leaf lock: no store lock,
            # no relay_cond; it runs on a relay thread under the session's login lock)
            relaylink.observe_relay_level(
                service.state_dir, ws, service.host_uid, level, alarms, origin=origin,
                gated=lambda: devmode_gated(service),
            )  # fmt: skip

        session = Session(conn(), signer, holder=FileHolderStore(state), alarm=alarms, stop=stop, relay_info=observe)

        holder: dict[str, RelayWorker] = {}

        def run_first_link() -> None:
            source = _conform(seams.link_source(service), relay_worker.LinkSource, "link_source")
            directory = Directory(session, owner_pk_pub=source.owner_pk_pub(), wsk_pub=wsk.public_key)
            retrier = Retrier(sleep=time.sleep, alarm=alarms, stop=stop)
            first_link(_LinkSteps(holder["w"], service, state, source, link, session, directory), session, directory,
                       retrier, store=state)  # fmt: skip

        worker = relay_worker.RelayWorker(
            workspace_id=ws, state=state, session=session, submit=service.submit, alarms=alarms, stop_event=stop,
            link_state=lambda: _link_state(state), precheck=lambda: convenience_block(service),
            first_link=run_first_link, on_ended=release, conn=conn,
        )  # fmt: skip
        holder["w"] = worker
        worker.publisher = _conform(seams.publisher(service, worker), relay_worker.Publisher, "publisher")
        worker.unpublished = _conform(seams.unpublished(service, worker), relay_worker.Unpublished, "unpublished")
        rids = RidStore(state)
        if seams.push is not None:  # H13a: the push thread, a companion started after the loop and the publisher
            pusher = seams.push(service, worker, bkeys, signer, view)
            worker.companions.append(pusher)

        def no_post(device: str, rid: str, envelope: bytes) -> str:
            raise RuntimeError("the loop installs the poster")  # replaced in the poll thread before any POST

        from .devmode import gated

        bridge = Bridge(
            workspace_id=ws, rids=rids, keys=bkeys, signer=signer, r6=view, port=RelayEntry(service, backend),
            job=worker.job, post=no_post, now_ms=now_ms, alarm=alarms, convenience_gate=lambda: gated(service),
        )  # fmt: skip
        # R6c: the pair thread (11A.15.2 "Threads"), started and stopped with the bridge loop; its own connection
        pairing = relaypair.start_thread(service, session, conn, worker=worker)
        worker.attach(BridgeLoop(bridge, session, conn=conn, workspace_id=ws, pairing=pairing), rids)
        service.relay = worker  # before the start thread exists (review R1); the caller holds relay_lock
        worker.start()
    except BaseException:
        if worker is not None and service.relay is worker:
            service.relay = None
        if pusher is not None:
            pusher.close()
        if rids is not None:
            rids.close()
        if state is not None:
            state.close()
        release()
        raise


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
        # the card was accepted by now (R1: PUT card precedes login, hold, genesis and this step): R6a's source moves
        # its pending card to accepted (11A.15.5; a source without the hook, a test fake, has nothing to move)
        accepted = getattr(self.source, "card_accepted", None)
        if callable(accepted):
            accepted()
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
    relay_level: int | None = None,
) -> dict[str, Any]:
    """11A.5 step 1, on the worker thread: ``link.json`` (state ``linking``) through ``store.relay_link`` with
    :func:`orch.host.relaylink.link_reservation` as the guard: the store lock, then the host-wide lock, then the
    ``sign.convenience_readers`` refusal against ``relay_level`` (the relay's reader level from ``GET
    /v2/relay-info``, read by :func:`link_step1` before this job; ``None``: not read, counts as 0) and ``host.limit``,
    held across the write; ``relay.link_exists`` for an existing link. Returns the record written. Nothing is sent
    from here."""
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
    store.relay_link(link, guard=lambda: link_reservation(service, relay_level=relay_level, origin=origin))
    return {**link, "state": "linking"}


def link_step1(
    service: WorkspaceService,
    *,
    origin: str,
    max_publication_bytes: int,
    ca_file: str | None = None,
    proxy: str | None = None,
    transport: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """11A.5 step 1 with H12d-3 (§0.8.6 item 9.8): ``info = fetch_relay_info(conn)`` on the CALLER's thread (never the
    workspace's worker thread: it is network I/O), then :func:`reserve_link` as a worker job with
    ``relay_level=info.reader``. ``transport`` is for tests only. Not exposed as a control op yet (11A.4 "As built
    in R5")."""
    from orch.relay import http
    from orch.relay.session import fetch_relay_info

    link = {"origin": origin, "ca_file": ca_file, "proxy": proxy}
    conn = transport(origin, link) if transport is not None else http.RelayConn(origin, ca_file=ca_file, proxy=proxy)
    info = fetch_relay_info(conn)
    return service.submit(
        lambda: reserve_link(
            service, origin=origin, max_publication_bytes=max_publication_bytes, ca_file=ca_file, proxy=proxy,
            relay_level=info.reader,
        )
    ).result(timeout=60)  # fmt: skip


# -- unlink (11A.6: the reservation is permanent) ---------------------------------------------------------------------


def unlink_job(service: WorkspaceService) -> str:
    """On the worker thread, holding ``service.relay_cond`` (no worker can start in between) and the store lock (the
    outbox turns off with the rename): ``link.json`` becomes ``unlinked-<linked_at>.json`` (``os.link`` then
    ``unlink``: never over an existing file), then the unlink intent is cleared and ``relay_reason`` is
    ``not_linked``. Crash-safe (round 4, orch-52): with an intent on disk the stop marker is persisted first (a crash
    between the intent and the marker must not let a later relink start without ``relay start``); the archive is made
    durable (directory fsync) before ``link.json`` is removed; an archive that is already the same file as
    ``link.json`` (a crash between the two) is reused, never duplicated; an intent whose link is already renamed is only
    cleared (the answer is ``"done"``). ``relay.not_linked`` without a link or an intent; ``lock.busy`` while a worker
    runs."""
    from orch.store import fsio

    from .custody import read_json
    from .relaylink import (
        clear_unlink_intent,
        link_path,
        mark_stopped,
        note_alarm,
        read_unlink_intent,
        relay_dir,
        unlink_pending,
    )  # fmt: skip

    store = service.store
    if store is None:
        raise HostError("workspace.unavailable", "the workspace is not open")
    with service.relay_cond:
        if service.relay is not None:
            raise HostError("lock.busy", "a relay worker runs; stop it first")
        pending = unlink_pending(service.state_dir, service.workspace_id)
        with store.locked():
            path = link_path(service.state_dir, service.workspace_id)
            if pending:
                intent = read_unlink_intent(service.state_dir, service.workspace_id, service.host_uid)  # bad: raises
                if os.path.lexists(path):
                    current = read_json(path, service.host_uid)
                    cur_at = current.get("linked_at") if isinstance(current, dict) else None
                    if intent["linked_at"] != cur_at:  # round 5 L1: an intent for another link: never acted on
                        note_alarm(
                            service.state_dir, service.workspace_id, service.host_uid, "relay_unlink_intent_stale",
                            "an unlink intent for another link was discarded", clock=service.clock,
                        )  # fmt: skip
                        LOG.error("relay unlink intent of %s is stale (another link): discarded", service.workspace_id)
                        clear_unlink_intent(service.state_dir, service.workspace_id, service.host_uid)
                        service.relay_reason = "unlink_intent_stale"
                        return "stale"
                # before either completion branch (P2)
                mark_stopped(service.state_dir, service.workspace_id, service.host_uid, clock=service.clock)
            if not os.path.lexists(path):
                if unlink_pending(service.state_dir, service.workspace_id):  # renamed before a crash: finish
                    clear_unlink_intent(service.state_dir, service.workspace_id, service.host_uid)
                    service.relay_reason = "not_linked"
                    return "done"
                raise HostError("relay.not_linked", "the workspace has no relay link")
            link = read_json(path, service.host_uid)
            at = link.get("linked_at") if isinstance(link, dict) else None
            stamp = at if type(at) is int and at >= 0 else int(service.clock() * 1000)
            d = relay_dir(service.state_dir, service.workspace_id)
            here = os.lstat(path)
            for i in range(1000):
                name = f"unlinked-{stamp}.json" if i == 0 else f"unlinked-{stamp}-{i}.json"
                try:
                    os.link(path, d / name, follow_symlinks=False)
                    break
                except FileExistsError:
                    there = os.lstat(d / name)
                    if (there.st_dev, there.st_ino) == (here.st_dev, here.st_ino):
                        break  # this very file, archived before a crash: reuse it
                    continue
            else:
                raise HostError("internal", "no free unlinked-<linked_at> name")
            fsio.fsync_dir(d)  # the archive is durable before the original goes (P1)
            os.unlink(path)
            fsio.fsync_dir(d)
            clear_unlink_intent(service.state_dir, service.workspace_id, service.host_uid)
            service.relay_reason = "not_linked"
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
    from orch.relay import worker as relay_worker

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
            kept = {k: row[k] for k in ("code", "count", "first_ms", "last_ms", "detail") if k in row}
            for k in ("code", "detail"):
                if type(kept.get(k)) is str:
                    kept[k] = relay_worker.printable(kept[k][: relay_worker.ALARM_DETAIL_MAX])
            out.append(kept)
    return out[:64]


def status(service: WorkspaceService) -> dict[str, Any] | None:
    """``host.status.relay`` with R6c's pairing block (``pairing``: offers, alarms, ``needs`` items, the drop count;
    no secret) when a pairing engine is open in this process."""
    from . import relaypair

    out = _status(service)
    pairing = relaypair.status(service)
    if out is not None and pairing is not None:
        out["pairing"] = pairing
    if out is not None and out.get("link") == "linking":
        extra = _genesis_pin_status(service)
        out["genesis_pin"] = extra["genesis_pin"]
        if "note" in extra:  # beside R4's held-evidence notes, never over them
            out["attention"] = [*out.get("attention", []), extra["note"]]
    return out


def _genesis_pin_status(service: WorkspaceService) -> dict[str, Any]:
    """The first link waits for the owner's signature of the genesis pin (11A.5 step 3, ``relay.genesis_pin``):
    ``genesis_pin: "needed"`` and an ``attention`` entry that names the command (the dashboard's click signer is a
    follow-up), or ``"signed"``. Never a secret."""
    from . import relaygenesis

    try:
        present = relaygenesis.pin_path(service).is_file()
    except OSError:
        present = False
    if present:
        return {"genesis_pin": "signed"}
    note = {
        "what": f"{service.entry.prefix}: the first relay link needs your signature of the workspace's genesis pin",
        "fix": "run orch host relay genesis-pin --workspace W at your terminal (your passphrase signs it), then "
        "orch host relay start --workspace W",
        "actionable": True,
    }
    return {"genesis_pin": "needed", "note": note}


def _status(service: WorkspaceService) -> dict[str, Any] | None:
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
        "gate": {"r6_ready": relay_worker.r6_ready(),
                 "reason": None if relay_worker.r6_ready() else relay_worker.R6_GATE_REASON},
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
        for companion in getattr(worker, "companions", ()):  # H13a: the push thread's state and intent counts
            push_status = getattr(companion, "push_status", None)
            if callable(push_status):
                out["push"] = push_status()
        return _with_held(service, out)
    if stuck:
        out.update(worker="stuck", alive=True, reason="relay_thread_stuck")
    elif stopping:
        out.update(worker="stopping", alive=True, reason=reason)
    else:
        out.update(worker="off", alive=False, reason=reason)
    with contextlib.suppress(Exception):
        out["alarms"] = _stored_alarms(service) if out["link"] != "none" else []
    out.setdefault("alarms", [])
    return _with_held(service, out)


def _with_held(service: WorkspaceService, out: dict[str, Any]) -> dict[str, Any]:
    """R4 (11A.9): the held unpublished evidence for the owner, with what to do (``attention``); SEAM #572."""
    from .relayr4 import held_attention, ledger_at

    ledger = ledger_at(service.state_dir, service.workspace_id, service.host_uid)
    pairs = ledger["held"]
    out["held_evidence"] = pairs
    out["ledger"] = {k: v for k, v in ledger.items() if k != "held"}
    note = held_attention(str(service.entry.prefix), pairs, ledger)
    out["attention"] = [note] if note is not None else []
    return out
