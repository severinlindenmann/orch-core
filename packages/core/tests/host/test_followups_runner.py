"""Time follow-ups through the live host store and planner, with a fake runner."""

import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from orch.host import agents
from orch.host.followups import FollowupRunner
from orch.identity import new_ulid
from orch.model import followups as rules
from orch.runner.config import RunnerConfig
from tests.host.dispatch_helpers import SESSION, ok
from tests.host.test_agents import grant_addon, w  # noqa: F401
from tests.store.helpers import ts


class Hub:
    def __init__(self, host):
        self.host, self.connected = host, True
        self.store = NS(records={})
        self.grants = Mock()
        self.commands = []
        self.starting, self.uid = {}, 0

    def take_start(self, person):
        return True

    def require_current(self):
        pass

    async def command(self, name, payload):
        self.commands.append((name, dict(payload)))
        return {}

    async def intent(self, wid, fields, payload, **audit):
        self.store.records[fields["run"]] = dict(fields, workspace_id=wid, state="starting")

    async def dispatch(self, run, payload, **kw):
        await self.command("spawn", dict(payload, run=run))
        self.store.records[run]["state"] = "running"
        return self.store.records[run]


@pytest.fixture
def env(w, monkeypatch, tmp_path):  # noqa: F811 - imported fixture
    from orch.runner import config

    async def no_timer(*args):
        pass

    monkeypatch.setattr(w.h.host, "_start_followups", no_timer)
    grant_addon(w, "schedules", ())
    hub = Hub(w.h.host)
    w.service.runners = hub
    monkeypatch.setattr(agents, "agent_missing", lambda *_: None)
    monkeypatch.setattr(agents, "_shell_ok", lambda: True)
    monkeypatch.setattr(config, "read_config", lambda *_: RunnerConfig())
    mono = [0]
    runner = FollowupRunner(w.service, clock=lambda: w.lab.ws.clock[0], mono=lambda: mono[0])

    def schedule():
        uid, key = w.ticket("Follow-up " + new_ulid())
        ok(w.conn().call("claim", {"ref": key}, grant=w.grant, session=SESSION, idem=True))
        due = w.lab.ws.clock[0] + 7200
        fid = "fu_" + new_ulid()
        w.host_append(
            lambda ws: (
                {
                    "type": "followup.set",
                    "actor": {**ws.agent, "session": SESSION},
                    "followup": fid,
                    "trigger": "time",
                    "due": ts(due),
                    "brief": "Check again",
                },
                uid,
            )
        )
        return uid, fid, due

    def view(uid):
        return w.h.run(w.service.call(lambda: w.service.store.ticket(uid))).followups[-1]

    return NS(
        w=w, hub=hub, runner=runner, mono=mono, schedule=schedule, view=view, tick=lambda: w.h.run(runner.tick_once())
    )


def test_due_once_and_secret_custody(env):
    e = env
    uid, fid, due = e.schedule()
    e.w.lab.ws.clock[0] = due
    e.tick()
    e.tick()
    assert e.view(uid)["state"] == "fired"
    put, spawn = e.hub.commands
    assert put[0] == "grant.put_run" and spawn[0] == "spawn"
    assert spawn[1]["run_grant"] and spawn[1]["workspace_id"] == e.w.wid
    assert fid in spawn[1]["argv"][-1]
    assert next(iter(e.hub.store.records.values()))["mode"] == "followup"
    secret = put[1]["secret"]
    assert secret not in json.dumps(spawn) + json.dumps(e.hub.store.records) + e.runner.path.read_text()
    assert e.runner.path.stat().st_mode & 0o777 == 0o600
    assert e.runner.path.parent.stat().st_mode & 0o777 == 0o700


def test_not_due(env):
    uid, _, _ = env.schedule()
    env.tick()
    assert env.view(uid)["state"] == "pending" and not env.hub.commands


def test_sleep_fires_late(env):
    uid, _, due = env.schedule()
    env.w.lab.ws.clock[0] = due + 3600
    env.mono[0] += 30
    env.tick()
    assert env.view(uid)["late_by"] == 3600
    assert env.runner.status()["slept_s"] > 3600


def test_past_window(env):
    uid, _, due = env.schedule()
    env.w.lab.ws.clock[0] = due + 86401
    env.tick()
    assert env.view(uid)["state"] == "did_not_run"
    assert env.view(uid)["reason"] == "no_host"
    assert not env.hub.commands


