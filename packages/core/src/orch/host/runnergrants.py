"""The runner grant on the host (D-P2-27, §12.4.2; H11c): ``grant.deposit``, ``grant.deposit_status``, ``grant.forget``.

**Deposit.** ``grant.deposit {deposit_id}`` is the one op that needs both a person and a grant: a person-class caller
whose UID maps to a valid member (evaluated as P) **and** a verified ``auth.grant`` naming a grant of that same person
whose ``grant.issued`` carries ``label:"runner"``, ``verbs:"agent"``, at most the workspace grant_hours (168) and
at least 15 minutes left.
It is sent by the process that drew the secret (the click signer in deposit-only grant mode, ``orch grant --runner``,
the CLI signer "hand to the runner"). The host forwards the secret **once** to the runner (``grant.put``), keeps no copy
(the request's :class:`~orch.host.auth.Redactor` removes it from every answer), and records only the outcome per
``deposit_id``: ``confirmed`` (the runner holds it), ``rejected`` (the runner refused it) or ``unknown`` (no answer:
``grant.deposit_status`` reports it, and the next ``runner.hello`` settles it). A forgotten or replaced runner grant id
is never deposited again (``grant.forgotten``); the dashboard's machine switch must be on and the runner connected.

**One per (workspace, person).** A confirmed deposit replaces that person's previous runner grant for new launches
(the runner holds one; the old id is marked forgotten, running agents keep their value). ``grant.forget {grant}``
(P, own grants) marks an id forgotten and tells the runner to drop it.

**Revalidation.** At every ``runner.hello`` the host compares the runner's reported grants with this table: a grant
that is not this person's current runner grant, forgotten, revoked, expired or whose person may no longer run agents
is dropped (``grant.drop``); a current grant the runner no longer holds is cleared (a runner restart loses every
grant). While any runner grant or any live agent run exists, the watcher checks them every second against the
authentication snapshot: a revoked or expired grant, or a removed member, is dropped at the runner, and the runs that
carry it get ``grant_state:"ended"`` (the runner does not kill them; the host refuses their writes).

The table: ``<state>/hosts/<ws>/runner-grants.json`` (0600 in 0700): ``{"v":1, "current":{person:{grant, until,
scope, deposit_id}}, "deposits":{deposit_id:{grant, person, outcome, reason, at}}, "forgotten":{grant: until}}``.
Never a secret.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from orch.model.types import GRANT_MAX_HOURS

from .auth import Redactor
from .custody import ensure_private_dir, read_json, write_private
from .errors import HostError

if TYPE_CHECKING:
    from .runners import RunnerHub
    from .workspace import Call

__all__ = ["GRANT_OPS", "MAX_HOURS", "MIN_LEFT", "RunnerGrants", "complete", "serve"]

LOG = logging.getLogger(__name__)

GRANT_OPS: dict[str, int] = {"grant.deposit": 1, "grant.deposit_status": 1, "grant.forget": 1}
#: a runner grant needs this much time left to be deposited (§12.4.2) or used for a launch
MIN_LEFT = 15 * 60
#: a runner grant lasts at most the workspace ``grant_hours`` setting, never more than this (D-P2-27, B8: was a fixed
#: 12 hours). The model accepts an owner's longer grant regardless; this cap is host admission only
MAX_HOURS = GRANT_MAX_HOURS
FILE = "runner-grants.json"
KEEP_DEPOSITS = 256
WATCH_SECONDS = 1.0
#: grant.deposit per person (Opus L5): 10 a minute, burst 5 (each one is a runner round trip, an fsync, an audit line)
DEPOSIT_RATE, DEPOSIT_BURST = 10 / 60, 5.0
_ULID = {"type": "string", "pattern": r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"}
_GRANT = {"type": "string", "pattern": r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$"}
_SCHEMAS = {
    "grant.deposit": {"type": "object", "properties": {"deposit_id": _ULID}, "required": ["deposit_id"],
                      "additionalProperties": False},
    "grant.deposit_status": {"type": "object", "properties": {"deposit_id": _ULID}, "required": ["deposit_id"],
                             "additionalProperties": False},
    "grant.forget": {"type": "object", "properties": {"grant": _GRANT}, "required": ["grant"],
                     "additionalProperties": False},
}  # fmt: skip
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}")


def _epoch(stamp: Any) -> float | None:
    if not isinstance(stamp, str) or not _TIME.match(stamp):
        return None
    return float(calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")))


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _validate(op: str, args: Any) -> None:
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")


# -- the table ---------------------------------------------------------------------------------------------------------


class GrantTable:
    """One workspace's runner-grant table (on the event loop; saved after every change)."""

    def __init__(self, state_dir: Path, uid: int, workspace_id: str, clock: Any = time.time) -> None:
        self.path = Path(state_dir) / "hosts" / workspace_id / FILE
        self.clock = clock
        self.state_dir, self.uid, self.workspace_id = Path(state_dir), uid, workspace_id
        self.current: dict[str, dict[str, Any]] = {}
        self.deposits: dict[str, dict[str, Any]] = {}
        self.forgotten: dict[str, str] = {}
        try:
            doc = read_json(self.path, uid)
        except HostError as exc:  # unreadable: start empty, and nothing may be deposited until it is fixed
            LOG.error("runner grants of %s unreadable: %s", workspace_id, exc.message)
            self.broken = True
            return
        self.broken = False
        if isinstance(doc, dict) and doc.get("v") == 1:
            self.current = {p: c for p, c in (doc.get("current") or {}).items() if isinstance(c, dict)}
            self.deposits = {d: r for d, r in (doc.get("deposits") or {}).items() if isinstance(r, dict)}
            self.forgotten = {g: str(u) for g, u in (doc.get("forgotten") or {}).items()}

    def save(self) -> None:
        if self.broken:
            raise HostError("custody.unsafe_mode", "the runner grant table cannot be read; fix or remove it first")
        now = self.clock()
        self.forgotten = {
            g: u for g, u in self.forgotten.items() if (_epoch(u) or now + 1) > now - 24 * 3600
        }  # an id past its expiry can never be deposited anyway
        if len(self.deposits) > KEEP_DEPOSITS:  # settled ones only: a pending deposit is never pruned (Opus L5)
            settled = sorted(
                ((d, r) for d, r in self.deposits.items() if r.get("outcome") != "pending"),
                key=lambda kv: str(kv[1].get("at")),
            )
            drop = {d for d, _ in settled[: max(0, len(settled) - KEEP_DEPOSITS)]}
            self.deposits = {d: r for d, r in self.deposits.items() if d not in drop}
        ensure_private_dir(self.state_dir / "hosts" / self.workspace_id, self.uid)
        write_private(self.path, {"v": 1, "current": self.current, "deposits": self.deposits,
                                  "forgotten": self.forgotten})  # fmt: skip


