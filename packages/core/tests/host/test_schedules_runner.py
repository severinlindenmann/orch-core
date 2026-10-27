"""Manual scheduler deposits fresh run grants and never repeats persisted occurrences."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from orch.host.schedules import ScheduleRunner
from orch.model import advance
from tests.host.test_followups_runner import env, w  # noqa: F401
from tests.model.test_schedules_wire import setup


@pytest.mark.parametrize("cancelled", [False, True])
def test_once_restart_cancel(monkeypatch, tmp_path, cancelled):
    asyncio.run(_once_restart_cancel(monkeypatch, tmp_path, cancelled))


async def _once_restart_cancel(monkeypatch, tmp_path, cancelled):
    from orch.host.followups import FollowupRunner

    w, uid, event, due = setup()  # noqa: F811 - imported fixture
    if cancelled:
        w.tev(uid, "schedule.cancelled", "tom", schedule=event["schedule"])
    w.clock = due
    state = w.state()
    commands = []

    class Store:
        def refresh(self):
            pass

        def load_all(self):
            pass

        def append(self, payload, log):
            e, result = w.try_(
                log, payload["type"], w.HOST, **{k: v for k, v in payload.items() if k not in ("type", "actor")}
            )
            assert not hasattr(result, "code"), result
            self.state = advance(self.state, e, log=log)
            return NS(event=e)

    store = Store()
    store.state = state

    async def call(fn, *args):
        return fn(*args)

    async def command(name, payload):
        commands.append((name, payload))

    hub = NS(connected=True, store=NS(records={}), command=command)
    service = NS(
        state_dir=tmp_path,
        workspace_id=w.workspace_id,
        host_uid=__import__("os").getuid(),
        store=store,
        state="ok",
        closing=False,
        call=call,
        sync=lambda: None,
        runners=hub,
    )
    monkeypatch.setattr(FollowupRunner, "_owns", lambda *a: True)

    from orch.host import agents

    def plan(self, uid, key, rec, hub):
        return None, {"run": rec.run}, None

    async def launch(call, hub, data):
        hub.store.records[data["run"]] = {"state": "running"}

    monkeypatch.setattr(FollowupRunner, "_plan", plan)
    monkeypatch.setattr(agents, "_start", launch)
    runner = ScheduleRunner(service, clock=lambda: due.timestamp(), mono=lambda: 0)
    await runner.tick_once()
    runner = ScheduleRunner(service, clock=lambda: due.timestamp(), mono=lambda: 0)
    await runner.tick_once()
    assert len(commands) == (0 if cancelled else 1)
    if commands:
        row = next(iter(store.state._core.tickets[uid].schedules[event["schedule"]].occurrences.values()))
        assert row["grant"] == commands[0][1]["grant"]
        assert commands[0][1]["secret"]


def test_host_early_window_never_refused_for_earliness(monkeypatch, tmp_path):
    asyncio.run(_early_edge(monkeypatch, tmp_path))


async def _early_edge(monkeypatch, tmp_path):
    from datetime import timedelta

    from orch.host import agents
    from orch.host.followups import FollowupRunner
    from orch.model.schedules_wire import early_s

    w, uid, event, due = setup()  # noqa: F811 - imported fixture
    commands, appended = [], []

    class Store:
        def refresh(self):
            pass

        def load_all(self):
            pass

        @property
        def state(self):
            return w.state()

        def append(self, payload, log):
            # the World stamps `at` 10 s after its clock; the host's own clock is that `at`
            w.clock = clock[0] - timedelta(seconds=10)
            e, result = w.try_(
                log, payload["type"], w.HOST, **{k: v for k, v in payload.items() if k not in ("type", "actor")}
            )
            assert not hasattr(result, "code"), result  # the model admits every firing the host starts
            appended.append(e)
            return NS(event=e)

    async def call(fn, *args):
        return fn(*args)

    async def command(name, payload):
        commands.append((name, payload))

    async def launch(call, hub, data):
        hub.store.records[data["run"]] = {"state": "running"}

    hub = NS(connected=True, store=NS(records={}), command=command)
    service = NS(
        state_dir=tmp_path,
        workspace_id=w.workspace_id,
        host_uid=__import__("os").getuid(),
        store=Store(),
        state="ok",
        closing=False,
        call=call,
        sync=lambda: None,
        runners=hub,
    )
    monkeypatch.setattr(FollowupRunner, "_owns", lambda *a: True)
    monkeypatch.setattr(FollowupRunner, "_plan", lambda self, uid, key, rec, hub: (None, {"run": rec.run}, None))
    monkeypatch.setattr(agents, "_start", launch)
    window = early_s(w.state()._core.ws)
    clock = [due - timedelta(seconds=window + 1)]
    runner = ScheduleRunner(service, clock=lambda: clock[0].timestamp(), mono=lambda: 0)
    await runner.tick_once()
    assert not appended and not commands  # before the window: the host does nothing
    clock[0] = due - timedelta(seconds=window)
    await runner.tick_once()
    assert [e["type"] for e in appended] == ["schedule.fired"] and appended[0]["at"] == w.at()
    assert len(commands) == 1


def test_addon_off_skips_firing_settles_due_and_on_again_fires_fresh(monkeypatch, tmp_path):
    asyncio.run(_addon_off_on(monkeypatch, tmp_path))


async def _addon_off_on(monkeypatch, tmp_path):
    from datetime import timedelta

    from orch.host import agents
    from orch.host.followups import FollowupRunner
    from orch.model.followups_wire import grant_chain
    from orch.model.schedules_wire import root_valid
    from orch.model.types import ts
    from tests.model.test_schedules_wire import grant_addon
    from tests.model.world import World, stamp

    w = World().bootstrap({"tom": "member"})  # noqa: F811 - imported fixture
    grant_addon(w)
    uid = w.ticket(owner="tom")
    due = w.clock.replace(second=0) + timedelta(hours=2)
    event, result = w.try_(
        uid,
        "schedule.set",
        "tom",
        schedule="sc_" + w.nid(),
        brief="Daily check",
        rule={"every": "day", "at": due.strftime("%H:%M"), "tz": "UTC"},
        starts=w.at(),
        ends=stamp(due + timedelta(days=2, hours=1)),
        run_hours=2,
    )
    assert not hasattr(result, "code"), result
    commands = []

    class Store:
        def refresh(self):
            pass

        def load_all(self):
            pass

        @property
        def state(self):
            return w.state()

        def append(self, payload, log):
            e, result = w.try_(
                log, payload["type"], w.HOST, **{k: v for k, v in payload.items() if k not in ("type", "actor")}
            )
            assert not hasattr(result, "code"), result
            return NS(event=e)

    async def call(fn, *args):
        return fn(*args)

    async def command(name, payload):
        commands.append((name, payload))

    async def launch(call, hub, data):
        hub.store.records[data["run"]] = {"state": "running"}

    hub = NS(connected=True, store=NS(records={}), command=command)
    service = NS(
        state_dir=tmp_path,
        workspace_id=w.workspace_id,
        host_uid=__import__("os").getuid(),
        store=Store(),
        state="ok",
        closing=False,
        call=call,
        sync=lambda: None,
        runners=hub,
    )
    monkeypatch.setattr(FollowupRunner, "_owns", lambda *a: True)
    monkeypatch.setattr(FollowupRunner, "_plan", lambda self, uid, key, rec, hub: (None, {"run": rec.run}, None))
    monkeypatch.setattr(agents, "_start", launch)
    clock = [due]
    runner = ScheduleRunner(service, clock=lambda: clock[0].timestamp(), mono=lambda: 0)

    def occurrences():
        return w.state()._core.tickets[uid].schedules[event["schedule"]].occurrences

    def tick(at):
        clock[0] = w.clock = at
        return runner.tick_once()

    await tick(due)
    assert len(commands) == 1
    first = commands[0][1]["grant"]

    w.clock = due + timedelta(minutes=10)
    w.wev("addon.disabled", "sev", name="schedules")
    core = w.state()._core
    chain = grant_chain(core.ws, first)
    assert chain and not root_valid(core, chain[-1].id, ts(w.at())) and core.ws.grants[first].ended

    second = due + timedelta(days=1)
    await tick(second - timedelta(minutes=2))  # inside the early window: nothing fires and nothing is settled yet
    assert len(commands) == 1 and stamp(second) not in occurrences()
    await tick(second)
    assert len(commands) == 1
    row = occurrences()[stamp(second)]
    assert (row["outcome"], row["reason"], row.get("grant")) == ("did_not_run", "addon_disabled", None)

    w.clock = second + timedelta(hours=1)
    grant_addon(w)
    await tick(second + timedelta(hours=1))
    assert len(commands) == 1  # no backfill of the occurrence missed while off
    assert w.state()._core.ws.grants[first].ended

    third = due + timedelta(days=2)
    await tick(third)
    assert len(commands) == 2
    fresh = commands[1][1]["grant"]
    assert fresh != first and occurrences()[stamp(third)]["grant"] == fresh
    core = w.state()._core
    assert not core.ws.grants[fresh].ended and root_valid(core, grant_chain(core.ws, fresh)[-1].id, ts(w.at()))


def test_real_store_deposit_and_closed_prompt(env):  # noqa: F811 - imported fixture
    from datetime import UTC, datetime

    from orch.host.auth import build_auth
    from orch.identity import new_ulid
    from tests.store.helpers import ts

    w = env.w  # noqa: F811 - imported fixture
    uid, key = w.ticket("Manual schedule")
    due = int(w.lab.ws.clock[0] // 60 * 60 + 7200)
    local = datetime.fromtimestamp(due, UTC)
    sid = "sc_" + new_ulid()
    w.person_event(
        "schedule.set",
        uid,
        schedule=sid,
        brief="Brief stays in the ledger",
        rule={"every": "once", "at": local.strftime("%H:%M"), "tz": "UTC", "date": local.strftime("%Y-%m-%d")},
        starts=ts(w.lab.ws.clock[0]),
        ends=ts(due + 3600),
        run_hours=2,
    )
    w.lab.ws.clock[0] = due
    runner = ScheduleRunner(w.service, clock=lambda: w.lab.ws.clock[0], mono=lambda: env.mono[0])
    w.h.run(runner.tick_once())
    assert [cmd for cmd, _ in env.hub.commands] == ["grant.put_run", "spawn"]
    put, spawn = [payload for _, payload in env.hub.commands]
    assert sid in spawn["argv"][-1] and "Brief stays in the ledger" not in spawn["argv"][-1]
    assert "orch schedule show" in spawn["argv"][-1]
    from orch import canon, crypto

    secret = crypto.unb64u(put["secret"].split(".")[1])
    assert w.service.store.grant_secret_hash(put["grant"]) == canon.grant_secret_hash(secret)
    auth = w.h.run(w.service.call(build_auth, w.service.store, w.service.store.root))
    assert auth.verify(put["secret"], due).state == "verified"
    runner = ScheduleRunner(w.service, clock=lambda: w.lab.ws.clock[0], mono=lambda: env.mono[0])
    w.h.run(runner.tick_once())
    assert len(env.hub.commands) == 2