@pytest.mark.parametrize(
    "writes,code,expected,reason",
    [(True, 0, "ran", None), (False, 0, "failed", "no_writes"), (True, 1, "failed", "harness_failed")],
)
def test_ended(env, writes, code, expected, reason):
    uid, _, due = env.schedule()
    env.w.lab.ws.clock[0] = due
    env.tick()
    run = next(iter(env.hub.store.records.values()))
    if writes:
        env.w.host_append(
            lambda ws: (
                {
                    "type": "log.added",
                    "actor": {**ws.agent, "session": run["session"], "grant": run["grant"]},
                    "text": "resumed",
                },
                uid,
            )
        )
    run.update(state="exited", exit={"code": code})
    env.tick()
    assert env.view(uid)["state"] == expected
    assert env.view(uid).get("reason") == reason


def test_reconnect(env):
    uid, fid, due = env.schedule()
    env.w.lab.ws.clock[0] = due
    env.hub.connected = False
    env.tick()
    assert env.view(uid)["state"] == "pending"
    assert env.runner.status()["blocked"][fid] == "runner_unavailable"
    env.hub.connected = True
    env.tick()
    assert env.view(uid)["state"] == "fired"


def test_restart_after_fired(env, monkeypatch):
    uid, _, due = env.schedule()
    env.w.lab.ws.clock[0] = due

    async def crash(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(env.hub, "command", crash)
    import concurrent.futures

    with pytest.raises(concurrent.futures.CancelledError):
        env.tick()
    assert env.view(uid)["state"] == "fired"
    runner = FollowupRunner(env.w.service, clock=lambda: due, mono=lambda: 0)
    env.w.h.run(runner.tick_once())
    env.w.h.run(runner.tick_once())
    assert env.view(uid)["reason"] == "host_restarted"
    assert not env.hub.commands


def test_caps(env, monkeypatch):
    rows = [env.schedule() for _ in range(3)]
    env.w.lab.ws.clock[0] = max(r[2] for r in rows)
    env.tick()
    assert sum(env.view(uid)["state"] == "fired" for uid, _, _ in rows) == 2
    for run in env.hub.store.records.values():
        run.update(state="exited", exit={"code": 0})
    monkeypatch.setattr(rules, "FU_RUNS_PER_DAY", 2)
    env.tick()
    assert env.view(rows[-1][0])["state"] == "pending"
    assert len(env.hub.commands) == 4


def test_cancelled_and_needs_person(env):
    uid, fid, due = env.schedule()
    env.w.person_event("followup.cancelled", uid, followup=fid, reason="person")
    uid2, _, due2 = env.schedule()
    key = env.w.h.run(env.w.service.call(lambda: env.w.service.store.ticket(uid2))).key
    for _ in range(5):
        ok(env.w.conn().call("claim", {"ref": key}, grant=env.w.grant, session=SESSION, idem=True))
        env.w.host_append(
            lambda ws: (
                {
                    "type": "followup.set",
                    "actor": {**ws.agent, "session": SESSION},
                    "followup": "fu_" + new_ulid(),
                    "trigger": "time",
                    "due": ts(due2),
                    "brief": "again",
                },
                uid2,
            )
        )
    assert env.view(uid2)["state"] == "needs_person"
    env.w.lab.ws.clock[0] = max(due, due2)
    env.tick()
    assert env.view(uid)["state"] == "cancelled"
    assert not env.hub.commands


def test_start_limits_apply_to_a_follow_up(env):
    uid, _, due = env.schedule()
    env.hub.take_start = lambda person: False
    env.w.lab.ws.clock[0] = due
    env.tick()
    assert env.view(uid)["state"] == "did_not_run" and "spawn" not in [c[0] for c in env.hub.commands]


def test_a_host_that_is_not_the_workspace_host_never_fires(env, monkeypatch):
    uid, _, due = env.schedule()
    monkeypatch.setattr(env.w.service.store, "_host", None)
    env.w.lab.ws.clock[0] = due
    env.tick()
    assert env.view(uid)["state"] == "pending" and not env.hub.commands


def test_harness_comes_from_the_setters_person_not_another_persons_run(env):
    uid, _, due = env.schedule()
    env.hub.store.records["r_other"] = {
        "run": "r_other", "workspace_id": env.w.wid, "ticket": uid, "kind": "agent",
        "person": "p_" + "0" * 32, "harness": "codex", "created_at": "2999-01-01T00:00:00Z", "state": "exited",
    }  # fmt: skip
    env.w.lab.ws.clock[0] = due
    env.tick()
    fired = [r for r in env.hub.store.records.values() if r.get("mode") == "followup"]
    assert fired and fired[0]["harness"] != "codex"