class RunnerGrants:
    """Every workspace's table, the deposit and forget steps, the hello reconciliation and the watcher."""

    def __init__(self, hub: RunnerHub) -> None:
        self.hub = hub
        self.tables: dict[str, GrantTable] = {}
        self._watching: asyncio.Task[None] | None = None
        #: what the last hello reported, and which workspaces were served then: a workspace bound after it (a host that
        #: has just started answers the runner's hello before it serves its workspaces) is settled in
        #: :meth:`workspace_served`, never judged on a missing service (that dropped the runner's grant while the
        #: persisted table still read "valid": "the runner holds no such runner grant")
        self._hello: dict[str, Any] | None = None
        #: one deposit, forget or drop at a time per (workspace, person): a deposit in flight never undoes a forget
        #: or beats a newer replacement (#466 review, Opus M1)
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def lock(self, workspace_id: str, person: str) -> asyncio.Lock:
        return self._locks.setdefault((workspace_id, person), asyncio.Lock())

    def table(self, workspace_id: str) -> GrantTable:
        found = self.tables.get(workspace_id)
        if found is None:
            service = self.hub.host.services.get(workspace_id)
            clock = service.clock if service is not None else time.time
            found = self.tables[workspace_id] = GrantTable(self.hub.host.state_dir, self.hub.host.host_uid,
                                                           workspace_id, clock)  # fmt: skip
        return found

    def current(self, workspace_id: str, person: str) -> dict[str, Any] | None:
        """The person's runner grant as the runner holds it now (``None`` without a connected runner)."""
        if not self.hub.connected:
            return None
        return self.table(workspace_id).current.get(person)

    def _audit(self, event: str, **fields: Any) -> None:
        self.hub._audit(event, **fields)

    # -- grant.deposit
    async def deposit(self, workspace_id: str, plan: dict[str, Any], secret: str) -> dict[str, Any]:
        return await self._shielded(workspace_id, plan["person"], self._deposit(workspace_id, plan, secret))

    async def _shielded(self, workspace_id: str, person: str, step: Any) -> Any:
        """Run ``step`` under the person's lock as its own task: a client that goes away cancels only its wait, never
        the step, so the lock is held until the runner answered (or the command timed out or its session ended)."""

        async def locked() -> Any:
            async with self.lock(workspace_id, person):
                return await step

        return await asyncio.shield(asyncio.get_running_loop().create_task(locked()))

    async def _deposit(self, workspace_id: str, plan: dict[str, Any], secret: str) -> dict[str, Any]:
        table = self.table(workspace_id)
        did, gid, person = plan["deposit_id"], plan["grant"], plan["person"]
        base = {"grant": gid, "until": plan["until"], "scope": plan["scope"]}
        seen = table.deposits.get(did)
        if seen is not None:
            if seen.get("grant") != gid or seen.get("person") != person:
                raise HostError("idem.conflict", "this deposit_id names another deposit")
            outcome = seen.get("outcome")
            if outcome != "pending":
                return {**base, "deposit": outcome, **({"reason": seen["reason"]} if seen.get("reason") else {})}
        if gid in table.forgotten:
            raise HostError("grant.forgotten", f"{gid} was forgotten or replaced on this machine; issue a new one")
        before = (table.current.get(person) or {}).get("grant")
        table.deposits[did] = {"grant": gid, "person": person, "outcome": "pending", "reason": None, "at": _iso(
            time.time())}  # fmt: skip
        table.save()  # durable before the secret leaves: a status query after a crash says unknown, never none
        try:
            res = await self.hub.command(
                "grant.put",
                {"deposit_id": did, "workspace_id": workspace_id, "person": person, "grant": gid, "secret": secret,
                 "until": plan["until"]},
            )  # fmt: skip
        except HostError as exc:
            if exc.code in ("outcome.unknown", "runner.unavailable"):
                self._audit("grant.deposit", workspace_id=workspace_id, grant=gid, person=person, deposit_id=did,
                            outcome="unknown")  # fmt: skip
                return {**base, "deposit": "unknown"}
            table.deposits[did].update(outcome="rejected", reason=exc.code)
            table.save()
            self._audit("grant.deposit", workspace_id=workspace_id, grant=gid, person=person, deposit_id=did,
                        outcome="rejected", reason=exc.code)  # fmt: skip
            return {**base, "deposit": "rejected", "reason": exc.code}
        if res.get("grant") != gid:
            return {**base, "deposit": "unknown"}
        now = (table.current.get(person) or {}).get("grant")
        if gid in table.forgotten or now not in (before, gid, None):
            # forgotten or replaced meanwhile (a hello settled another grant): never confirmed, dropped at the runner;
            # the runner's one slot for this person held ``gid`` last, so the other grant is gone there too
            table.deposits[did].update(outcome="rejected", reason="grant.forgotten")
            if now is not None and now != gid:
                self._forget_current(table, person, "replaced_meanwhile")
            table.save()
            self._audit("grant.deposit", workspace_id=workspace_id, grant=gid, person=person, deposit_id=did,
                        outcome="rejected", reason="grant.forgotten")  # fmt: skip
            await self._drop(gid)
            return {**base, "deposit": "rejected", "reason": "grant.forgotten"}
        self._approve(table, person, gid, plan["until"], plan["scope"], did)
        return {**base, "deposit": "confirmed"}

    def _forget_current(self, table: GrantTable, person: str, reason: str) -> str | None:
        """Clear the person's current grant and mark it forgotten (a dropped grant never comes back)."""
        cur = table.current.pop(person, None)
        if cur is None:
            return None
        table.forgotten[cur["grant"]] = str(cur.get("until") or "")
        self._audit("grant.drop", workspace_id=table.workspace_id, grant=cur["grant"], person=person, reason=reason)
        self.hub.publish_grant(table.workspace_id, person)
        return str(cur["grant"])

    def _approve(self, table: GrantTable, person: str, gid: str, until: str, scope: str, did: str) -> None:
        old = table.current.get(person)
        if old is not None and old.get("grant") != gid:
            table.forgotten[old["grant"]] = str(old.get("until") or until)
            self._audit("grant.replace", workspace_id=table.workspace_id, person=person, old=old["grant"], new=gid)
        table.current[person] = {"grant": gid, "until": until, "scope": scope, "deposit_id": did}
        if did in table.deposits:
            table.deposits[did].update(outcome="confirmed", reason=None)
        table.save()
        self.hub.publish_grant(table.workspace_id, person)
        self._audit("grant.deposit", workspace_id=table.workspace_id, grant=gid, person=person, deposit_id=did,
                    outcome="confirmed")  # fmt: skip
        self.watch()

    # -- grant.forget
    async def forget(self, workspace_id: str, person: str, gid: str, until: str) -> dict[str, Any]:
        return await self._shielded(workspace_id, person, self._forget(workspace_id, person, gid, until))

    async def _forget(self, workspace_id: str, person: str, gid: str, until: str) -> dict[str, Any]:
        table = self.table(workspace_id)
        cur = table.current.get(person)
        was = cur is not None and cur.get("grant") == gid
        table.forgotten[gid] = until
        if was:
            table.current.pop(person, None)
        table.save()
        if was:
            self.hub.publish_grant(workspace_id, person)
        self._audit("grant.forget", workspace_id=workspace_id, grant=gid, person=person, held=was)
        await self._drop(gid)
        return {"forgotten": was}

    async def _drop(self, gid: str) -> None:
        if not self.hub.connected:
            return  # the next hello reconciles: a forgotten id is dropped there
        with contextlib.suppress(HostError):
            await self.hub.command("grant.drop", {"grant": gid}, timeout=5.0)

    # -- runner.hello
    async def reconcile(self, reported: list[dict[str, Any]]) -> None:
        """After a hello: what the runner holds decides ``current`` and every pending deposit; anything the host no
        longer allows is dropped at the runner.

        No await happens before the tables change (the drops are sent after every change is made), so this never
        interleaves with a deposit's or a forget's bookkeeping; if an await is ever added before a change, take the
        per-person lock (:meth:`lock`) around it."""
        self._hello = {"reported": list(reported), "served": set(self.hub.host.services)}
        await self._settle(reported, None)

    async def workspace_served(self, wid: str) -> None:
        """A workspace was bound after the runner's hello: settle it by what the runner reported then (a deposit needs
        a served workspace, so nothing changed at the runner since)."""
        hello = self._hello
        if hello is None or wid in hello["served"] or self.hub.host.services.get(wid) is None:
            return
        hello["served"].add(wid)
        await self._settle([r for r in hello["reported"] if r["workspace_id"] == wid], wid)

    async def _settle(self, reported: list[dict[str, Any]], only: str | None) -> None:
        held = {(r["workspace_id"], r["person"]): r for r in reported}
        drops: list[str] = []
        for (wid, person), rep in held.items():
            service = self.hub.host.services.get(wid)
            if service is None:
                continue  # not served yet: workspace_served settles it
            table = self.table(wid) if service is not None else None
            if table is None or not self._allowed(service, table, person, rep["grant"]):
                drops.append(rep["grant"])
        # every served workspace's persisted table, loaded or not: after a host restart nothing is loaded yet, and a
        # persisted ``current`` the runner does not hold must not read as available (Codex P2 #2)
        for wid in sorted(set(self.tables) | {w for w, _ in held} | set(self.hub.host.services)):
            if self.hub.host.services.get(wid) is None or (only is not None and wid != only):
                continue
            table = self.table(wid)
            changed = False
            for did, dep in table.deposits.items():
                if dep.get("outcome") != "pending":
                    continue
                rep = held.get((wid, dep.get("person")))
                if rep is not None and rep["grant"] == dep.get("grant") and rep["grant"] not in drops:
                    cur = table.current.get(dep["person"])
                    if cur is None or cur.get("grant") != rep["grant"]:
                        self._approve(table, dep["person"], rep["grant"], rep["until"], self._scope(wid, rep["grant"]),
                                      did)  # fmt: skip
                    else:  # already current (a repeated deposit of it): this deposit settles too (Codex P2 #3)
                        dep.update(outcome="confirmed", reason=None)
                        changed = True
                else:
                    dep.update(outcome="rejected", reason="runner_restarted")
                    changed = True
            for person, cur in list(table.current.items()):
                rep = held.get((wid, person))
                if rep is None or rep["grant"] != cur.get("grant") or rep["grant"] in drops:
                    self._forget_current(table, person, "not_held")  # a dropped grant never comes back (item 7)
                    changed = True
            if changed:
                with contextlib.suppress(HostError):
                    table.save()
        for (wid, _person), rep in held.items():  # a reported grant the host no longer allows is forgotten too
            if rep["grant"] in drops and wid in self.hub.host.services:
                table = self.table(wid)
                if rep["grant"] not in table.forgotten:
                    table.forgotten[rep["grant"]] = str(rep.get("until") or "")
                    with contextlib.suppress(HostError):
                        table.save()
        for gid in drops:
            self._audit("grant.drop", grant=gid, reason="revalidated")
            await self._drop(gid)
        for wid in sorted(set(self.hub.host.services)):
            self.hub.publish_grant(wid)  # the runner (re)connected: every person's view may differ from before
        self.watch()

    def _scope(self, wid: str, gid: str) -> str:
        service = self.hub.host.services.get(wid)
        auth = getattr(service, "auth", None)
        entry = auth.grants.get(gid) if auth is not None else None
        return str(entry.scope) if entry is not None else "workable"

    def _allowed(self, service: Any, table: GrantTable, person: str, gid: str) -> bool:
        if gid in table.forgotten:
            return False
        cur = table.current.get(person)
        pending = any(d.get("grant") == gid and d.get("person") == person and d.get("outcome") == "pending"
                      for d in table.deposits.values())  # fmt: skip
        if (cur is None or cur.get("grant") != gid) and not pending:
            return False
        return self.valid(service, person, gid) is None

    @staticmethod
    def valid(service: Any, person: str, gid: str, *, left: float = 0) -> str | None:
        """``None`` while the grant is a live grant of ``person``, else the ``launch.no_grant`` reason (``revoked``,
        ``expiring``)."""
        auth = getattr(service, "auth", None)
        if auth is None:
            return "revoked"
        entry = auth.grants.get(gid)
        now = service.clock()
        if entry is None or entry.person != person or entry.revoked or entry.expires_s is None:
            return "revoked"
        if not auth.person_ok(person, now) or auth.roles.get(person) in (None, "viewer"):
            return "revoked"
        if entry.expires_s <= now:
            return "revoked"
        if entry.expires_s - now < left:
            return "expiring"
        return None

    def launch_reason(self, service: Any, person: str, cur: dict[str, Any]) -> str | None:
        """The ONE rule for "may this grant start an agent now": ``None`` when it may, else the ``launch.no_grant``
        reason. A live grant of the person with at least :data:`MIN_LEFT` left (``revoked``, ``expiring``; a role
        change to viewer or a removed person counts as ``revoked``), and a ``scope: all`` grant never starts agents for
        a member (``scope``). The launch plan (agents.plan) and ``whoami.runner.grant.startable`` both use it."""
        reason = self.valid(service, person, cur["grant"], left=MIN_LEFT)
        auth = getattr(service, "auth", None)
        if reason is None and cur.get("scope") == "all" and auth is not None and auth.roles.get(person) == "member":
            reason = "scope"
        return reason

    # -- the watcher
    def watch(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._watching is None or self._watching.done():
            self._watching = loop.create_task(self._watch())

    def _busy(self) -> bool:
        if any(t.current for t in self.tables.values()):
            return True
        return any(_watched(r) for r in self.hub.store.records.values())

    async def _watch(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while self._busy():
                await asyncio.sleep(WATCH_SECONDS)
                try:
                    await self._pass()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the watcher outlives any one failure
                    LOG.warning("runner grant watcher: %s", type(exc).__name__)

    async def _pass(self) -> None:
        from orch.runner import enablement

        for wid, table in list(self.tables.items()):
            service = self.hub.host.services.get(wid)
            auth = getattr(service, "auth", None)
            if service is not None and auth is not None and not auth.fresh(service.entry.store_dir):
                await asyncio.wrap_future(service.submit(lambda s=service: (s.refresh(), s.sync(check=True))))
            # dashboard execution switched off: the runner gives up the workspace's grants too (fail closed)
            off = not enablement.state_of(self.hub.host.state_dir, wid, self.hub.host.host_uid)["enabled"]
            for person in list(table.current):
                async with self.lock(wid, person):  # never in the middle of a deposit or a forget
                    cur = table.current.get(person)
                    if cur is None:
                        continue
                    forgotten = cur["grant"] in table.forgotten  # defence in depth: a forgotten id is never current
                    if off or forgotten or service is None or self.valid(service, person, cur["grant"]) is not None:
                        self._forget_current(table, person, "switch_off" if off else "ended")  # never again (item 7)
                        with contextlib.suppress(HostError):
                            table.save()
                        await self._drop(cur["grant"])
        for rec in list(self.hub.store.records.values()):
            if not _watched(rec) or not rec.get("grant"):
                continue
            service = self.hub.host.services.get(rec.get("workspace_id"))
            if service is None or self.valid(service, rec["person"], rec["grant"]) is not None:
                await self.hub._save({**rec, "grant_state": "ended"})


def _watched(rec: dict[str, Any]) -> bool:
    """A live agent run whose grant still counts as ``ok``."""
    return rec.get("kind") == "agent" and rec.get("grant_state") == "ok" and rec.get("state") in ("starting", "running")


# -- the ops: the worker half -----------------------------------------------------------------------------------------


def serve(call: Call) -> dict[str, Any]:
    """On the worker thread: the column, the caller and the grant; the table work is :func:`complete`'s."""
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    _validate(op, args)
    opts = call.frame.get("opts") or {}
    if opts.get("dry_run"):
        raise HostError("invalid.input", f"dry_run is not supported by {op}")
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    hub = getattr(service, "runners", None)
    if hub is None:
        raise HostError("runner.unavailable", "this host serves no runner", retry_after_ms=1000)
    if op == "grant.deposit":
        return ok_result({"plan": _deposit_plan(call, hub, args["deposit_id"])})
    person = str(call.viewer)
    if op == "grant.deposit_status":
        return ok_result({"plan": {"deposit_id": args["deposit_id"], "person": person}})
    view = service.store.state.workspace.grants.get(args["grant"])
    if view is None or view.person != person:
        raise HostError("not_found", "no such grant of yours")  # another person's grant is not found (N5)
    return ok_result({"plan": {"grant": args["grant"], "person": person, "until": view.expires_at}})


def _deposit_plan(call: Call, hub: RunnerHub, deposit_id: str) -> dict[str, Any]:
    from orch.runner import enablement

    service = call.service
    grant = call.verified.grant if call.verified is not None else None
    if call.column.name != "G" or grant is None or call.peer.kind != "person":
        raise HostError("op.not_allowed", "grant.deposit needs a person's own terminal or signer and auth.grant")
    mapped = hub.host.registry.mapped_person(call.peer.uid, service.workspace_id)
    auth = service.auth
    now = service.clock()
    if mapped is None or hub.host.pins.pinned(call.peer.uid) or auth is None or not auth.person_ok(mapped, now):
        raise HostError("op.not_allowed", "grant.deposit: this UID is not a member who may run agents")
    if grant.person != mapped:
        raise HostError("op.not_allowed", "grant.deposit: only your own runner grant")
    view = service.store.state.workspace.grants.get(grant.id)
    if view is None or getattr(view, "label", None) != "runner":
        raise HostError("grant.not_runner", 'only a grant issued with label "runner" can be handed to the runner')
    if view.verbs != "agent":
        raise HostError("grant.verbs", 'a runner grant covers verbs "agent"')
    issued, expires = _epoch(view.issued_at), _epoch(view.expires_at)
    cap = min(service.store.state.workspace.settings["grant_hours"], MAX_HOURS)  # on the state read now (B8)
    if issued is None or expires is None or expires - issued > cap * 3600:
        raise HostError("op.not_allowed", f"a runner grant lasts at most {cap} hours (the workspace setting)")
    if expires - now < MIN_LEFT:
        raise HostError("grant.expiring", "the grant has less than 15 minutes left; issue a new one")
    if not enablement.state_of(hub.host.state_dir, service.workspace_id, hub.host.host_uid)["enabled"]:
        raise HostError("runner.disabled", "dashboard execution is off for this workspace on this machine "
                        "(missing: machine); turn it on in your terminal: orch runner enable",
                        detail={"missing": "machine"})  # fmt: skip
    if not hub.connected:
        raise HostError("runner.unavailable", "no runner is connected", retry_after_ms=1000)
    return {"deposit_id": deposit_id, "grant": grant.id, "person": mapped, "until": view.expires_at,
            "scope": str(view.scope)}  # fmt: skip


# -- the event-loop half --------------------------------------------------------------------------------------------


async def complete(call: Call, checked: dict[str, Any]) -> dict[str, Any]:
    from orch.runner import enablement

    from .workspace import ok_result

    op = call.frame["op"]
    plan = checked["data"]["plan"]
    hub: RunnerHub = call.service.runners
    grants = hub.grants
    wid = call.service.workspace_id
    redactor = Redactor((call.frame.get("auth") or {}).get("grant"))
    try:
        if op == "grant.deposit_status":
            dep = grants.table(wid).deposits.get(plan["deposit_id"])
            if dep is None or dep.get("person") != plan["person"]:
                return ok_result({"deposit": "none"})
            outcome = dep.get("outcome")
            reason = dep.get("reason") if outcome == "rejected" else None
            return ok_result({"deposit": "unknown" if outcome == "pending" else outcome,
                              **({"reason": reason} if reason else {})})  # fmt: skip
        if op == "grant.forget":
            return ok_result(await grants.forget(wid, plan["person"], plan["grant"], plan["until"]))
        if not enablement.state_of(hub.host.state_dir, wid, hub.host.host_uid)["enabled"]:  # again, at use
            raise HostError(
                "runner.disabled",
                "dashboard execution is off for this workspace (missing: machine)",
                detail={"missing": "machine"},
            )
        secret = (call.frame.get("auth") or {}).get("grant")
        if not isinstance(secret, str) or not secret.startswith(plan["grant"] + "."):
            raise HostError("op.not_allowed", "grant.deposit: auth.grant is not the planned grant")
        from .runners import _take

        if not _take(hub._deposits, plan["person"], DEPOSIT_RATE, DEPOSIT_BURST):  # after every refusal above
            raise HostError("rate.limited", "at most 10 grant deposits per minute", retry_after_ms=6000)
        return redactor.obj(ok_result(await grants.deposit(wid, plan, secret)))
    except HostError as exc:
        raise redactor.error(exc) from None
