"""One registered workspace inside the host (D-P2-1): its worker thread, its store, its state and its socket gate.

The worker thread is the single owner of the mutable :class:`~orch.store.Store`; everything that touches the store
runs on it (:meth:`WorkspaceService.call`). The state is one of

* ``ok``;
* ``degraded``: reads only (an unacknowledged invalid event, a frozen workspace); a write answers
  ``workspace.degraded``;
* ``unavailable``: missing root, a broken or mismatched genesis, unsafe modes; every op answers
  ``workspace.unavailable`` with the cause.

:class:`WorkspaceGate` is the request gate on the workspace socket. It applies §0.6 in order:

1. and 2. (framing, envelope) are the transport's;
3. the op: ``unknown_command`` for an op the backend does not serve, then ``op.unsupported`` when the client's
   ``hello.ops`` names another contract version;
4. drain (``host.restarting``);
5. ``grant.secret_in_args``, then an unverified grant on a write that column G would allow (``grant.required`` or
   ``grant.expired``);
6. the column (``op.not_allowed``, ``human_only``);
7. ``session.admit``: the agent rate (``rate.limited``) and the in-flight quota (``quota.host``).

Steps 3 to 6 run synchronously on the event loop, before ``admit``. Step 5's grant check and step 6 use the
workspace's :class:`~orch.host.auth.AuthView` snapshot (no blocking store access on the loop). When that snapshot is
stale (the workspace log changed since it was built) the gate does not answer from it; in every case the worker thread
then refreshes the store and **recomputes the column before the op runs**, and that answer is the one that counts
(F8: rechecked on every request). Steps 8 to 11 are the backend's (:class:`orch.host.dispatch.RegistryBackend`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from orch import layout as layout_mod
from orch.layout import LayoutError

from . import frames, shareauto
from .auth import AuthView, Verified, build_auth, secret_in_request
from .blobs import BlobStore
from .columns import Column, OpKind, refusal, resolve
from .custody import check_tree, check_workspace_files
from .errors import HostError
from .journal import Batch, Cursors, Journal
from .outcomes import OutcomeStore, StoreReconciler
from .phonehints import HintStore
from .registry import Entry
from .relayid import RelayIdentity
from .requests import RequestStore
from .transport import Peer, Session

if TYPE_CHECKING:
    from .service import Host

__all__ = ["Call", "OpBackend", "StatusBackend", "WorkspaceGate", "WorkspaceService", "ok_result", "person_ok"]

State = Literal["ok", "degraded", "unavailable"]
LOG = logging.getLogger(__name__)
Phase = Literal["read", "write", "wait"]


def person_ok(workspace: Any, person: str, now: float) -> bool:
    """F8: ``person`` is a member with a device that is not removed, revoked or expired and carries ``decide``."""
    from orch.identity.certs import person_scope_ok

    if person not in workspace.members:
        return False
    for device in workspace.devices.values():
        if device.person != person or device.removed or device.revoked:
            continue
        if device.expires_ms is not None and device.expires_ms <= now * 1000:
            continue
        if person_scope_ok({"scopes_max": list(device.scopes)}):
            return True
    return False


def deny_append(log: str, event: dict[str, Any]) -> None:
    """The store's append check outside a write's ``apply`` (reads, dry runs, person reads): no caller event may land
    without an outcome record listing it (§0.7). The host's own healing events are exempt in the store."""
    from orch.store import StoreError

    raise StoreError("internal", f"{event.get('type')}: an append outside a write")


def ok_result(data: Any, *, seq: int = 0, duplicate: bool = False) -> dict[str, Any]:
    return {
        "data": data,
        "key": None,
        "seq": seq,
        "cursor": 0,
        "hints": [],
        "lines": [],
        "duplicate": duplicate,
        "exit": 0,
        "head": None,
    }


