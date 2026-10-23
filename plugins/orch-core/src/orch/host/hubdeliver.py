"""The host side of ``event_deliver`` (hub contract 8.7, ticket-format 15.5): a custodial event the hub delivers is
verified, judged by the model at its new position and appended, together with the ledger pins it needs.

:class:`StoreLanding` is :class:`orch.hubclient.custodial.Landing` over the workspace's :class:`~orch.store.Store`.
Everything runs under the store lock. ``land`` judges the pins and then the event on a scratch state with
``model.preview`` (so a refused event pins nothing), and only then appends them in the same lock hold. The store
commits one line at a time: a crash between the pins and the event leaves valid pins and no event, which is harmless
(the hub resends, the dedup finds nothing, the event lands then). :func:`pump` takes one delivery from the link and
answers it: ``{}`` or the reply code, which the hub reads as final or retryable (:data:`custodial.RETRYABLE`)."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .workspace import WorkspaceService

_log = logging.getLogger("orch.host.hubdeliver")
WORKSPACE = "workspace"


class _Heads(Sequence):
    """``heads[seq - 1]`` of a log, read from the store on demand (a long log is never copied)."""

    def __init__(self, store: Any, log: str) -> None:
        self._store, self._log = store, log

    def __len__(self) -> int:
        return self._store.log_seq(self._log) or 0

    def __getitem__(self, i: Any) -> str:
        if type(i) is not int or not 0 <= i < len(self):
            raise IndexError(i)
        head = self._store.head_at(self._log, i + 1)
        if head is None:
            raise IndexError(i)
        return head


class StoreLanding:
    """:class:`~orch.hubclient.custodial.Landing` over a workspace service's store."""

    def __init__(
        self, service: WorkspaceService, fetch: Callable[[Any], bool] | None = None, state: Any = None
    ) -> None:
        self.service = service
        self.store = service.store
        self._fetch = fetch if fetch is not None else (lambda st: _fetch_snapshot(service, st))
        self._state = state  # the HubState being served (refreshed in place by a snapshot fetch)

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        with self.store.locked():
            yield

    def core(self) -> Any:
        return self.store.state._core  # noqa: SLF001 - the model's own state, read under the lock

    def logged(self, log: str, event_id: str) -> Mapping[str, Any] | None:
        if log != WORKSPACE and self.store.uid_of(log) is None:
            return None
        return self.store.logged_event(log, event_id)

    def refresh_snapshot(self) -> bool:
        return bool(self._fetch(self._state))

    def last_snapshot_pin_ms(self, hub_id: str) -> int | None:
        from orch.model.types import ts

        last = None
        for line, _head in self.store.events_after(WORKSPACE, 0, 1 << 30):
            if (
                line["type"] == "hub.pinned"
                and line["hub_id"] == hub_id
                and line["object"]["o"]["kind"] == "hub.snapshot"
            ):
                last = ts(line["at"]) * 1000
        return last

    def shared_items(self, log: str) -> tuple[Mapping[str, Any], frozenset[str]]:
        """The live shared items (C6 ``ShareState.sent``) of THIS host as the item check reads them. An entry counts
        only if it carries the ``chain`` and ``f1`` the item was built with (C6b, core#1035: the values the person was
        shown); an older entry is not live for a decision and the delivery is ``not_granted`` (fail closed)."""
        from . import shareops

        state = shareops._load(self.service)  # noqa: SLF001
        live: dict[str, Any] = {}
        old: set[str] = set()
        for lg, entry in state.sent.items():
            if lg in state.withdrawn:  # revoked by the owner: not live for a decision now, before the hub acknowledges
                continue
            h = entry.get("item_hash")
            if type(h) is not str or entry.get("digest") is None:
                continue
            if type(entry.get("chain")) is dict and type(entry.get("f1")) is dict:
                live[h] = {"chain": entry["chain"], "f1": entry["f1"]}
            old.update(x for x in entry.get("superseded", ()) if type(x) is str)
        return live, frozenset(old - set(live))

    def log_heads(self, log: str) -> Sequence[str]:
        return _Heads(self.store, log)

    def land(self, pins: list[dict[str, Any]], event: dict[str, Any], log: str) -> None:
        from orch import canon
        from orch.hubclient.custodial import LandRefused
        from orch.identity import new_ulid
        from orch.model import Refusal, preview

        store = self.store
        state = store.state
        ws_stamp = store.peek_stamp(WORKSPACE)
        seq, prev = ws_stamp["seq"], ws_stamp["prev"]
        stamp = store.peek_stamp(log)
        at = stamp["at"]
        probes = []
        for pin in pins:
            probe = {"v": 2, "hash_v": 1, "id": new_ulid(), "based_on": prev, **pin, "seq": seq, "prev": prev, "at": at}
            probes.append((WORKSPACE, probe))
            seq, prev = seq + 1, canon.event_head(probe)
        if log == WORKSPACE:
            probes.append((log, {**event, "seq": seq, "prev": prev, "at": at}))
        else:
            ev_stamp = {**stamp, "ws_seq": stamp["ws_seq"] + len(pins)}
            probes.append((log, {**event, **ev_stamp}))
        for where, probe in probes:  # nothing is written until every one of them admits
            r = preview(state, probe, log=where)
            if isinstance(r, Refusal):
                raise LandRefused(r)
            state = r
        old_check = store.append_check

        def check(
            lg: str, ev: dict[str, Any]
        ) -> None:  # the host's own append seam (dispatch.deny_append): this event only
            if (lg, ev.get("id")) != (log, event["id"]):
                from orch.store import StoreError

                raise StoreError("internal", f"{ev.get('type')}: an append the delivery does not list")

        store.append_check = check
        try:
            for pin in pins:  # host events: the store does not ask the check for them
                store.append(dict(pin), log=WORKSPACE)
            store.append(dict(event), log=log)
        finally:
            store.append_check = old_check


