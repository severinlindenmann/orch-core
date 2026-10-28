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
        #: C10: the secret of a hub-requested grant that a pending local hand-off matched, with the runner plan. It
        #: lives here from the append until ``deliver`` gives it to the runner (or drops it); never logged or stored.
        self.deposits: list[tuple[dict[str, Any], str]] = []
        #: FB15: ``(hand-off request record, the host-actor addon.granted that was appended)`` of an add-on the hub's
        #: ``addon.requested`` turned on; ``deliver`` finishes the install and the request after the lock is released.
        self.granted: list[tuple[dict[str, Any], dict[str, Any]]] = []

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
        answer = None
        if log == WORKSPACE and event.get("type") == "grant.requested":
            answer = self._answer(state, event, at, seq, probes[-1][1])
            if answer is not None:  # the host-actor grant.issued in the same lock hold; judged like every other line
                r = preview(state, answer[0], log=WORKSPACE)
                if isinstance(r, Refusal):
                    raise LandRefused(r)
                state = r
        addon = None
        if log == WORKSPACE and event.get("type") == "addon.requested":
            addon = self._answer_addon(state, event, at, seq, probes[-1][1])
            if addon is not None:  # the host-actor addon.granted in the same lock hold; judged like every other line
                r = preview(state, addon[0], log=WORKSPACE)
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
            written = store.append(dict(event), log=log).event
            if answer is not None:
                # issued_at is the request line's own ``at`` (the store stamps at append, so a clock tick since the
                # preview cannot make it differ): min(request.at, now) = request.at, and expires_at follows
                grant = {k: v for k, v in answer[0].items() if k not in ("seq", "prev", "at", "based_on")}
                grant["issued_at"] = written["at"]
                grant["expires_at"] = _plus_hours(written["at"], grant["hours"])
                store.append(grant, log=WORKSPACE)
                if answer[2] is not None:  # a pending local hand-off matched: the runner gets the secret, only it
                    self.deposits.append((answer[2], answer[1]))
            if addon is not None:
                grant = {k: v for k, v in addon[0].items() if k not in ("seq", "prev", "at", "based_on")}
                self.granted.append((addon[1], store.append(grant, log=WORKSPACE).event))
        finally:
            store.append_check = old_check

    def _answer_addon(
        self, state: Any, event: dict[str, Any], at: str, seq: int, request: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """The host-actor ``addon.granted`` that answers an admitted ``addon.requested`` (hub protocol 8.8b, FB15), or
        ``None``: the request then stays in the log and turns nothing on. It is answered only when the prepared
        hand-off request it names (``handoff``, made by the owner at this host: ``human.prepare {handoff}`` of
        ``addon.grant``) is of the same canonical person, still open on the host's clock, and carries exactly the
        requested terms. The declarations come from that request's event (the package the host staged and re-checks
        under this lock: :func:`marketinstall.recheck`), never from the hub. Returns ``(event with seq/prev/at/based_on,
        the request record)``."""
        from orch import canon
        from orch.hubclient.custodial import LandRefused
        from orch.identity import new_ulid
        from orch.model import Code, Refusal

        from . import hubhandoff, marketinstall

        ws = state._core.ws  # noqa: SLF001 - the model's own state, read under the lock
        req = ws.addon_requests.get(event["id"])
        if req is not None and any(
            o is not req and o["handoff"] == req["handoff"] and o["used"] for o in ws.addon_requests.values()
        ):
            return None  # this hand-off was answered by an earlier request (the log says so, under this lock)
        if req is None:  # the model admitted the request and recorded it; anything else is a bug: refuse, write nothing
            raise LandRefused(Refusal(Code.GRANT_TERMS, "the add-on request was not recorded"))
        requests = self.service.requests
        try:
            record = requests.read(req["handoff"]) if requests is not None else None
        except Exception:  # noqa: BLE001 - an unreadable record is no hand-off
            record = None
        if (
            record is None
            or record.get("op") != "addon.grant"
            or record.get("state") != "prepared"
            or record.get("hub_handoff") is not True
            or record.get("submit")
            or record.get("workspace_id") != self.service.workspace_id
            or not _same_asker(ws, record, req["person"])
            or requests.lapsed(record)
        ):
            return None  # no live local hand-off for these terms: the request is inert
        ev = hubhandoff.prepared_event(record)
        keys = ("name", "version", "package_sha256", "capabilities", "fields", "sections", "artifact_kinds", "runtime")
        body = {k: ev[k] for k in keys if k in ev}
        terms = ("name", "version", "package_sha256", "capabilities", "runtime")
        if ev.get("type") != "addon.granted" or any(body.get(k) != req[k] for k in terms):
            return None
        grant = {
            "v": 2,
            "hash_v": 1,
            "id": new_ulid(),
            "type": "addon.granted",
            "actor": {"kind": "host"},
            **body,
            "request": event["id"],
        }
        try:
            marketinstall.recheck(self.service, record, [grant])  # the staged bytes as they are now (C2), before append
        except Exception as exc:  # noqa: BLE001 - a changed stage or a revoked package: refuse, nothing is written
            why = str(getattr(exc, "code", "the staged package changed"))
            raise LandRefused(Refusal(Code.GRANT_TERMS, why)) from None
        prev = canon.event_head(request)
        return {**grant, "based_on": prev, "seq": seq + 1, "prev": prev, "at": at}, record

    def _answer(
        self, state: Any, event: dict[str, Any], at: str, seq: int, request: dict[str, Any]
    ) -> tuple[dict[str, Any], str, dict[str, Any] | None] | None:
        """The host-actor ``grant.issued`` that answers an admitted ``grant.requested`` (hub protocol 8.8, C10): the
        same terms, ``request`` = the request line's id, ``issued_at`` = ``min(request.at, now)`` (here both are
        ``at``), a fresh ``secret_hash``. Returns ``(event with seq/prev/at, secret, runner plan or None)``.

        The secret goes to the runner only if a pending local hand-off of kind ``grant`` for this canonical person,
        hub binding and exact terms is live (consumed here, inside the lock). Without one, or without a connected
        runner on a switched-on machine, the secret is destroyed when this returns: the grant is inert."""
        from orch import canon
        from orch.hubclient.custodial import LandRefused
        from orch.identity import new_grant, new_ulid
        from orch.model import Code, Refusal
        from orch.model.types import ts

        from . import grantoff

        ws = state._core.ws  # noqa: SLF001 - the model's own state, read under the lock
        req = ws.requests.get(event["id"])
        if req is None:  # the model admitted the request and recorded it; anything else is a bug: refuse, write nothing
            raise LandRefused(Refusal(Code.GRANT_TERMS, "the request was not recorded"))
        _check_verbs(req["verbs"])
        token, secret, secret_hash = new_grant(ts(at) * 1000)
        issued = {
            "v": 2,
            "hash_v": 1,
            "id": new_ulid(),
            "type": "grant.issued",
            "actor": {"kind": "host"},
            "grant": token.grant_id,
            "scope": req["scope"],
            "verbs": req["verbs"],
            "issued_at": at,
            "hours": req["hours"],
            "expires_at": _plus_hours(at, req["hours"]),
            "secret_hash": secret_hash,
            "request": event["id"],
            **({"label": req["label"]} if req["label"] is not None else {}),
        }
        prev = canon.event_head(request)
        probe = {**issued, "based_on": prev, "seq": seq + 1, "prev": prev, "at": at}
        plan = None
        runners = getattr(self.service, "runners", None)
        want = grantoff.terms(req["scope"], req["verbs"], req["hours"], req["label"])
        if runners is not None and _runner_ready(self.service, runners):
            model = dict(ws.hub)
            if grantoff.GrantHandoffs(self.service).take(
                model, person=req["person"], hub_id=req["hub_id"], handoff=req["handoff"], want=want
            ):
                plan = {
                    "deposit_id": new_ulid(),
                    "grant": token.grant_id,
                    "person": req["person"],
                    "until": issued["expires_at"],
                    "scope": req["scope"],
                }
        return probe, secret, plan


def _same_asker(ws: Any, record: dict[str, Any], signer: str) -> bool:
    """Is the hub signer of an ``addon.requested`` the person who prepared the hand-off here? An aliased hub user is
    that member (canonical ``p_`` id). A hub user the owner never aliased (a console-link join) is ``u_...``: it is
    accepted only when it is THE hub user the hub named as this workspace's owner when the hand-off was made
    (``hub_user``, hub ``owner_get``) and the hand-off was prepared by a workspace owner of this host. Any other hub
    user, admin or not, is refused."""
    from orch.model import identity

    local = record.get("person")
    if type(local) is not str:
        return False
    if local == signer:
        return True
    hub_user = record.get("hub_user")
    return (
        signer.startswith("u_")
        and type(hub_user) is str
        and signer == "u_" + hub_user
        and identity.role_of(ws, local) == "owner"
    )


def _plus_hours(stamp: str, hours: int) -> str:
    from datetime import UTC, datetime

    from orch.model.types import ts

    return datetime.fromtimestamp(ts(stamp) + 3600 * hours, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_verbs(verbs: Any) -> None:
    """A requested list names exact operations an agent may run (as ``orch grant --verbs``): refuse the request
    otherwise, so no grant is made that covers nothing the requester meant."""
    from orch.hubclient.custodial import LandRefused
    from orch.model import Code, Refusal

    if verbs == "agent":
        return
    import orch.ops as ops

    known = {n for n in ops.names() if ops.get(n).who in ("agent", "unattended")}
    if any(v not in known for v in verbs):
        raise LandRefused(Refusal(Code.GRANT_VERB, "verbs must be operations an agent may run"))


def _runner_ready(service: Any, runners: Any) -> bool:
    """Can the host give a secret to its runner now? Connected, and dashboard execution switched on for this machine."""
    from orch.runner import enablement

    try:
        return (
            bool(runners.connected)
            and enablement.state_of(runners.host.state_dir, service.workspace_id, runners.host.host_uid)["enabled"]
        )
    except Exception:  # noqa: BLE001 - any doubt: no secret leaves
        return False


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

    landing = StoreLanding(service, fetch, state)
    try:
        accept_event_deliver(
            state, {"log": log, "event": event}, land=landing,
            now_ms=int(service.clock() * 1000), dev_mode=hubops.workspace_dev(service),
        )  # fmt: skip
        _warn_policy(event)
        return None
    except HubRefused as exc:
        _log.warning("event_deliver %s refused: %s", event.get("type"), exc.code)
        return exc.code
    except StoreError as exc:
        _log.warning("event_deliver: the store refused: %s (%s)", exc.code, exc.detail)
        return "unavailable" if exc.code.startswith("store.") else "internal"
    except Exception:  # noqa: BLE001 - one bad delivery never stops the loop; it is answered
        _log.exception("event_deliver failed")
        return "internal"
    finally:
        _to_runner(service, landing)
        _finish_addons(service, landing)


def _to_runner(service: WorkspaceService, landing: StoreLanding) -> None:
    """Give the secret of a hub-requested grant to the runner (C10), once, and forget it. Outside the store lock; the
    secret is in no log line and no error. A failure leaves an inert grant (nobody holds the secret)."""
    import asyncio

    deposits, landing.deposits = landing.deposits, []
    runners = getattr(service, "runners", None)
    loop = getattr(getattr(runners, "host", None), "loop", None)
    for plan, secret in deposits:
        if runners is None or loop is None:
            continue
        fut = asyncio.run_coroutine_threadsafe(runners.grants.deposit(service.workspace_id, plan, secret), loop)
        fut.add_done_callback(
            lambda f, g=plan["grant"]: _log.info("hub-requested grant %s: deposit %s", g, _outcome(f))
        )


def _finish_addons(service: WorkspaceService, landing: StoreLanding) -> None:
    """FB15: after an ``addon.granted`` the hub's request caused: put the granted bytes in place (idempotent, by digest,
    as ``human.finish`` does) and end the prepared request ``submitted``. Outside the store lock; failures are logged,
    never raised (the registry fails closed on a directory whose digest is not the grant's; start-up finishes it)."""
    from . import marketinstall
    from .workspace import ok_result

    granted, landing.granted = landing.granted, []
    for record, event in granted:
        try:
            marketinstall.after_grant(service, record, [event])
            template = record.get("result") or {}
            result = ok_result(template.get("data") or {}, seq=int(event.get("seq") or 0))
            result["key"] = template.get("key")
            result["hints"] = list(template.get("hints") or [])[:8]
            result["lines"] = [str(line) for line in template.get("lines") or []][:10000]
            outcome = {"state": "committed", "result": result, "exit": 0}
            service.requests.write({**record, "state": "submitted", "done_at": service.clock(), "outcome": outcome})
        except Exception:  # noqa: BLE001 - one bad finish never stops the loop
            _log.exception("add-on request: finishing the install failed")


def _outcome(fut: Any) -> str:
    try:
        return str(fut.result().get("deposit", "unknown"))
    except Exception as exc:  # noqa: BLE001 - never the exception text: it could carry a request
        return type(exc).__name__


def _warn_policy(event: dict[str, Any]) -> None:
    """P2-d: a policy that turns a gate off is admitted with a warning (the contract has no wire field for it yet: the
    host log carries it)."""
    if event.get("type") != "policy.changed":
        return
    for gate, pol in sorted((event.get("gates") or {}).items()):
        if pol.get("applies") == "off":
            _log.warning("policy.changed turns off the %s gate", gate)


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