class WorkspaceService:
    def __init__(self, entry: Entry, state_dir: Path, host_uid: int, *, clock: Callable[[], float] = time.time):
        self.entry = entry
        self.state_dir = Path(state_dir)
        self.host_uid = host_uid
        self.clock = clock
        self.executor = ThreadPoolExecutor(1, thread_name_prefix=f"orch-ws-{entry.workspace_id[:8]}")
        self.store: Any = None
        self.outcomes = OutcomeStore(
            self.state_dir,
            entry.workspace_id,
            host_uid,
            clock=clock,
            reconciler=StoreReconciler(lambda: self.store),
        )
        self.state: State = "unavailable"
        self.cause = "not opened yet"
        #: the authentication snapshot (:mod:`orch.host.auth`), rebuilt on the worker thread after every refresh
        self.auth: AuthView | None = None
        #: the handlers' capabilities (D-P2-9): built once per workspace on the worker thread, closed with it
        self.caps: Any = None
        #: the host's pool of git reader children (:class:`orch.gitread.client.ReaderPool`, F10), set by the host;
        #: ``None``: git is denied to this workspace's handlers
        self.git_pool: Any = None
        #: the declared repo paths last pinned (:meth:`sync_git`), and when to retry pins that failed (monotonic)
        self._git_declared: tuple[str, ...] | None = None
        self._git_retry_at = 0.0
        self._git_backoff = 0.0
        #: set while ``unregister`` drains this workspace: new requests answer ``workspace.unavailable``
        self.closing = False
        #: the subscription journal (:mod:`orch.host.journal`, H6a), built at open; worker thread only
        self.journal: Journal | None = None
        #: where the worker hands journal batches (the host's bus, on the event loop); ``None``: nobody listens
        self.publish: Callable[[Batch], None] | None = None
        #: the host's bus (:class:`orch.host.bus.Bus`): subscriptions and parked waits; set by the host
        self.bus: Any = None
        #: cursor authentication, the host's (set by the host; a private key otherwise)
        self.cursors = Cursors(os.urandom(32))
        #: why the journal could not be built ("" while it is fine): the workspace stays degraded
        self.journal_failed = ""
        #: human requests (§0.8.1, H5a): ``<state>/hosts/<ws>/requests/``
        self.requests: RequestStore | None = RequestStore(self.state_dir, entry.workspace_id, host_uid, clock=clock)
        #: B10 Part B: the unsigned phone-approval hints (``<state>/hosts/<ws>/phone-hints.json``)
        self.phone_hints = HintStore(self.state_dir, entry.workspace_id, host_uid, clock=clock)
        self.outcomes.roll_forward = self._roll_forward
        self.outcomes.prepared = self._prepared
        #: blobs (§0.10, H4a2): ``<state>/hosts/<ws>/blobs/``; the host shares one quota among its workspaces
        self.blobs = BlobStore(self.state_dir, entry.workspace_id, host_uid, clock=clock)
        if self.requests is not None:  # a request's file bytes are charged until it goes terminal (stripped)
            self.requests.on_terminal = self.blobs.release_request
        #: the relay worker (§11A.4, R5's ``RelayWorker``: ``stop()``), ``None`` while none runs
        self.relay: Any = None
        #: R5's start hook ``fn(service)``: it starts and sets :attr:`relay` (under :attr:`relay_lock`, after
        #: :func:`orch.host.relaylink.may_start`); ``None``: no relay client in this build
        self.relay_starter: Callable[[WorkspaceService], None] | None = None
        #: serializes the relay worker's start against ``orch host relay stop`` and ``close``; R5's start runs under it
        #: re-entrant (R5 round 3): a seam factory run by the starter under this lock on the worker thread that
        #: reaches the foreign-append audit cannot self-deadlock the workspace
        self.relay_lock = threading.RLock()
        self.relay_cond = threading.Condition(self.relay_lock)
        #: stops in progress (a worker taken out of :attr:`relay` whose ``stop`` and ``join`` have not returned)
        self._relay_stopping = 0
        #: a worker whose threads outlived the bounded join (alarm ``relay_thread_stuck``): no new worker until it ends
        self._relay_stuck: Any = None
        #: the bound of waiting for a stopping worker's threads (§11A.7: the longest total deadline, 120 s)
        self.relay_join_seconds = 120.0
        #: why no relay worker runs (``host.status.relay``): ``not_linked``, ``stopped``, the R6 gate, ``start_failed:
        #: <code>``, ``convenience_events_present``, ``relay_thread_stuck``, …; ``None`` while one runs or before open
        self.relay_reason: str | None = None
        #: §11A.17: the host's :class:`orch.host.devmode.DevHost` (``None`` on a normal host) and whether this
        #: workspace passed the dev gate at its last fresh check (serve, and every column-R L2)
        self.dev: Any = None
        self.dev_ok = False

    @property
    def workspace_id(self) -> str:
        return self.entry.workspace_id

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future[Any]:
        return self.executor.submit(fn, *args)

    async def call(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.wrap_future(self.submit(fn, *args))

    # -- on the worker thread
    def _unavailable(self, cause: str) -> None:
        self.state, self.cause = "unavailable", cause[:300]
        if self.store is not None:
            self.store.close()
            self.store = None

    def open(self) -> None:
        from orch.custody import FileBackend
        from orch.store import BackendSigner, Store, StoreError
        from orch.store.pins import HostPins

        wid = self.entry.workspace_id
        try:
            if os.path.realpath(self.entry.root) != self.entry.root:
                raise HostError("custody.unsafe_mode", f"{self.entry.root} now resolves elsewhere")
            check_tree(Path(self.entry.root), self.host_uid)
            try:  # before the custody list: a store from an older version in the root is named, not "no .orch"
                layout_mod.store_of(self.entry.root)
            except LayoutError as exc:
                raise StoreError("validation.path", str(exc)) from None
            check_workspace_files(self.state_dir, wid, Path(self.entry.root), self.host_uid)
            pin = HostPins(self.state_dir, wid).genesis()
            if pin is None:
                raise HostError("trust.genesis_mismatch", "no genesis pin for this workspace")
            keys = self.state_dir / "hosts" / wid / "keys"
            backend = FileBackend(keys) if keys.is_dir() else None
            signer = BackendSigner(backend, "wsk") if backend is not None and backend.exists("wsk") else None
            store = Store.open(
                self.entry.root,
                expected_workspace_id=wid,
                expected_genesis=pin,
                host=signer,
                host_state_dir=self.state_dir,
                clock=self.clock,
                load="lazy",
                # C3b 15.3: a dev host serves sandbox workspaces only (11A.17.2 check 4), so its replay may verify dev
                # hub material; a normal host never does
                hub_dev=getattr(self, "dev", None) is not None,
            )
        except (HostError, StoreError, OSError, ValueError, KeyError) as exc:
            self._unavailable(f"{getattr(exc, 'code', type(exc).__name__)}: {exc}")
            return
        if self.store is not None:
            self.store.close()
        self.store = store
        self._judge()
        self._snapshot()
        if self.state != "unavailable":
            self.sync_git()  # pins at workspace open (F7), under this workspace's principal
            try:
                self.outcomes.open()
                from .relaypair import register as pair_register

                # R6c (11A.15.19 item 13): the relay.pair_device source BEFORE human.recover, so an interrupted
                # pairing submit rolls forward through R6's hooks; R6's own open-time recovery runs here too
                pair_register(self)
                if self.requests is not None:
                    from .human import recover

                    self.requests.open()
                    recover(self)  # a crash between two appends of a human submission rolls forward (§0.8.1)
                from .relayentry import register as relay_register

                # R5 (§11A.5 "For R3/R5/R6"): this service's relay_fields hook, before reconcile_all rolls a pending
                # relay.submit forward. Only the hook: the result builder registers itself at import (relaybuild), and
                # relay.submit.recover runs in the bridge's start (J9), never here
                relay_register(self)
                self.blobs.open()
                if self.requests is not None:  # the stored requests' file bytes count again (F9)
                    for rid, principal, size in self.requests.held():
                        uid = principal_uid(principal)
                        if uid is None:  # never the whole workspace: this one record is not charged
                            LOG.error("request %s: unparseable principal; its file bytes are not charged", rid)
                            continue
                        self.blobs.hold_request(rid, principal, uid, size, force=True)
                self.outcomes.reconcile_all()  # a crash between an append and its record (§0.7)
                self.outcomes.sweep()
            except (HostError, OSError, ValueError) as exc:
                self._unavailable(f"{getattr(exc, 'code', type(exc).__name__)}: {exc}")
        if self.state != "unavailable":
            self._build_journal(audit=False)
        self._relay_open()
        from . import peerops

        peerops.maybe_start(self)  # V3 C12: the receive thread of a workspace that uses peers (never fails the open)
        shareauto.maybe_start(self)  # V3 C6: the debounced share pass of a workspace that has share state
        from . import logsyncops

        logsyncops.start(self)  # V3 C14: the hub log_sync sender of a full-mode workspace (never fails the open)
        from . import cutover

        cutover.start(self)  # V3 C10: resume an owed cut-over (closing; a dev pre-C10 full join) in the background

    # -- the relay worker (§11A.4 "The hook", R5)
    def _relay_open(self) -> None:
        """The end of :meth:`open`, on the worker thread: start the relay worker when ``link.json`` exists (§11A.4).
        Never fails the open: whatever keeps the worker from starting is :attr:`relay_reason`."""
        from .relaylink import link_path, unlink_pending

        if self.state == "unavailable":
            self.relay_reason = "workspace_unavailable"
            return
        if unlink_pending(self.state_dir, self.workspace_id):  # an unlink a restart interrupted: finish it, never serve
            from .relayhook import unlink_job

            try:
                done = unlink_job(self)
            except Exception as exc:  # noqa: BLE001 - the intent stays: nothing starts, the next open tries again
                LOG.error("relay unlink of %s could not finish: %s", self.workspace_id, type(exc).__name__)
                self.relay_reason = "unlink_pending"
                return
            if done != "stale":  # a stale intent (another link's) was discarded: this link goes on as usual
                return
        if not os.path.lexists(link_path(self.state_dir, self.workspace_id)):
            self.relay_reason = "not_linked"
            return
        with self.relay_cond:
            self._relay_try_start()

    def _relay_try_start(self) -> bool:
        """Under :attr:`relay_cond`: start the worker through :attr:`relay_starter` when nothing blocks it (the
        closing workspace, a running, stopping or stuck worker, :func:`orch.host.relaylink.start_blocker`: the stop
        marker, the link, the R6 gate). A starter that raises leaves no worker and names why; never raised."""
        from .relaylink import start_blocker

        if self.closing:
            self.relay_reason = "closing"
            return False
        if self.relay is not None:
            return False
        if self._relay_stopping:
            self.relay_reason = "stop_in_progress"
            return False
        if self._stuck_alive():
            self.relay_reason = "relay_thread_stuck"
            return False
        if self.state == "unavailable":
            self.relay_reason = "workspace_unavailable"
            return False
        why = start_blocker(self.state_dir, self.workspace_id, self.host_uid)
        if why is not None:
            self.relay_reason = why
            return False
        if self.relay_starter is None:
            self.relay_reason = "no_relay_client"
            return False
        try:
            self.relay_starter(self)
        except Exception as exc:  # noqa: BLE001 - a failed start never fails the open or the caller: it is reported
            self.relay = None
            code = getattr(exc, "code", None) or type(exc).__name__
            self.relay_reason = f"start_failed: {code}"[:120]
            LOG.error("relay worker of %s did not start: %s", self.workspace_id, code)
            return False
        if self.relay is None:
            self.relay_reason = "start_failed: no worker"
            return False
        self.relay_reason = None
        return True

    def _relay_halt(self, code: str) -> None:
        """Stop the running worker because of what the workspace now holds (§11A.6 "Foreign appends":
        ``convenience_events_present``), on the worker thread: :meth:`stop` at once (no store job is queued from
        now on), the bounded join on a thread of its own (the relay threads may wait for this very worker thread).
        Not persisted: the next start checks again."""
        with self.relay_cond:
            worker = self._take_relay()
            self.relay_reason = code
        if worker is None:
            return
        alarm = getattr(worker, "alarm", None)
        if callable(alarm):
            try:
                alarm(code, "the merged history holds an auth convenience event")
            except Exception:  # noqa: BLE001 - the stop matters, not the alarm bookkeeping
                LOG.exception("relay alarm of %s failed", self.workspace_id)
        try:
            worker.stop()
        except Exception:  # noqa: BLE001 - _shutdown_relay below retries and reports it
            LOG.exception("relay worker of %s: stop failed", self.workspace_id)

        def finish() -> None:
            ended = False
            try:
                ended = self._shutdown_relay(worker)
            finally:
                self._end_stop(worker, ended)

        threading.Thread(target=finish, name=f"orch-relay-halt-{self.workspace_id[:8]}", daemon=True).start()

    # -- the subscription journal (H6a, D-P2-12, F11)
    def _build_journal(self, *, audit: bool) -> None:
        """(Re)build the journal from the logs and hook it into the store. A rebuild after a foreign append or a
        restore marks the workspace ``degraded`` while it runs, writes one audit line, and its batch ends every
        subscription with ``reset`` (the bus does that on a reset batch).

        A build that fails leaves no journal: the workspace stays ``degraded`` (:attr:`journal_failed`), every
        subscription ends with ``reset``, ``sub.open`` answers ``workspace.unavailable``, and the next try is the 60 s
        check (no retry on every op)."""
        store = self.store
        if store is None:
            return
        journal = self.journal or Journal(self.entry.workspace_id, self.entry.store_dir, self.cursors)
        if audit:
            self.state, self.cause = "degraded", "the subscription journal is being rebuilt"
            LOG.warning(
                "audit: workspace %s: foreign append or restore (%s); rebuilding the journal, ending subscriptions",
                self.entry.workspace_id,
                journal.cause or self.journal_failed or "start",
            )
        store.before_append, store.on_commit = None, None
        try:
            batch = journal.build(store)
        except Exception as exc:  # noqa: BLE001 - whatever failed, there is no journal to serve from
            if audit:
                self._relay_audit()
            self.journal = None
            self.journal_failed = f"journal: {getattr(exc, 'code', type(exc).__name__)}: {exc}"[:300]
            LOG.error("audit: workspace %s: the journal cannot be built: %s", self.entry.workspace_id, exc)
            if self.publish is not None:  # end every subscription on the old feed
                self.publish(Batch(self.entry.workspace_id, "", 0, (), {}, reset=True))
            if self.store is not None:
                self._judge()
            return
        self.journal, self.journal_failed = journal, ""
        store.before_append, store.on_commit = journal.check, self._committed_hook(journal)
        if self.store is not None:
            self._judge()
        if audit:
            self._relay_audit()
        if self.publish is not None:
            self.publish(batch)

    #: event types whose commit can revoke a device (11A.9, 11A.5 "Wake chaining (B1)"): the relay unpublished
    #: consumer re-judges its ledger at once after one of these
    _REVOKING_COMMITS = frozenset({"device.revoked", "member.removed", "restore"})

    def _committed_hook(self, journal: Any) -> Callable[[str, dict[str, Any]], None]:
        """``Store.on_commit``: the journal's, then the relay wakes (11A.5 "Wake chaining (B1)"): the publisher on every
        append (it publishes now, not at its next idle check), the membership thread (it re-plans) and the
        unpublished consumer after a commit that can revoke. Called inside the store's critical section; the wakes only
        set events and never raise."""
        from . import logsyncops

        committed = journal.committed

        def hook(log: str, event: dict[str, Any]) -> None:
            try:
                committed(log, event)
            finally:
                self._relay_wake(event)
                shareauto.touch(self)  # V3 C6: a debounced share pass (selective mode only; flag + wake, never raises)
                logsyncops.wake(
                    self
                )  # V3 C14: a committed event, so the hub view follows (debounced; only sets a flag)

        return hook

    def _relay_wake(self, event: Any) -> None:
        worker = self.relay
        if worker is None:
            return
        try:
            wake = getattr(getattr(worker, "publisher", None), "wake", None)  # NoPublisher has none
            if callable(wake):
                wake()
            for companion in getattr(worker, "companions", ()):  # R6b's membership thread re-plans on any commit
                wake = getattr(companion, "wake", None)
                if callable(wake):
                    wake()
            if isinstance(event, dict) and event.get("type") in self._REVOKING_COMMITS:
                wake = getattr(getattr(worker, "unpublished", None), "wake", None)
                if callable(wake):
                    wake()
        except Exception:  # noqa: BLE001 - a wake is a hint: never fail the store's commit
            LOG.exception("relay wake of %s failed", self.workspace_id)

    def _relay_audit(self) -> None:
        """§11A.6 "Foreign appends": after a refresh that loaded events this host did not append, a running relay
        worker is stopped when the history now holds a convenience event (or the mark or flag is set). The worker is
        read under ``relay_cond``: a start in progress (which never waits for this thread) finishes first, so a worker
        whose precheck already passed is always seen here (review R1). A check that raises fails closed."""
        with self.relay_cond:
            if self.relay is None:
                return
        from .relayhook import convenience_block

        try:
            code = convenience_block(self)
        except Exception as exc:  # noqa: BLE001 - cannot tell: fail closed, the worker stops
            LOG.error("relay convenience check of %s failed: %s", self.workspace_id, type(exc).__name__)
            code = "relay_convenience_check_failed"
        if code is not None:
            self._relay_halt(code)

    def sub_cursor(self, principal: str) -> str:
        """The ``sub_cursor`` of a snapshot read (§0.9), on the worker thread right where the read takes its state:
        the journal head after a foreign-append check, so every event the state shows is in the journal.
        ``ticket.query``, ``ticket.get`` and ``needs`` (H6b) return it; ``sub.open {after}`` with it loses and repeats
        nothing."""
        self.sync(check=True)
        if self.journal is None:
            raise HostError("workspace.unavailable", "the subscription journal is not available")
        return self.journal.cursor(principal)

    def sync(self, *, check: bool = False, retry: bool = False) -> None:
        """After an op (worker thread): look for a foreign append (cheaply; every log with ``check``, the 60 s check
        and ``sub.open``), rebuild a stale journal, else hand its new entries to the bus with a fresh authentication
        snapshot. A journal that failed to build is retried only with ``retry`` (the 60 s check)."""
        store = self.store
        if store is None:
            return
        if self.journal is None:
            if retry and self.journal_failed and self.state != "unavailable":
                self._build_journal(audit=True)
                self._snapshot()
            return
        journal = self.journal
        from orch.store import StoreError

        try:
            if check:
                with store.locked():  # every log's signature, against the files as the lock finds them
                    journal.check(full=True)
            else:
                journal.check()  # O(1): the store's own counter and one stat
        except (StoreError, OSError) as exc:
            LOG.warning("journal check of %s failed: %s", self.entry.workspace_id, exc)
            return
        if journal.stale:
            self._build_journal(audit=True)
            self._snapshot()
            return
        batch = journal.take()
        if batch is None:
            return
        self._snapshot()  # delivery revalidates against this: a revoke or removal it carries counts at once
        if self.publish is not None:
            self.publish(batch)

    def _prepared(self, record: dict[str, Any]) -> dict[str, Any] | str:
        from .human import outcome_prepared

        return outcome_prepared(self, record)

    def _roll_forward(self, record: dict[str, Any]) -> dict[str, Any] | str:
        from .human import outcome_fields

        return outcome_fields(self, record)

    def _judge(self) -> None:
        ws = self.store.state.workspace
        if ws.genesis is None or ws.workspace_id != self.entry.workspace_id:
            self._unavailable("the chain is broken at the genesis")
        elif ws.frozen or any(not item.acknowledged for item in ws.invalid):
            self.state, self.cause = "degraded", "an unacknowledged invalid event"
        elif self.journal_failed:
            self.state, self.cause = "degraded", self.journal_failed
        else:
            self.state, self.cause = "ok", ""

    def _snapshot(self) -> None:
        from orch.store import StoreError

        if self.store is None:
            self.auth = None
            return
        try:
            self.auth = build_auth(self.store, self.entry.store_dir)
        except (StoreError, OSError, ValueError) as exc:
            self._unavailable(f"{getattr(exc, 'code', type(exc).__name__)}: {exc}")
            self.auth = None

    def refresh(self) -> None:
        """Re-read the store, judge the state and rebuild the authentication snapshot (worker thread)."""
        from orch.store import StoreError

        if self.store is None:
            return
        try:
            self.store.refresh()
        except StoreError as exc:
            self._unavailable(f"{exc.code}: {exc}")
            self.auth = None
            return
        self._judge()
        self._snapshot()
        self.sync_git()  # a settings.changed that added, moved or removed a repo (F7)

    def person_valid(self, person: str) -> bool:
        self.refresh()
        return self.auth is not None and self.auth.person_ok(person, self.clock())

    def verify_grant(self, grant: str | None) -> Verified:
        """``auth.grant`` checked against the freshly refreshed store (worker thread): §0.5, F4."""
        self.refresh()
        if grant is None:
            return Verified("absent")
        if self.auth is None:
            return Verified("malformed" if self._malformed(grant) else "unverified")
        return self.auth.verify(grant, self.clock())

    @staticmethod
    def _malformed(grant: str) -> bool:
        from orch.identity import Refused, parse_grant

        try:
            parse_grant(grant)
        except Refused:
            return True
        return False

    def capabilities(self) -> Any:
        """The handlers' :class:`~orch.ops.caps.Capabilities` (host mode), built once (worker thread)."""
        if self.caps is None:
            from orch.ops.caps import Capabilities

            # The workspace root only: the host's state directory (keys, pins, outcome records) is never a handler's
            # file root, and no ORCH_STATE_DIR or ORCH_WORKSPACE reaches a handler (the store is injected).
            git = None
            if self.git_pool is not None:
                from orch.gitread.client import HostGitReader, PinStore
                from orch.gitread.errors import Unavailable

                # pins live in the host's state directory (0600), one file per workspace. Tier A runs the
                # tree.check job in the host's own worktree-profile child (H10a); tier B waits for the worker (H10).
                try:
                    pins = PinStore(self.state_dir / "hosts" / self.workspace_id / "git-pins.json")
                    git = HostGitReader(
                        self.git_pool,
                        pins,
                        principal=f"host:{self.workspace_id}",  # pins queue per workspace, never host-wide (F10)
                        tree_checks=self.entry.tier == "A",
                    )
                except (OSError, ValueError, Unavailable):
                    git = None  # an unreadable pin file is never replaced by fresh pins: git is denied (fail closed)
            self.caps = Capabilities.host((os.fspath(self.entry.store_dir),), env={}, clock=self.clock, git=git)
        return self.caps

    def sync_git(self) -> None:
        """Pin the repos ``settings.repos`` declares, at workspace open and whenever the declared set changes (a
        ``settings.changed``, F7), and drop pins of repos no longer declared; an existing pin is never replaced
        (:meth:`orch.gitread.client.HostGitReader.sync`). Pins run under this workspace's own principal. A repo that
        cannot be pinned is remembered and retried only after a backoff (1 min, doubling to 1 h) or the next change of
        the declared set, never on every read; until then each read of it is ``observe.unavailable``. Cheap when
        nothing changed (worker thread). Called centrally: by :meth:`open`, :meth:`refresh` and :meth:`caps_for`."""
        caps = self.capabilities()
        reader = caps.git.reader
        if reader is None or self.store is None:
            return
        repos = self.store.state.workspace.repos
        declared = tuple(
            sorted(os.fspath(p) for n in repos if (p := caps.git.repo_path(self.store.layout, repos, n)) is not None)
        )
        now = time.monotonic()
        if declared == self._git_declared and now < self._git_retry_at:
            return
        if declared != self._git_declared:
            self._git_backoff = 0.0
        try:
            failed = any(why is not None for why in reader.sync(list(declared)).values())
        except OSError:
            failed = True  # the pin file cannot be written: nothing new is pinned (PinStore writes the file first)
        self._git_declared = declared
        if failed:
            self._git_backoff = min(3600.0, max(60.0, self._git_backoff * 2))
            self._git_retry_at = now + self._git_backoff
        else:
            self._git_backoff, self._git_retry_at = 0.0, float("inf")

    def caps_for(self, principal: str) -> Any:
        """One request's capabilities: pins up to date (:meth:`sync_git`), git reads under ``principal``."""
        self.sync_git()
        return self.capabilities().with_principal(principal)

    def _shutdown_relay(self, worker: Any) -> bool:
        """R5's worker contract: ``stop()`` (§11A.7 steps 1 to 3), then ``join(timeout) -> bool``, true when every
        relay thread has ended. A worker without ``join`` counts as ended when ``stop`` returns. Called without the
        lock; returns whether the worker ended."""
        try:
            worker.stop()
            join = getattr(worker, "join", None)
            ended = True if join is None else bool(join(self.relay_join_seconds))
        except Exception:  # noqa: BLE001 - a failing stop is a worker that may still run
            LOG.exception("relay worker of %s: stop failed", self.workspace_id)
            ended = False
        if not ended:
            LOG.error("relay_thread_stuck: workspace %s: a relay thread outlived the stop", self.workspace_id)
        return ended

    def _take_relay(self) -> Any:
        """Under :attr:`relay_cond`: take the running worker out and count its stop as in progress."""
        worker, self.relay = self.relay, None
        if worker is not None:
            self._relay_stopping += 1
        return worker

    def _end_stop(self, worker: Any, ended: bool) -> None:
        with self.relay_cond:
            self._relay_stopping -= 1
            if not ended:
                self._relay_stuck = worker
            self.relay_cond.notify_all()

    def _stuck_alive(self) -> bool:
        """Under :attr:`relay_cond`: a retained stuck worker whose threads still run (``join(0)`` false); a stuck
        worker whose threads ended is forgotten."""
        stuck = self._relay_stuck
        if stuck is None:
            return False
        alive = getattr(stuck, "alive", None)
        if callable(alive):  # R5's worker: no join and no I/O under the lock (its reaper releases what it holds)
            if alive():
                return True
            self._relay_stuck = None
            return False
        join = getattr(stuck, "join", None)
        if join is not None and not join(0):
            return True
        self._relay_stuck = None
        return False

    def relay_stuck_alive(self) -> bool:
        """Under :attr:`relay_cond`: a retained stuck worker still runs (``host.status.relay``)."""
        return self._stuck_alive()

    def relay_stopping_count(self) -> int:
        """Under :attr:`relay_cond`: stops in progress (``host.status.relay``)."""
        return self._relay_stopping

    def _stops_done(self) -> bool:
        """Under :attr:`relay_cond`: wait (bounded) until no stop is in progress."""
        return self.relay_cond.wait_for(lambda: self._relay_stopping == 0, timeout=self.relay_join_seconds + 30)

    def relay_stop(self) -> str:
        """``orch host relay stop`` (§11A.4): persist the stop (it outlives a restart), then stop a running worker
        and wait for its threads (bounded). Answers only once the shutdown is done: ``"stopped"``, ``"stuck"`` (a
        thread outlived the bound) or ``"none"`` (no worker was running; a stop in progress was waited for)."""
        from .relaylink import mark_stopped

        with self.relay_cond:
            mark_stopped(self.state_dir, self.workspace_id, self.host_uid, clock=self.clock)
            self.relay_reason = "stopped"
            worker = self._take_relay()
            if worker is None:  # "none" only when no worker runs at all, a retained stuck one included
                return "none" if self._stops_done() and not self._stuck_alive() else "stuck"
        ended = False
        try:
            ended = self._shutdown_relay(worker)
        finally:
            self._end_stop(worker, ended)
        return "stopped" if ended else "stuck"

    def relay_start(self) -> str:
        """``orch host relay start``: wait for a stop in progress, clear the stop, and start the worker through R5's
        hook when the workspace is linked and served. Returns ``"started"`` or ``"none"``; ``lock.busy`` while a
        stop has not finished or a stuck worker's threads still run (no second worker beside them)."""
        from .relaylink import clear_stopped, unlink_pending

        with self.relay_cond:
            if self.closing:
                raise HostError("workspace.unavailable", "the workspace is closing")
            if unlink_pending(self.state_dir, self.workspace_id):
                raise HostError("lock.busy", "unlink_pending: an unlink of this workspace has not finished yet")
            if not self._stops_done():
                raise HostError("lock.busy", "a relay stop is still in progress; try again")
            if self.closing:
                raise HostError("workspace.unavailable", "the workspace is closing")
            if self._stuck_alive():
                raise HostError("lock.busy", "relay_thread_stuck: a stopped relay thread still runs; restart the host")
            clear_stopped(self.state_dir, self.workspace_id, self.host_uid)
            return "started" if self._relay_try_start() else "none"

    def relay_unlink(self) -> str:
        """``orch host relay unlink`` (§11A.6). Crash-safe order (R5 round 3, D-1): the durable unlink intent, then
        the stop marker, both under :attr:`relay_cond` and before the worker is touched; then the bounded stop; then,
        in one worker job holding :attr:`relay_cond` and the store lock, the rename of ``link.json`` to
        ``unlinked-<linked_at>.json`` (never deleted: the reservation is permanent) and the intent cleared. Until
        then no worker starts (``start_blocker``: ``unlink_pending``), ``relay start`` is refused, and an open after a
        restart finishes it. Refused while a worker's threads still run (``lock.busy``, the intent stays: the next
        open finishes it). Returns the new file name (``"done"`` when a crash had already renamed it)."""
        from .custody import read_json
        from .relayhook import unlink_job
        from .relaylink import link_path, mark_stopped, mark_unlink_intent, unlink_pending

        with self.relay_cond:
            if self.closing:
                raise HostError("workspace.unavailable", "the workspace is closing")
            if not os.path.lexists(link_path(self.state_dir, self.workspace_id)) and not unlink_pending(
                self.state_dir, self.workspace_id
            ):
                raise HostError("relay.not_linked", "the workspace has no relay link")
            if not unlink_pending(self.state_dir, self.workspace_id):  # an existing intent is never rebound
                link = read_json(link_path(self.state_dir, self.workspace_id), self.host_uid)
                at = link.get("linked_at") if isinstance(link, dict) else None
                mark_unlink_intent(
                    self.state_dir, self.workspace_id, self.host_uid,
                    linked_at=at if type(at) is int and at >= 0 else None, clock=self.clock,
                )  # fmt: skip
            mark_stopped(self.state_dir, self.workspace_id, self.host_uid, clock=self.clock)
            self.relay_reason = "unlink_pending"
            worker = self._take_relay()
            if worker is None and (not self._stops_done() or self._stuck_alive()):
                raise HostError("lock.busy", "relay_thread_stuck: a stopped relay thread still runs; restart the host")
        if worker is not None:
            ended = False
            try:
                ended = self._shutdown_relay(worker)
            finally:
                self._end_stop(worker, ended)
            if not ended:
                raise HostError("lock.busy", "relay_thread_stuck: the relay worker did not stop; nothing was renamed")
        pending = self.submit(lambda: unlink_job(self))
        try:
            name = pending.result(timeout=60)
        except TimeoutError:  # the rename stays queued behind the worker thread and will still run
            self.relay_reason = "unlink_pending"
            raise HostError(
                "lock.busy",
                "unlink_pending: the rename is queued behind the workspace's worker and completes when it runs "
                "(durably: a restart finishes it); no relay worker starts meanwhile",
                hint="check with orch host status: the link shows as unlinked once it ran",
            ) from None
        return name

    def close(self) -> None:
        from . import peerops

        peerops.stop(self)  # V3 C12: the ws->ws receive thread, before the store goes
        shareauto.stop(self)  # V3 C6: the share pass thread, before the store goes
        from . import logsyncops

        logsyncops.stop(self)  # V3 C14
        # §11A.7 shutdown: the relay worker stops first and its threads are waited for (bounded by 120 s) before the
        # job that closes the store is queued; jobs it queued before stopping run first (the worker FIFO)
        with self.relay_cond:
            self.closing = True  # before the worker is taken: a racing relay_start never starts a new one
            self.relay_reason = "closing"
            stuck = self._relay_stuck  # an earlier stop's stuck worker: stays tracked while it is joined below
            worker = self._take_relay()
            if worker is None:
                self._stops_done()
        if worker is not None:
            ended = False
            try:
                ended = self._shutdown_relay(worker)
            finally:
                self._end_stop(worker, ended)
        join = getattr(stuck, "join", None)
        if join is not None:
            if join(self.relay_join_seconds):  # a stuck worker's threads too, bounded
                with self.relay_cond:
                    if self._relay_stuck is stuck:
                        self._relay_stuck = None
            else:  # kept: a concurrent relay_stop keeps answering "stuck" (Codex #455 delta 2)
                LOG.error("relay_thread_stuck: workspace %s: closing with a relay thread alive", self.workspace_id)

        def shut() -> None:
            rec = getattr(self, "session_records", None)
            if rec is not None:
                rec.flush()  # the throttled session records (host/sessions.py)
            if self.store is not None:
                self.store.close()
                self.store = None
            self.blobs.close()
            if self.caps is not None:
                self.caps.close()
                self.caps = None

        try:
            self.submit(shut).result(timeout=30)
        except RuntimeError:
            pass  # already shut down
        self.executor.shutdown(wait=True)


@dataclass
class Call:
    service: WorkspaceService
    session: Session
    peer: Peer
    frame: dict[str, Any]
    column: Column
    viewer: str | None
    #: the verified grant (column G), else ``None``: an unverified grant is no identity
    verified: Verified = Verified("absent")
    #: the principal of this request (§0.5): ``g:<grant id>@<uid>`` for a verified grant, else ``uid:<n>``
    principal: str = ""
    #: for a backend with ``state_checks``: computes the authoritative column on the worker thread, inside the op's
    #: own job (so ``opts.timeout_ms`` covers it, and one connection's writes stay in one FIFO); the backend calls
    #: :meth:`authorize` first
    columns: Callable[[], tuple[Column, str | None, Verified]] | None = None
    kind: OpKind | None = None
    #: the transport's handle of this request (``sub.open``: no ``evt`` of the subscription before its ``res``)
    request: frames.Request | None = None
    #: the verified identity of a bridge request (column R, §11A.4), else ``None``; set only by the relay entry
    relay: RelayIdentity | None = None

    def authorize(self) -> None:
        """Recompute the column from the refreshed store and refuse outside it (§0.6 steps 5 and 6, F8).

        A relay call (``relay`` set, H12a) recomputes through the relay entry's ``relay_columns`` (passed as
        :attr:`columns`) and keeps ``principal = "r:<device_id>"``: it never derives a principal from a UID. Every
        inconsistency refuses (``op.not_allowed``): a relay identity on a socket peer or the reverse, no recompute, a
        socket column or another viewer than the device's person, an op outside R's closed list."""
        op = self.frame.get("op") if isinstance(self.frame, dict) else None
        if self.column.name == "R" and self.relay is None:  # only a relay identity is ever column R (before any return)
            raise HostError("op.not_allowed", f"{op} is not allowed here")
        relay_peer = self.peer.kind == "relay"
        if self.relay is not None or relay_peer:
            self._authorize_relay(op)
            return
        if self.columns is None:
            return
        self.column, self.viewer, self.verified = self.columns()
        if self.column.name == "R":  # only a relay identity is ever column R
            raise HostError("op.not_allowed", f"{op} is not allowed here")
        self.principal = principal(self.peer.uid, self.verified)
        code = refusal(self.column, self.kind, op) if self.kind is not None else None
        if code is not None:
            raise HostError(code, f"{op} is not allowed here")

    def _authorize_relay(self, op: Any) -> None:
        identity = self.relay

        def refuse() -> HostError:
            return HostError("op.not_allowed", f"{op} is not allowed here")

        if (
            identity is None
            or self.peer.kind != "relay"
            or self.peer.uid != -1
            or self.peer.person != identity.person_id
            or getattr(self.service, "workspace_id", None) != identity.workspace_id
            or self.columns is None
        ):
            raise refuse()
        kind = _relay_kind(op)  # derived here, never the caller's: a caller's kind that differs refuses
        if kind is None or (self.kind is not None and self.kind != kind):
            raise refuse()
        self.kind = kind
        column, viewer, _verified = self.columns()
        if column.name == "R" and viewer != identity.person_id:
            raise refuse()
        if column.name != "R" and column.error is None:  # a socket column never serves a relay request
            raise refuse()
        self.column, self.viewer, self.verified = column, (viewer if column.name == "R" else None), Verified("absent")
        self.principal = identity.principal
        code = refusal(self.column, self.kind, op)
        if code is not None:
            raise HostError(code, f"{op} is not allowed here")
        if self.column.name == "R" and op != "reader.declare":  # H12d-3: serving is bound to the current rd (9.5)
            from .readerops import check_rd

            check_rd(self.service, identity)


def _relay_kind(op: Any) -> OpKind | None:
    """The kind of a relay op: ``device_signed`` for ``relay.submit``, else the socket backend's own ``op_kind``; an
    op nobody serves has none."""
    from .columns import DEVICE_SIGNED_OPS
    from .dispatch import op_kind

    if not isinstance(op, str):
        return None
    if op in DEVICE_SIGNED_OPS:
        return "device_signed"
    kind = op_kind(op)
    return kind[0] if kind is not None else None


class OpBackend(Protocol):
    """**Seam (H4a1):** the workspace ops. ``kind`` is (matrix category, phase) or ``None`` for an unknown op."""

    caps: list[str]
    ops: dict[str, int]

    def kind(self, op: str) -> tuple[OpKind, Phase] | None: ...

    async def run(self, call: Call) -> dict[str, Any]: ...


def principal_uid(value: str) -> int | None:
    """The UID of a principal (``uid:<n>`` or ``g:<grant id>@<n>``), or ``None`` for anything else."""
    import re

    m = re.fullmatch(r"(?:uid:|g:[^@\s]+@)([0-9]{1,10})", value) if isinstance(value, str) else None
    return int(m.group(1)) if m else None


def principal(uid: int, verified: Verified) -> str:
    """§0.5, F4: ``g:<grant id>@<uid>`` only for a verified grant; a grant id alone never makes a principal. Raises
    for a relay peer (``uid = -1``, §11A.4): its principal is ``r:<device_id>``, from its identity alone."""
    if type(uid) is not int or uid < 0:
        raise ValueError("a relay peer has no UID principal")
    return f"g:{verified.grant.id}@{uid}" if verified.grant is not None else f"uid:{uid}"


class StatusBackend:
    """H2a's only workspace op: ``host.status`` (§0.8). Class N gets only ``version`` and ``tier``."""

    caps = ["ops/1"]
    ops = {"host.status": 1}

    def __init__(self, started: Callable[[], float] | None = None) -> None:
        self.started = started

    def kind(self, op: str) -> tuple[OpKind, Phase] | None:
        return ("read", "read") if op == "host.status" else None

    async def run(self, call: Call) -> dict[str, Any]:
        from orch.cli.main import VERSION_LINE

        data: dict[str, Any] = {"version": VERSION_LINE[:64], "tier": call.service.entry.tier}
        if call.column.name in {"P", "G"}:
            data["state"] = call.service.state
            if self.started is not None:
                data["uptime_s"] = int(time.monotonic() - self.started())
        return ok_result(data)


class WorkspaceGate:
    """The transport :class:`~orch.host.transport.Handler` of one workspace socket (§0.6 steps 3 to 7, then work)."""

    def __init__(self, host: Host, workspace_id: str, backend: OpBackend) -> None:
        self.host, self.workspace_id, self.backend = host, workspace_id, backend

    def __call__(self, session: Session, frame: dict[str, Any], request: frames.Request) -> Any:
        op = frame["op"]

        def refuse(error: HostError) -> None:
            session.respond(request, error.response(request.id))

        kind = self.backend.kind(op)
        if kind is None:  # step 3
            refuse(HostError("unknown_command", f"{op} is not served"))
            return None
        served = self.backend.ops.get(op, 1)
        wanted = ((session.hello or {}).get("ops") or {}).get(op, 1)
        if wanted != served:
            refuse(HostError("op.unsupported", f"{op} is served at version {served}, not {wanted}"))
            return None
        if self.host.draining:  # step 4
            refuse(self.host.restarting())
            return None
        if secret_in_request(frame):  # step 5
            refuse(HostError("grant.secret_in_args", "a grant secret goes in auth.grant only; nothing was run"))
            return None
        code = self._early(session.peer, frame, kind[0])  # steps 5 and 6 from the snapshot
        if code is not None:
            refuse(HostError(code, f"{op} is not allowed here"))
            return None
        if not session.admit(request):  # step 7
            return None
        return self._work(session, frame, request, *kind)

    def _early(self, peer: Peer, frame: dict[str, Any], kind: OpKind) -> str | None:
        """The column's refusal from the snapshot, or ``None`` (allowed, or the snapshot cannot say: stale)."""
        service = self.host.services.get(self.workspace_id)
        auth = service.auth if service is not None else None
        if service is None or auth is None or service.closing or not auth.fresh(service.entry.store_dir):
            return None
        now = service.clock()
        verified = auth.verify((frame.get("auth") or {}).get("grant"), now)
        mapped = self.host.registry.mapped_person(peer.uid, self.workspace_id) if peer.kind == "person" else None
        valid = mapped is not None and not self.host.pins.pinned(peer.uid) and auth.person_ok(mapped, now)
        return refusal(resolve(peer.kind, verified.state, valid), kind)

    def _column(self, service: WorkspaceService, peer: Peer, frame: dict[str, Any]) -> tuple[Column, str | None, Any]:
        """The authoritative column, viewer and grant, from the freshly refreshed store (worker thread, F8)."""
        verified = service.verify_grant((frame.get("auth") or {}).get("grant"))
        mapped = self.host.registry.mapped_person(peer.uid, self.workspace_id) if peer.kind == "person" else None
        now = service.clock()
        auth = service.auth
        valid = mapped is not None and not self.host.pins.pinned(peer.uid) and auth is not None
        valid = valid and auth.person_ok(mapped, now)  # type: ignore[union-attr]
        column = resolve(peer.kind, verified.state, valid)
        viewer = verified.person if column.name == "G" else (mapped if column.name == "P" else None)
        return column, viewer, verified if column.name == "G" else Verified(verified.state)

    async def _work(self, session: Session, frame: dict[str, Any], request: frames.Request, kind: Any, phase: Any):
        try:
            service = self.host.services.get(self.workspace_id)
            if service is None or service.state == "unavailable" or service.closing:
                cause = (
                    "not registered"
                    if service is None
                    else ("being unregistered" if service.closing else service.cause)
                )
                raise HostError("workspace.unavailable", f"the workspace cannot be served: {cause}")
            deferred = bool(getattr(self.backend, "state_checks", False))
            if deferred:  # the backend authorizes inside its own worker job (Call.authorize)
                column, viewer, verified = Column("N", "no-person"), None, Verified("absent")
            else:
                column, viewer, verified = await service.call(self._column, service, session.peer, frame)
                code = refusal(column, kind)
                if code is not None:
                    raise HostError(code, f"{frame['op']} is not allowed here")
                if service.state == "unavailable":  # an H2a backend: step 10 here
                    raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
                if phase == "write" and service.state == "degraded":
                    raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
            if phase == "wait" and self.host.draining:
                raise self.host.restarting()  # admitted before the drain, but a wait never parks during one
            if not session.state.start(request, phase):
                return
            call = Call(
                service, session, session.peer, frame, column, viewer, verified, principal(session.peer.uid, verified)
            )
            call.request = request
            if deferred:
                call.columns, call.kind = (lambda: self._column(service, session.peer, frame)), kind
            result = await self.backend.run(call)
            session.respond(request, {"t": "res", "id": request.id, "ok": True, "result": result})
        except HostError as exc:
            session.respond(request, exc.response(request.id))
        finally:
            bus = getattr(self.host, "bus", None)
            if bus is not None:  # evts held back for this answer (a write's own events, a new subscription) may go
                bus.kick()
