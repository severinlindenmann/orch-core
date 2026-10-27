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
