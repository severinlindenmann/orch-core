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


def daily_harness(monkeypatch, tmp_path):
    """A daily schedule by tom on his ticket and a runner whose hub records and appended events are visible."""
    from datetime import timedelta

    from orch.host import agents
    from orch.host.followups import FollowupRunner
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
            e, result = w.try_(
                log, payload["type"], w.HOST, **{k: v for k, v in payload.items() if k not in ("type", "actor")}
            )
            assert not hasattr(result, "code"), result
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
    clock = [due]
    runner = ScheduleRunner(service, clock=lambda: clock[0].timestamp(), mono=lambda: 0)

    def tick(at):
        clock[0] = w.clock = at
        return runner.tick_once()

    def occurrences():
        return w.state()._core.tickets[uid].schedules[event["schedule"]].occurrences

    return NS(
        w=w,
        uid=uid,
        due=due,
        hub=hub,
        commands=commands,
        appended=appended,
        tick=tick,
        occurrences=occurrences,
        event=event,
        store=service.store,
        runner=runner,
        clock=clock,
    )


def test_backdated_starts_host_never_fires_an_occurrence_before_signing(monkeypatch, tmp_path):
    asyncio.run(_backdated(monkeypatch, tmp_path))


async def _backdated(monkeypatch, tmp_path):
    from datetime import timedelta

    from tests.model.test_schedules_wire import backdated_daily
    from tests.model.world import stamp

    h = daily_harness(monkeypatch, tmp_path)
    h.w.tev(h.uid, "schedule.cancelled", "tom", schedule=h.event["schedule"])  # only the backdated rule is live
    e, nine = backdated_daily(h.w, h.uid)

    def occurrences():
        return h.w.state()._core.tickets[h.uid].schedules[e["schedule"]].occurrences

    await h.tick(nine + timedelta(minutes=31))
    # 09:00 precedes the signing: the host neither fires nor settles it (the model would refuse both).
    assert stamp(nine) not in occurrences()
    assert not [x for x in h.appended if x.get("schedule") == e["schedule"]]
    nxt = nine + timedelta(days=1)
    await h.tick(nxt)
    fired = [x for x in h.appended if x.get("schedule") == e["schedule"]]
    assert [(x["type"], x["occurrence"]) for x in fired] == [("schedule.fired", stamp(nxt))]


def test_a_refused_append_does_not_abort_the_tick(monkeypatch, tmp_path):
    asyncio.run(_refused_append(monkeypatch, tmp_path))


async def _refused_append(monkeypatch, tmp_path):
    from tests.model.world import stamp

    h = daily_harness(monkeypatch, tmp_path)
    payload = {k: h.event[k] for k in ("brief", "rule", "starts", "ends", "run_hours")}
    other, result = h.w.try_(h.uid, "schedule.set", "tom", schedule="sc_" + h.w.nid(), **payload)
    assert not hasattr(result, "code"), result
    append, calls = h.store.append, []

    def flaky(payload, log):
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("schedule.denied: refused by the model")
        return append(payload, log)

    h.store.append = flaky
    await h.tick(h.due)
    # The first row's append was refused: nothing recorded for it; the second row still fired.
    assert stamp(h.due) not in h.occurrences()
    second = h.w.state()._core.tickets[h.uid].schedules[other["schedule"]].occurrences
    assert "grant" in second[stamp(h.due)] and len(h.commands) == 1
    await h.tick(h.due)  # the next tick re-evaluates the first row from the ledger
    assert "grant" in h.occurrences()[stamp(h.due)] and len(h.commands) == 2


def test_dead_own_session_claim_is_released_by_the_firing(monkeypatch, tmp_path):
    asyncio.run(_dead_own_session(monkeypatch, tmp_path))


