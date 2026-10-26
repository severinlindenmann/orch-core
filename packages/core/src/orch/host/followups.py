"""Workspace timer for ledger-backed, single-use follow-up runs."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from types import SimpleNamespace

from orch.identity import new_grant, new_ulid
from orch.model import claims
from orch.model import followups as rules
from orch.model import followups_wire as wire
from orch.model.types import ts

from . import agents, custody
from .errors import HostError
from .runners import LIVE

LOG = logging.getLogger(__name__)


class FollowupRunner:
    def __init__(self, service, *, clock, mono, tick_s=30):
        self.service, self.clock, self.mono, self.tick_s = service, clock, mono, tick_s
        self.path = service.state_dir / "hosts" / service.workspace_id / "followups.json"
        self.intents = custody.read_json(self.path, service.host_uid) or {}
        self.task = None
        self._wall, self._mono = clock(), mono()
        self._last_tick, self._slept_s, self._blocked = None, 0, {}
        self._lock = asyncio.Lock()

    def start(self):
        self.task = asyncio.create_task(self._loop())

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _loop(self):
        while True:
            try:
                await self.tick_once()
            except Exception:  # noqa: BLE001 - a failed workspace tick must not kill the timer
                LOG.exception("follow-up tick failed")
            await asyncio.sleep(self.tick_s)

    def status(self):
        return {"last_tick": self._last_tick, "slept_s": self._slept_s, "blocked": dict(self._blocked)}

    def _save(self):
        custody.ensure_private_dir(self.path.parent, self.service.host_uid)
        custody.write_private(self.path, self.intents)

    def _owns(self, store, core):
        """Fail closed: only the host whose key is the workspace key (genesis wsk_pub) fires follow-ups."""
        from orch import crypto

        host = getattr(store, "_host", None)
        key = getattr(host, "public_key", None)
        ok = key is not None and bool(core.ws.wsk_pub) and crypto.b64u(key) == core.ws.wsk_pub
        return ok

    def _read(self, now):
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
        for ticket in core.tickets.values():
            for rec in ticket.followups.values():
                if rec.fired_at and ts(rec.fired_at) // 86400 == now // 86400:
                    today += 1
                state = wire.view_state(core, ticket, rec, now)[0]
                if state == "fired" or (state == "pending" and rec.trigger == "time"):
                    rows.append((ticket.uid, ticket.key, copy.copy(rec)))
        cap = rules.effective_caps(core.ws.settings.get("followups", {})).runs_per_day
        return rows, today, cap

    async def tick_once(self):
        async with self._lock:
            now, mono = self.clock(), self.mono()
            drift = (now - self._wall) - (mono - self._mono)
            self._wall, self._mono, self._last_tick = now, mono, now
            if drift < -300:
                LOG.warning("follow-up clock moved backwards; skipping tick")
                return
            if drift > 60:
                self._slept_s = drift
            rows, today, cap = await self.service.call(self._read, int(now))
            hub = getattr(self.service, "runners", None)
            records = hub.store.records if hub else {}
            self._blocked = {}
            live = 0
            for uid, _key, rec in rows:
                if rec.state != "fired":
                    continue
                run = records.get(rec.run)
                if run is None:
                    # The ledger is authoritative even if the cache write after fired was interrupted. Only settle
                    # once the runner is connected (its records are synced); until then the row stays fired.
                    if hub is not None and hub.connected:
                        tried = self.intents.get(rec.id, {}).get("attempted")
                        await self._outcome(
                            uid, rec, "did_not_run", "runner_unavailable" if tried else "host_restarted"
                        )
                elif run.get("state") in LIVE:
                    live += 1
                else:
                    exit_info = run.get("exit") or {}
                    failed = exit_info.get("code") not in (None, 0) or exit_info.get("signal")
                    failed = failed or exit_info.get("reason") in ("spawn_failed", "lost", "runner_restarted")
                    wrote = await self.service.call(self._wrote, uid, rec, run.get("session"))
                    await self._outcome(
                        uid,
                        rec,
                        "failed" if failed or not wrote else "ran",
                        "harness_failed" if failed else "no_writes" if not wrote else None,
                    )
            pending = sorted((r for r in rows if r[2].state == "pending"), key=lambda r: ts(r[2].due))
            for uid, key, rec in pending:
                if now > ts(rec.deadline or rec.due) + rules.FU_LATE_S:
                    await self._outcome(uid, rec, "did_not_run", "no_host")
                    continue
                if now < ts(rec.due) - rules.FU_TIME_EARLY_S or live >= rules.FU_CONCURRENT_RUNS or today >= cap:
                    continue
                reason = (
                    "runner_unavailable"
                    if hub is None or not hub.connected
                    else await self.service.call(agents.agent_missing, self.service, hub)
                )
                if reason:
                    self._blocked[rec.id] = reason
                    LOG.info("follow-up %s blocked: %s", rec.id, reason)
                    continue
                if await self.fire(uid, key, rec, hub):
                    live += 1
                today += 1

    def _wrote(self, uid, rec, session):
        fired = False
        for event, _ in self.service.store.events_after(uid, 0, 1 << 30):
            if event["type"] == "followup.fired" and event.get("followup") == rec.id:
                fired = True
            elif fired and session and claims.in_family(event["actor"].get("session") or "", session):
                return True
        return False

    def _append(self, uid, typ, payload):
        store = self.service.store
        store.refresh()
        event = store.append({"type": typ, "actor": {"kind": "host"}, **payload}, log=uid).event
        self.service.sync()
        return event

    async def _outcome(self, uid, rec, outcome, reason=None):
        payload = {"followup": rec.id, "outcome": outcome}
        if reason is not None:
            payload["reason"] = reason
        if rec.run:
            payload["run"] = rec.run
        await self.service.call(self._append, uid, "followup.outcome", payload)
        LOG.info("follow-up %s outcome %s %s", rec.id, outcome, reason or "")

    def _plan(self, uid, key, rec, hub):
        store = self.service.store
        store.refresh()
        grant = store.state._core.ws.grants[rec.grant]
        previous = [
            r
            for r in hub.store.records.values()
            if r.get("workspace_id") == self.service.workspace_id
            and r.get("ticket") in (uid, key)
            and r.get("kind") == "agent"
            and r.get("person") == rec.person
        ]
        own = [r for r in previous if r.get("session") and claims.in_family(r["session"], rec.setter_session)]
        last = max(own or previous, key=lambda r: r.get("created_at", ""), default={})
        args = {
            "ticket": key,
            "harness": last.get("harness") or "claude-code",
            "mode": "followup",
            "model": last.get("model"),
        }
        call = SimpleNamespace(
            service=self.service,
            viewer=rec.person,
            column=SimpleNamespace(name="G"),
            principal=f"g:{rec.grant}@{self.service.host_uid}",
            verified=SimpleNamespace(grant=grant),
            frame={"auth": {}},
        )
        fu = {"id": rec.id, "grant": rec.grant, "until": grant.expires_at, "scope": uid}
        plan = agents.plan(call, args, hub, agent=True, fu=fu)
        if plan["blocked"]:
            raise HostError(plan["blocked"]["code"], plan["blocked"]["message"])
        data = dict(
            run=rec.run,
            person=rec.person,
            harness=args["harness"],
            mode="followup",
            model=plan["model"],
            launch=plan["_launch"],
            cwd=plan["cwd"],
            spec=plan["spec"],
            idem=rec.id,
            policy=plan["policy"],
            subagent_model=None,
            label=None,
            cols=120,
            rows=40,
        )
        return call, data, grant.expires_at

    async def fire(self, uid, key, rec, hub):
        run = "r_" + new_ulid()
        self.intents[rec.id] = {"fu": rec.id, "run": run, "at": self.clock(), "state": "intent"}
        await self.service.call(self._save)
        token, secret, secret_hash = new_grant()
        await self.service.call(
            self._append,
            uid,
            "followup.fired",
            {
                "followup": rec.id,
                "grant": token.grant_id,
                "trigger": "time",
                "secret_hash": secret_hash,
                "run": run,
            },
        )
        rec.run, rec.grant = run, token.grant_id
        LOG.info("follow-up %s fired run %s", rec.id, run)
        try:
            self.intents[rec.id]["state"] = "fired"
            await self.service.call(self._save)
            # Read the derived expiry from the admitted ledger event, never from parent expiry.
            until = await self.service.call(lambda: self.service.store.state._core.ws.grants[rec.grant].expires_at)
            await hub.command(
                "grant.put_run",
                {
                    "run": run,
                    "workspace_id": self.service.workspace_id,
                    "person": rec.person,
                    "grant": rec.grant,
                    "secret": secret,
                    "until": until,
                },
            )
            del secret, token
            call, data, _ = await self.service.call(self._plan, uid, key, rec, hub)
            await agents._start(call, hub, data)
            return True
        except Exception as exc:  # noqa: BLE001 - a fired follow-up must settle, never retry
            code = getattr(exc, "code", None) or ""
            if code == "outcome.unknown" or hub.store.records.get(run) is not None:
                self.intents.setdefault(rec.id, {})["attempted"] = True
                with contextlib.suppress(Exception):
                    await self.service.call(self._save)
                # the start may have happened: leave the row fired, the next tick settles it from the run record
                return True
            reason = (
                "runner_unavailable"
                if code.startswith("runner.")
                else "budget"
                if code in ("run.limit", "rate.limited")
                else "harness_failed"
                if code.startswith(("harness.", "launch."))
                else "error"
            )
            with contextlib.suppress(Exception):  # the unused one-run secret must not stay in the runner
                await hub.command("grant.drop", {"grant": rec.grant})
            await self._outcome(uid, rec, "did_not_run", reason)
            return False
