"""Host timer for bounded person-signed schedules. Ledger occurrence keys are authoritative."""

import contextlib
import logging
from types import SimpleNamespace

from orch.identity import new_grant, new_ulid
from orch.model import claims, schedules, schedules_wire
from orch.model import followups as rules
from orch.model.followups_wire import enabled, stamp
from orch.model.types import ts

from .followups import FollowupRunner
from .runners import LIVE

LOG = logging.getLogger(__name__)


class ScheduleRunner(FollowupRunner):
    """Reuse the timer, host ownership, append, planning and one-run deposit plumbing."""

    def __init__(self, service, *, clock, mono, tick_s=30):
        super().__init__(service, clock=clock, mono=mono, tick_s=tick_s)
        self.path = self.path.with_name("schedules.json")
        # Schedule occurrence keys in the ledger replace the follow-up intent cache.
        self.intents = {}

    def _read_schedules(self, now):
        service = self.service
        if service.store is None or service.state != "ok" or service.closing:
            return [], 0, 0
        store = service.store
        store.refresh()
        store.load_all()
        core = store.state._core
        if not self._owns(store, core):
            return [], 0, 0
        rows, today = [], 0
        # Add-on off or purged: nothing fires; an occurrence that comes due meanwhile is settled did_not_run
        # (addon_disabled), never backfilled when the add-on is on again.
        on = enabled(core.ws)
        for ticket in core.tickets.values():
            for rec in ticket.schedules.values():
                for occurrence, row in rec.occurrences.items():
                    if row.get("fired_at") and ts(row["fired_at"]) // 86400 == now // 86400:
                        today += 1
                    if row.get("grant") and not row.get("outcome"):
                        rows.append((ticket.uid, ticket.key, self._run(rec, occurrence, row), None))
                if rec.cancelled:
                    continue
                # Never an occurrence before the signing plus the minimum lead (a backdated `starts`).
                floor = schedules_wire.first_allowed(core.ws, rec) - 1
                after = max([floor, *(ts(o) for o in rec.occurrences)])
                due = schedules.occurrences(rec.rule, ts(rec.starts), ts(rec.ends), after)
                if due is not None and now >= due - (schedules_wire.early_s(core.ws) if on else 0):
                    # A live claim of the setter is `busy` in the ledger itself (blocked), whatever the runner
                    # reports for its session: only a lapsed claim is released by the firing.
                    reason = (
                        "addon_disabled"
                        if not on
                        else "missed"
                        if now > due + rules.FU_LATE_S or now >= ts(rec.ends)
                        else schedules_wire.blocked(core, ticket, rec, now)
                    )
                    rows.append((ticket.uid, ticket.key, self._run(rec, stamp(due), {}), reason))
        return rows, today, schedules_wire.early_s(core.ws)

    @staticmethod
    def _run(rec, occurrence, row):
        return SimpleNamespace(
            id=rec.id,
            occurrence=occurrence,
            person=rec.person,
            setter_session="",
            run=row.get("run"),
            grant=row.get("grant"),
            cancelled=rec.cancelled,
            child=row.get("child") or (schedules.child_uid(rec.id, occurrence) if rec.target == "new" else None),
        )

    async def _outcome(self, uid, rec, outcome, reason=None):
        await self.service.call(
            self._append,
            uid,
            "schedule.outcome",
            {
                "schedule": rec.id,
                "occurrence": rec.occurrence,
                "outcome": outcome,
                **({"reason": reason} if reason else {}),
            },
        )

    def _wrote(self, uid, rec, session):
        fired = bool(rec.child)
        for event, _ in self.service.store.events_after(rec.child or uid, 0, 1 << 30):
            if (
                event["type"] == "schedule.fired"
                and event.get("schedule") == rec.id
                and event.get("occurrence") == rec.occurrence
            ):
                fired = True
            elif fired and session and claims.in_family(event["actor"].get("session") or "", session):
                return True
        return False

    async def tick_once(self):
        async with self._lock:
            now, mono = self.clock(), self.mono()
            drift = (now - self._wall) - (mono - self._mono)
            self._wall, self._mono, self._last_tick = now, mono, now
            if drift < -300:
                return
            rows, today, _ = await self.service.call(self._read_schedules, int(now))
            hub = getattr(self.service, "runners", None)
            records = hub.store.records if hub else {}
            # Shared workspace concurrency across schedules and follow-ups, including runs
            # already submitted by the other timer. The model also checks atomically.
            live = sum(
                r.get("state") in LIVE
                and r.get("mode") == "followup"
                and r.get("workspace_id") == self.service.workspace_id
                for r in records.values()
            )
            for uid, key, rec, reason in rows:
                # One refused append (the model re-checks every firing and outcome) must not abort the tick: that
                # row records nothing now and is judged again from the ledger on the next tick; the rest proceed.
                try:
                    if rec.grant:
                        if rec.child:
                            problem, _ = await self.service.call(self._create_child, uid, rec)
                            if problem:
                                await self._outcome(uid, rec, "did_not_run", problem)
                                continue
                        await self._settle(uid, rec, records, hub)
                        continue
                    if reason:
                        await self._outcome(uid, rec, "did_not_run", reason)
                        continue
                    if (
                        live >= rules.FU_CONCURRENT_RUNS
                        or today >= rules.FU_RUNS_PER_DAY
                        or not hub
                        or not hub.connected
                    ):
                        continue
                    started = await self.fire(uid, key, rec, hub)
                except Exception:  # noqa: BLE001 - logged; the next tick retries from the ledger
                    LOG.warning("schedule %s occurrence %s: append refused", rec.id, rec.occurrence, exc_info=True)
                    continue
                live += started
                today += 1

    async def _settle(self, uid, rec, records, hub):
        """Record the outcome of a fired occurrence once its run is gone."""
        run = records.get(rec.run)
        if run is None:
            if hub and hub.connected:
                await self._outcome(uid, rec, "did_not_run", "host_restarted")
        elif run.get("state") not in LIVE:
            info = run.get("exit") or {}
            failed = (
                info.get("code") not in (None, 0)
                or info.get("signal")
                or info.get("reason") in ("spawn_failed", "lost", "runner_restarted")
            )
            wrote = await self.service.call(self._wrote, uid, rec, run.get("session"))
            nothing = await self.service.call(self._nothing_to_do, rec)
            if nothing and wrote:
                await self._outcome(uid, rec, "ran_nothing")
                return
            await self._outcome(
                uid,
                rec,
                "failed" if failed or not wrote else "ran",
                "harness_failed" if failed else "no_writes" if not wrote else None,
            )

    def _nothing_to_do(self, rec):
        child = self.service.store.state._core.tickets.get(rec.child)
        return child is not None and child.status == "closed" and child.close_resolution == "nothing_to_do"

    def _create_child(self, uid, run):
        store = self.service.store
        with store.locked():
            store.refresh()
            store.load_all()
            core = store.state._core
            child = core.tickets.get(run.child)
            if child is not None:
                return None, child.key
            anchor = core.tickets[uid]
            rec = anchor.schedules[run.id]
            reason = (
                "grant_revoked"
                if rec.cancelled
                else "addon_disabled"
                if not enabled(core.ws)
                else schedules_wire.blocked(core, anchor, rec, int(self.clock()), creating=run.occurrence)
            )
            if reason:
                return reason, None
            event = store.append(
                {
                    "type": "ticket.created",
                    "id": new_ulid(),
                    "based_on": None,
                    "actor": {"kind": "person", "id": rec.person, "device": rec.device},
                    "key": store.peek_key(),
                    "ticket_type": "chore",
                    "owner": rec.person,
                    "title": schedules.child_title(rec, run.occurrence),
                    "via": {
                        "anchor": uid,
                        "schedule_id": rec.id,
                        "occurrence": run.occurrence,
                        "set_sig_hash": rec.set_sig_hash,
                    },
                },
                log=run.child,
            ).event
            self.service.sync()
            return None, event["key"]

    async def fire(self, uid, key, rec, hub):
        token, secret, secret_hash = new_grant()
        rec.run, rec.grant = "r_" + new_ulid(), token.grant_id
        # The persisted fired event is the run intent. A crash before deposit is settled
        # as host_restarted, never retried; no secret is persisted.
        await self.service.call(
            self._append,
            uid,
            "schedule.fired",
            {
                "schedule": rec.id,
                "occurrence": rec.occurrence,
                "grant": rec.grant,
                "secret_hash": secret_hash,
                "run": rec.run,
                **({"child": rec.child} if rec.child else {}),
            },
        )
        target = uid
        if rec.child:
            problem, key = await self.service.call(self._create_child, uid, rec)
            if problem:
                await self._outcome(uid, rec, "did_not_run", problem)
                return False
            target = rec.child
        try:
            await self._deposit_start(target, key, rec, hub, secret)
            return True
        except Exception as exc:  # noqa: BLE001 - a persisted firing must settle, never retry
            if getattr(exc, "code", None) == "outcome.unknown" or rec.run in hub.store.records:
                return True
            with contextlib.suppress(Exception):  # disconnected runner must not prevent the ledger outcome
                await hub.command("grant.drop", {"grant": rec.grant})
            await self._outcome(uid, rec, "did_not_run", "runner_unavailable")
            return False