def _fetch_snapshot(service: Any, state: Any) -> bool:
    from orch.hubclient import snapshot

    from . import hubops

    wsk_pub, sign = hubops._wsk_sign(service)  # noqa: SLF001
    dev = hubops.workspace_dev(service)
    link, transport = hubops._snapshot_link(service, state, wsk_pub, sign, dev)  # noqa: SLF001
    try:
        return snapshot.refresh(
            state=state, link=link, workspace_id=service.workspace_id, dev_mode=dev,
            now_ms=lambda: int(service.clock() * 1000),
            save=lambda fetched: hubops._save_snapshot(service, fetched),  # noqa: SLF001
        )  # fmt: skip
    finally:
        getattr(transport, "close", lambda: None)()


def deliver(service: WorkspaceService, state: Any, log: str, event: dict[str, Any], *, fetch: Any = None) -> str | None:
    """Admit one delivered event; returns ``None`` (done, also for a dedup hit) or the reply code. Never raises."""
    from orch.hubclient import HubRefused
    from orch.hubclient.custodial import accept_event_deliver
    from orch.store import StoreError

    from . import hubops

    try:
        accept_event_deliver(
            state, {"log": log, "event": event}, land=StoreLanding(service, fetch, state),
            now_ms=int(service.clock() * 1000), dev_mode=hubops.workspace_dev(service),
        )  # fmt: skip
        return None
    except HubRefused as exc:
        return exc.code
    except StoreError as exc:
        _log.warning("event_deliver: the store refused: %s (%s)", exc.code, exc.detail)
        return "unavailable" if exc.code.startswith("store.") else "internal"
    except Exception:  # noqa: BLE001 - one bad delivery never stops the loop; it is answered
        _log.exception("event_deliver failed")
        return "internal"


def pump(service: WorkspaceService, link: Any, state: Any, wait: float = 0.0) -> bool:
    """Take one ``event_deliver`` off ``link`` (if any), admit it and acknowledge it. ``True`` when one was handled."""
    got = link.next_event_deliver(wait)
    if got is None:
        return False
    log, event = got
    # the store belongs to the workspace's worker thread (workspace.py): admission runs there
    code = service.submit(deliver, service, state, log, event).result(timeout=120)
    link.ack_event_deliver(code)
    return True
