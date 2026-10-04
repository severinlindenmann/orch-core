import asyncio
import time
from datetime import datetime, timezone

import pytest

from addon_fixtures import Clock, loaded
from orch.addons import userfiles
from orch.addons.api import Snapshot
from orch.addons.loader import AddonRegistry
from orch.addons.scheduler import LONG_POLL_FETCH_CAP, LONG_POLL_RUN_CAP, Scheduler
from orch.testing import FakeRunner

T0 = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


class Inbox:
    id, kind, interval_s = "inbox", "status", 5

    def __init__(self, *, always_on=False, mode="interval"):
        self.always_on, self.mode, self.calls = always_on, mode, 0

    def scopes(self, ctx):
        return ["default"]

    def fetch(self, ctx, scope, previous):
        self.calls += 1
        ctx.run(["tool", "wait", "--timeout", "25"], timeout=120)
        return Snapshot(self.id, scope, T0, items=({"id": "a", "label": "x", "role": "ok", "text": "1"},))


class Obj:
    def __init__(self, p):
        self.providers = [p]


def _sched(ws, provider, *, live=False):
    runner = FakeRunner(strict=False)
    la = loaded(ws, Obj(provider), name="tixlike", capabilities=["provider"], binaries=["tool"], menu=None,
                settings_schema=[])
    la.ctx.runner = runner
    clock = Clock()
    return Scheduler(ws, AddonRegistry(ws, {"tixlike": la}), clock=clock, wall=lambda: T0, live=lambda: live), clock, runner


def test_always_on_waits_for_the_human_switch(ws):
    s, clock, _ = _sched(ws, Inbox(always_on=True))
    assert s.due() == []
    userfiles.set_background(ws.root, "tixlike", True)
    assert [k.provider for k in s.due()] == ["inbox"]


def test_switch_off_stops_it_again(ws):
    p = Inbox(always_on=True)
    userfiles.set_background(ws.root, "tixlike", True)
    s, clock, _ = _sched(ws, p)
    assert len(s.step()) == 1
    userfiles.set_background(ws.root, "tixlike", False)
    clock.t += 3600
    assert s.due() == [] and p.calls == 1


class Quiet(Inbox):
    id = "quiet"

    def scopes(self, ctx):
        self.calls += 1
        return ["default"]


def test_background_runs_no_code_of_other_providers(ws):
    on, off = Inbox(always_on=True), Quiet()
    userfiles.set_background(ws.root, "tixlike", True)
    la = loaded(ws, Obj(on), name="tixlike", capabilities=["provider"], binaries=["tool"], menu=None, settings_schema=[])
    la.obj.providers.append(off)
    la.ctx.runner = FakeRunner(strict=False)
    s = Scheduler(ws, AddonRegistry(ws, {"tixlike": la}), clock=Clock(), wall=lambda: T0, live=lambda: False)
    assert [k.provider for k in s.step()] == ["inbox"]
    assert off.calls == 0                               # not even its scopes()


def test_switch_does_nothing_for_providers_that_are_not_always_on(ws):
    userfiles.set_background(ws.root, "tixlike", True)
    s, _, _ = _sched(ws, Inbox(always_on=False))
    assert s.due() == []


def test_always_must_be_exactly_true(ws):
    p = Inbox()
    p.always_on = "yes"
    userfiles.set_background(ws.root, "tixlike", True)
    s, _, _ = _sched(ws, p)
    assert s.due() == []


def test_switch_of_another_workspace_does_not_count(ws, tmp_path):
    userfiles.set_background(tmp_path / "elsewhere", "tixlike", True)
    s, _, _ = _sched(ws, Inbox(always_on=True))
    assert s.due() == []


def test_interval_always_on_keeps_its_interval(ws):
    p = Inbox(always_on=True)
    userfiles.set_background(ws.root, "tixlike", True)
    s, clock, runner = _sched(ws, p)
    assert len(s.step()) == 1
    assert s.step() == []
    clock.t += 5
    assert len(s.step()) == 1
    assert runner.timeouts == [120.0, 120.0]   # interval providers are not capped


def test_long_poll_is_due_again_right_after_a_good_fetch(ws):
    p = Inbox(always_on=True, mode="long_poll")
    userfiles.set_background(ws.root, "tixlike", True)
    s, clock, _ = _sched(ws, p)

    def fetch(ctx, scope, previous):
        p.calls += 1
        clock.t += 6  # the provider genuinely waited for news before returning (over the 5 s floor)
        return Snapshot(p.id, scope, T0, items=())
    p.fetch = fetch

    assert len(s.step()) == 1
    assert len(s.step()) == 1 and p.calls == 2          # no interval wait in between


def test_long_poll_fetch_under_the_floor_waits_the_interval(ws):
    """A long-poll fetch that returns almost instantly every time (a misbehaving or abusive endpoint that never
    actually blocks for news) must not be hammered in a tight loop: below a 5 s floor it waits like an interval
    provider instead of being scheduled again at once."""
    p = Inbox(always_on=True, mode="long_poll")  # the default fetch doesn't advance the clock: elapsed == 0
    userfiles.set_background(ws.root, "tixlike", True)
    s, clock, _ = _sched(ws, p)
    assert len(s.step()) == 1
    assert s.step() == []                               # under the floor: not due again yet
    clock.t += p.interval_s
    assert len(s.step()) == 1 and p.calls == 2


