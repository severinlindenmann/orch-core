"""Workspace timer for ledger-backed, single-use follow-up runs."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

from orch.identity import new_grant, new_ulid
from orch.model import claims
from orch.model import followup_events as fe
from orch.model import followups as rules
from orch.model import followups_wire as wire
from orch.model.types import ts

from . import addonpoll, agents, custody, hubpolicy
from .errors import HostError
from .runners import LIVE

LOG = logging.getLogger(__name__)


class FollowupRunner:
    def __init__(self, service, *, clock, mono, tick_s=30):
        self.service, self.clock, self.mono, self.tick_s = service, clock, mono, tick_s
        self.path = service.state_dir / "hosts" / service.workspace_id / "followups.json"
        self.intents = custody.read_json(self.path, service.host_uid) or {}
        self.rings_path = self.path.with_name("hook-rings.json")  # rings live apart from the run intents
        self.rings = custody.read_json(self.rings_path, service.host_uid) or {}
        #: hook hashes of this workspace's pending webhook follow-ups, refreshed by every read; the receiver asks this
        #: set first so an unknown id never costs a store read
        self.hooks = frozenset()
        # addon-api 1.2 (#718): polling of add-on events; only refs a pending follow-up waits on are ever asked
        self.polls_path = self.path.with_name("addon-polls.json")
        self.poller = addonpoll.Poller(
            saved=custody.read_json(self.polls_path, service.host_uid), save=self._save_polls
        )
        self.addon_done = {}  # (kind, ref) -> the closed event shape of a finished poll
        self.intents.pop("_rings", None)  # rings used to live here; they have their own file now
        self.task = None
        self._wall, self._mono = clock(), mono()
        self._last_tick, self._slept_s, self._blocked = None, 0, {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._cursor = {}  # follow-up id -> last ticket-log seq scanned for its event

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
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.tick_s)
            self._wake.clear()

    def status(self):
        return {
            "last_tick": self._last_tick,
            "slept_s": self._slept_s,
            "blocked": dict(self._blocked),
            "addon_sources": {
                k: dict(v) for k, v in self.poller.sources.items()
            },  # failed polls: kind|ref -> code, since
        }

    def _save_polls(self, data):
        custody.ensure_private_dir(self.polls_path.parent, self.service.host_uid)
        custody.write_private(self.polls_path, data)

    def _save_rings(self):
        custody.ensure_private_dir(self.rings_path.parent, self.service.host_uid)
        custody.write_private(self.rings_path, self.rings)

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
            return [], 0, (0, rules.FU_TIME_EARLY_S)
        store = service.store
        store.refresh()
        store.load_all()
        core = store.state._core
        if not self._owns(store, core):
            return [], 0, (0, rules.FU_TIME_EARLY_S)
        rows, today, hooks = [], 0, set()
        for ticket in core.tickets.values():
            for rec in ticket.followups.values():
                if rec.fired_at and ts(rec.fired_at) // 86400 == now // 86400:
                    today += 1
                state = wire.view_state(core, ticket, rec, now)[0]
                if state == "pending" and rec.hook:
                    hooks.add(rec.hook)
                if state == "fired" or (state == "pending" and rec.trigger in ("time", "orch", "webhook", "addon")):
                    rows.append((ticket.uid, ticket.key, copy.copy(rec)))
        self.hooks = frozenset(hooks)
        caps = wire.caps_of(core.ws)
        return rows, today, (caps.runs_per_day, caps.early_s)

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
            rows, today, (cap, early) = await self.service.call(self._read, int(now))
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
            await self._poll_addons(rows, now)
            pending = sorted((r for r in rows if r[2].state == "pending"), key=lambda r: ts(r[2].due or r[2].deadline))
            for uid, key, rec in pending:
                if now > ts(rec.deadline or rec.due) + rules.FU_LATE_S:
                    await self._outcome(uid, rec, "did_not_run", "no_host")
                    continue
                wake = await self._wake_reason(uid, rec, now, early)
                if wake is None or live >= rules.FU_CONCURRENT_RUNS or today >= cap:
                    continue
                reason = (
                    "runner_unavailable"
                    if hub is None or not hub.connected
                    else await self.service.call(agents.agent_missing, self.service, hub, "schedules")
                )
                if reason:
                    self._blocked[rec.id] = reason
                    LOG.info("follow-up %s blocked: %s", rec.id, reason)
                    continue
                if await self.fire(uid, key, rec, hub, *wake):
                    live += 1
                today += 1

    # -- what wakes a pending follow-up: (trigger, fallback, event) or None
    async def _wake_reason(self, uid, rec, now, early):
        if rec.trigger == "time":
            return ("time", None, None) if now >= ts(rec.due) - early else None
        if rec.trigger == "addon":
            found = self.addon_done.get(((rec.on or {}).get("kind"), (rec.on or {}).get("ref")))
            if found is not None and fe.matches(rec.on, found):
                return ("addon", None, dict(found))
        elif rec.trigger == "webhook":
            rang = self.rings.get(rec.hook)
            if rang is not None:
                return ("webhook", None, fe.webhook_event(rec.hook, rang))
        else:
            event = await self.service.call(self._scan, uid, rec)
            if event is not None:
                return ("orch", None, event)
        if now >= ts(rec.deadline):  # the source never came: the deadline wakes it, with the reason
            fallback = {"webhook": "no_ring", "addon": "addon_silent"}.get(rec.trigger, "no_event")
            return ("deadline", fallback, None)
        return None

    # -- add-on events (#718): the host asks an add-on only about refs that a pending follow-up names
    def _addon_snapshot(self, waiting):
        """Worker thread: the ACTIVE grants (and their signed settings) of the add-ons that waiting follow-ups name."""
        from orch.addons import install
        from orch.addons.settings_value import KEY
        from orch.store.render import thaw

        ws = self.service.store.state.workspace
        names = {fe.addon_of((r.on or {}).get("kind", "")) for r in waiting}
        granted = install.as_addons({n: a for n, a in ws.addons.items() if n in names})
        held = ws.settings.get(KEY)
        return {
            n: (a, thaw(held[n]) if held is not None and n in held else None)
            for n, a in granted.items()
            if a.enabled and not a.purged
        }

    def _poll_one(self, item, granted, settings):
        """Executor thread: one `events.poll` of the add-on named by the kind's first word; (state, status)."""
        from orch.addons import credential, envtree, install, runner

        s = self.service
        package, _ = install.read_installed(s.store.layout.store, granted.name)
        provider = credential.provider(s.state_dir, s.workspace_id, s.host_uid, granted.name, granted.capabilities)
        env = envtree.EnvLocation(str(s.state_dir), s.workspace_id) if granted.runtime is not None else None
        return runner.poll(
            package, granted, item[1], item[2], settings=settings, limits=runner.Limits(timeout=10.0),
            credential=provider, env=env, gate=hubpolicy.run_gate(s),
        )  # fmt: skip

    async def _poll_addons(self, rows, now):
        waiting = [r[2] for r in rows if r[2].trigger == "addon" and r[2].state == "pending"]
        snap = await self.service.call(self._addon_snapshot, waiting) if waiting else {}
        items = []
        for rec in waiting:
            kind, ref = rec.on["kind"], rec.on["ref"]
            entry = snap.get(fe.addon_of(kind))
            decl = entry and fe.declared({entry[0].name: entry[0]}, kind)
            if decl and fe.addon_fits(decl, ref):
                items.append((entry[0].name, kind, ref, decl["interval_s"]))
        self.poller.forget(items)
        wanted = {(i[1], i[2]) for i in items}
        self.addon_done = {k: v for k, v in self.addon_done.items() if k in wanted}
        mono = self.mono()
        loop = asyncio.get_running_loop()
        for item in self.poller.plan([i for i in items if (i[1], i[2]) not in self.addon_done], mono, now):
            granted, settings = snap[item[0]]
            try:
                state, status = await loop.run_in_executor(None, self._poll_one, item, granted, settings)
            except Exception as exc:  # noqa: BLE001 - a failed poll is a failed source, never a crash of the timer
                code = addonpoll.code_of(getattr(exc, "code", ""))
                LOG.warning("add-on poll %s failed: %s", item[1], getattr(exc, "code", type(exc).__name__))
                self.poller.failed(item, code, self.mono(), now)
                continue
            if state == "done":
                at = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                self.addon_done[(item[1], item[2])] = fe.addon_event(item[1], item[2], status, at)
                self.poller.done(item)
            elif state == "pending":
                self.poller.pending(item, self.mono())
            else:
                self.poller.failed(item, "reported", self.mono(), now)

    def _scan(self, uid, rec):
        """The first ticket-log event after the set that matches the follow-up's `on`; only its closed shape leaves."""
        store = self.service.store
        store.refresh()
        after = self._cursor.get(rec.id)
        if after is None:
            after = store.seq_before(uid, ts(rec.set_at))
        while True:
            batch = store.events_after(uid, after, 256)
            for event, _ in batch:
                after = event["seq"]
                shape = fe.from_ledger(event)
                if shape is not None and ts(shape["at"]) >= ts(rec.set_at) and fe.matches(rec.on, shape):
                    self._cursor[rec.id] = after
                    return shape
            if len(batch) < 256:
                self._cursor[rec.id] = after
                return None

    def learn_hooks(self):
        """N4 (c): called on the worker thread right after a ``followup.set`` is committed, so a real pending hook is
        known before its first ring (the tick is 30 s on a real host and a webhook sender rings once). Only adds hashes;
        an unknown hash still costs nothing (no store read) and shares no slot with a known one."""
        store = self.service.store
        if store is None or self.service.state != "ok" or self.service.closing:
            return
        core = store.state._core
        if not self._owns(store, core):
            return
        now = int(self.clock())
        new = {
            rec.hook
            for t in core.tickets.values()
            for rec in t.followups.values()
            if rec.hook and wire.view_state(core, t, rec, now)[0] == "pending"
        }
        if new - self.hooks:
            self.hooks = self.hooks | frozenset(new)

    # -- doorbell (host/hooks.py calls this; the body of a request never gets here)
    def _hook_pending(self, hook_hash):
        store = self.service.store
        if store is None or self.service.state != "ok" or self.service.closing:
            return False
        store.refresh()
        store.load_all()
        core = store.state._core
        now = int(self.clock())
        return self._owns(store, core) and any(
            r.hook == hook_hash and wire.view_state(core, t, r, now)[0] == "pending"
            for t in core.tickets.values()
            for r in t.followups.values()
        )

    async def ring(self, hook_hash):
        """True when a pending follow-up of this workspace is bound to the hook. Only the first ring counts."""
        if hook_hash not in self.hooks or not await self.service.call(self._hook_pending, hook_hash):
            LOG.info("webhook ring ignored: no pending follow-up for hook %s", hook_hash[:8])
            return False
        LOG.info("webhook ring accepted for hook %s", hook_hash[:8])
        if hook_hash not in self.rings:
            self.rings[hook_hash] = datetime.fromtimestamp(self.clock(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            await self.service.call(self._save_rings)
        self._wake.set()
        return True

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
            idem=rec.run if rec.id.startswith("sc_") else rec.id,
            policy=plan["policy"],
            subagent_model=None,
            label=None,
            cols=120,
            rows=40,
        )
        return call, data, grant.expires_at

    async def fire(self, uid, key, rec, hub, trigger="time", fallback=None, event=None):
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
                "trigger": trigger,
                "secret_hash": secret_hash,
                "run": run,
                **({"fallback": fallback} if fallback else {}),
                **({"event": event} if event else {}),
            },
        )
        rec.run, rec.grant = run, token.grant_id
        if rec.hook:
            self.rings.pop(rec.hook, None)
            await self.service.call(self._save_rings)
        LOG.info("follow-up %s fired run %s", rec.id, run)
        try:
            self.intents[rec.id]["state"] = "fired"
            await self.service.call(self._save)
            await self._deposit_start(uid, key, rec, hub, secret)
            del secret, token
            return True
        except Exception as exc:  # noqa: BLE001 - a fired follow-up must settle, never retry
            code = getattr(exc, "code", None) or ""
            LOG.warning("follow-up %s start failed: %s %s", rec.id, code, str(exc)[:200])
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

    async def _deposit_start(self, uid, key, rec, hub, secret):
        # Read the derived expiry from the admitted ledger event, never from parent expiry.
        until = await self.service.call(lambda: self.service.store.state._core.ws.grants[rec.grant].expires_at)
        await hub.command(
            "grant.put_run",
            {
                "run": rec.run,
                "workspace_id": self.service.workspace_id,
                "person": rec.person,
                "grant": rec.grant,
                "secret": secret,
                "until": until,
            },
        )
        call, data, _ = await self.service.call(self._plan, uid, key, rec, hub)
        await agents._start(call, hub, data)