async def _dead_own_session(monkeypatch, tmp_path):
    from datetime import timedelta

    from tests.model.world import stamp

    h = daily_harness(monkeypatch, tmp_path)
    h.w.clock = h.due - timedelta(minutes=90)
    a = h.w.claim(h.uid, "tom", gid=h.w.grant("tom", "workable", hours=1))
    # The runner knows that session, and it has ended; its one-hour grant expired, so the claim has lapsed.
    h.hub.store.records["r_old"] = {"state": "exited", "session": a["session"], "workspace_id": h.w.workspace_id}
    await h.tick(h.due)
    assert [e["type"] for e in h.appended] == ["schedule.fired"] and len(h.commands) == 1
    row = h.occurrences()[stamp(h.due)]
    assert "grant" in row and row["released"] is True
    assert h.w.view(h.uid).claim is None  # released as part of schedule.fired


def test_live_own_claim_is_busy_even_when_the_runner_says_its_session_ended(monkeypatch, tmp_path):
    asyncio.run(_live_own_claim_dead_session(monkeypatch, tmp_path))


async def _live_own_claim_dead_session(monkeypatch, tmp_path):
    from datetime import timedelta

    from tests.model.world import stamp

    h = daily_harness(monkeypatch, tmp_path)
    h.w.clock = h.due - timedelta(minutes=5)
    a = h.w.claim(h.uid, "tom")  # an ordinary grant, live at the occurrence
    h.hub.store.records["r_old"] = {"state": "exited", "session": a["session"], "workspace_id": h.w.workspace_id}
    await h.tick(h.due)
    assert not h.commands and [e["type"] for e in h.appended] == ["schedule.outcome"]
    row = h.occurrences()[stamp(h.due)]
    assert (row["outcome"], row["reason"]) == ("did_not_run", "busy")
    assert h.w.view(h.uid).claim.session == a["session"]


def test_live_run_holding_the_claim_is_busy_and_next_occurrence_fires(monkeypatch, tmp_path):
    asyncio.run(_live_run_busy(monkeypatch, tmp_path))


async def _live_run_busy(monkeypatch, tmp_path):
    from datetime import timedelta

    from orch.model import claims
    from orch.model.types import ts
    from tests.model.world import stamp

    h = daily_harness(monkeypatch, tmp_path)
    h.w.clock = h.due - timedelta(minutes=5)
    h.w.settings(grant_hours=48)  # the interactive session's claim outlives the day to the next occurrence
    a = h.w.claim(h.uid, "tom", gid=h.w.grant("tom", "workable", hours=48))
    h.hub.store.records["r_live"] = {"state": "running", "session": a["session"], "workspace_id": h.w.workspace_id}
    await h.tick(h.due)
    assert not h.commands
    assert [e["type"] for e in h.appended] == ["schedule.outcome"]
    row = h.occurrences()[stamp(h.due)]
    assert (row["outcome"], row["reason"], row.get("grant")) == ("did_not_run", "busy", None)
    assert h.w.view(h.uid).claim.session == a["session"]  # never released under a live run
    await h.tick(h.due + timedelta(minutes=1))
    assert not h.commands and len(h.appended) == 1  # no retry of this occurrence

    # The run ends and the session stays idle past the claim TTL: the claim lapsed, so the next day fires.
    h.hub.store.records["r_live"]["state"] = "exited"
    second = h.due + timedelta(days=1)
    h.w.clock = second - timedelta(minutes=5)
    core = h.w.state()._core
    assert claims.lapse_reason(core.ws, core.tickets[h.uid], ts(h.w.at())) == "expired"
    await h.tick(second)
    assert len(h.commands) == 1 and "grant" in h.occurrences()[stamp(second)]
    assert h.w.view(h.uid).claim is None


def test_claim_of_another_person_is_claimed_by_other(monkeypatch, tmp_path):
    asyncio.run(_other_person(monkeypatch, tmp_path))


async def _other_person(monkeypatch, tmp_path):
    from datetime import timedelta

    from tests.model.world import stamp

    h = daily_harness(monkeypatch, tmp_path)
    h.w.clock = h.due - timedelta(minutes=5)
    a = h.w.claim(h.uid, "sev")  # no runner record: whether it runs does not matter for another person
    await h.tick(h.due)
    assert not h.commands and [e["type"] for e in h.appended] == ["schedule.outcome"]
    row = h.occurrences()[stamp(h.due)]
    assert (row["outcome"], row["reason"]) == ("did_not_run", "claimed_by_other")
    assert h.w.view(h.uid).claim.session == a["session"]


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