def test_long_poll_backs_off_after_a_failure(ws):
    p = Inbox(always_on=True, mode="long_poll")
    p.fetch = lambda ctx, scope, previous: (_ for _ in ()).throw(RuntimeError("offline"))
    userfiles.set_background(ws.root, "tixlike", True)
    s, clock, _ = _sched(ws, p)
    s.step()
    assert s.step() == []
    clock.t += 11
    assert len(s.step()) == 1


def test_long_poll_run_timeout_is_capped(ws):
    p = Inbox(always_on=True, mode="long_poll")
    userfiles.set_background(ws.root, "tixlike", True)
    s, _, runner = _sched(ws, p)
    s.step()
    assert runner.timeouts[-1] == LONG_POLL_RUN_CAP


def test_long_poll_is_single_flight(ws):
    p = Inbox(always_on=True, mode="long_poll")
    userfiles.set_background(ws.root, "tixlike", True)
    s, _, _ = _sched(ws, p)
    [key] = s.due()
    s._st(key).running = True                       # run_forever marks it before the fetch starts
    assert s.due() == []


def test_long_poll_fetch_is_cut_off_at_the_fetch_cap(ws, monkeypatch):
    p = Inbox(always_on=True, mode="long_poll")
    userfiles.set_background(ws.root, "tixlike", True)
    s, _, _ = _sched(ws, p)
    limits = []

    async def fake_wait_for(aw, timeout):
        limits.append(timeout)
        aw.cancel() if hasattr(aw, "cancel") else None
        raise asyncio.TimeoutError
    import orch.addons.scheduler as sched_mod
    monkeypatch.setattr(sched_mod.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(s, "run_job", lambda key: time.sleep(0))
    [key] = s.due()
    asyncio.run(s._run_async(key))
    assert limits == [LONG_POLL_FETCH_CAP] and LONG_POLL_FETCH_CAP == 40.0 and LONG_POLL_RUN_CAP == 35.0


def test_provider_context_caps_the_timeout(ws):
    from orch.addons.api import AddonContext
    from orch.addons.manifest import parse_manifest
    from addon_fixtures import GOOD
    runner = FakeRunner(strict=False)
    ctx = AddonContext(ws, "demo", manifest=parse_manifest({**GOOD, "name": "demo"}), runner=runner)
    ctx.provider_context(max_timeout=35.0).run(["git", "status"], timeout=120)
    ctx.provider_context(max_timeout=35.0).run(["git", "status"], timeout=10)
    ctx.provider_context().run(["git", "status"], timeout=120)
    assert runner.timeouts == [35.0, 10.0, 120.0]


def test_background_flag_survives_other_writes(ws):
    userfiles.set_background(ws.root, "tixlike", True)
    userfiles.set_enabled(ws.root, "tixlike", True)
    userfiles.save_addon_config(ws.root, "tixlike", {"a": 1})
    assert userfiles.workspace_addons(ws.root)["tixlike"]["background"] is True


def test_background_defaults_to_off(ws):
    userfiles.set_enabled(ws.root, "tixlike", True)
    assert userfiles.workspace_addons(ws.root)["tixlike"]["background"] is False


def test_background_post_is_human_and_same_origin(ws, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    c = TestClient(create_app(ws, "tok"))
    c.get("/?token=tok")
    assert c.post("/workspace/addons/tixlike/background", data={"on": "1"}, follow_redirects=False).status_code == 403
    r = c.post("/workspace/addons/tixlike/background", data={"on": "1"}, headers={"origin": "http://testserver"},
               follow_redirects=False)
    assert r.status_code == 303 and userfiles.workspace_addons(ws.root)["tixlike"]["background"] is True
    r = c.post("/workspace/addons/tixlike/background", data={"on": "0"}, headers={"origin": "http://testserver"},
               follow_redirects=False)
    assert userfiles.workspace_addons(ws.root)["tixlike"]["background"] is False


def test_background_post_needs_the_token(ws):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    c = TestClient(create_app(ws, "tok"))
    r = c.post("/workspace/addons/tixlike/background", data={"on": "1"}, headers={"origin": "http://testserver"},
               follow_redirects=False)
    assert r.status_code == 401 and "tixlike" not in userfiles.workspace_addons(ws.root)


def test_workspace_row_shows_the_switch_only_for_always_on_providers(ws, monkeypatch):
    from fastapi.testclient import TestClient
    from orch.dashboard import views
    from orch.dashboard.app import create_app
    monkeypatch.setattr(views, "_setup_count", lambda ws, checks=None: 0)
    from orch.addons.discovery import Found
    import orch.dashboard.routes_workspace as rw
    p = Inbox(always_on=True, mode="long_poll")
    la = loaded(ws, Obj(p), name="tixlike", capabilities=["provider"], binaries=["tool"], menu=None, settings_schema=[])
    ws._addons = AddonRegistry(ws, {"tixlike": la})
    monkeypatch.setattr(rw, "discover", lambda: [Found("tixlike", "default", ws.root, la.manifest)])
    monkeypatch.setattr(userfiles, "trust_state", lambda f: "trusted")
    userfiles.set_enabled(ws.root, "tixlike", True)
    c = TestClient(create_app(ws, "tok"))
    c.get("/?token=tok")
    c.app.state.addons.reload = lambda: None
    html = c.get("/workspace").text
    assert 'action="/workspace/addons/tixlike/background"' in html
    assert "Keep syncing while Mission Control runs" in html
    assert "Off: it syncs only while a Mission Control tab is open." in html
    p.always_on = False
    assert 'action="/workspace/addons/tixlike/background"' not in c.get("/workspace").text
