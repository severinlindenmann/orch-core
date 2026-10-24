"""H1 L3: ``runner.restart``, the host asking a tier-B runner to restart itself on the new release."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from orch.host.errors import HostError
from orch.host.runners import RunnerHub
from orch.runner.restart import RestartGate
from tests.host.test_runner_socket import ME, Runner, rstate, w, wait_for  # noqa: F401  # noqa: F401  (fixtures)


class Clock:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def test_an_idle_runner_ends_after_it_answered() -> None:
    stop, clock = threading.Event(), Clock()
    gate = RestartGate(lambda: 0, stop, sleep=clock.sleep)
    assert gate.request() == {"restarting": True, "live": 0, "force": False}
    gate._thread.join(5)
    assert stop.is_set() and clock.slept == [0.5]  # a delay, so the answer reaches the host first


def test_a_busy_runner_waits_until_its_runs_end_and_force_ends_it_at_once() -> None:
    stop, clock, live, go = threading.Event(), Clock(), [2], threading.Event()

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        go.wait(5)

    gate = RestartGate(lambda: live[0], stop, poll=0.1, sleep=sleep)
    assert gate.request()["live"] == 2
    assert not stop.is_set()
    live[0] = 0  # the runs ended
    go.set()
    gate._thread.join(5)
    assert stop.is_set() and clock.slept == [0.1, 0.5]

    stop2, clock2 = threading.Event(), Clock()
    gate2 = RestartGate(lambda: 5, stop2, sleep=clock2.sleep)
    assert gate2.request(force=True) == {"restarting": True, "live": 5, "force": True}
    gate2._thread.join(5)
    assert stop2.is_set()


def test_a_second_request_only_upgrades_to_force_and_starts_no_second_thread() -> None:
    stop, release = threading.Event(), threading.Event()
    gate = RestartGate(lambda: 1, stop, sleep=lambda s: release.wait(5))
    gate.request()
    first = gate._thread
    assert gate.request(force=True)["force"] is True and gate._thread is first
    release.set()
    first.join(5)
    assert stop.is_set()


def test_the_command_reaches_the_runner_through_the_host(w, rstate) -> None:  # noqa: F811
    r = Runner(w, rstate)
    try:
        asked: list[bool] = []
        r.link._restart = lambda force: asked.append(force) or {"restarting": True, "live": 0, "force": force}
        res = w.h.run(w.h.host.runners.restart_runner(force=True))
        assert res == {"restarting": True, "live": 0, "force": True} and asked == [True]
        res = w.h.run(w.h.host.runners.restart_runner())
        assert asked == [True, False]
    finally:
        r.close()


def test_a_runner_that_cannot_restart_says_so(w, rstate) -> None:  # noqa: F811
    r = Runner(w, rstate)
    try:
        with pytest.raises(HostError):  # the host maps a refused runner command to its generic code
            w.h.run(w.h.host.runners.restart_runner())
    finally:
        r.close()


def test_tier_a_never_asks_for_a_restart_even_when_the_release_differs(w, rstate) -> None:  # noqa: F811
    r = Runner(w, rstate, release="old000000000")  # same UID as the host: the person's service manager restarts it
    try:
        asked: list[bool] = []
        r.link._restart = lambda force: asked.append(force) or {}
        assert w.h.host.runners.release == "old000000000"
        asyncio.run(asyncio.sleep(0.3))
        assert asked == []
    finally:
        r.close()


def _hub(uid, release, host_uid=1000) -> tuple[RunnerHub, list[bool]]:
    hub = RunnerHub.__new__(RunnerHub)
    hub.uid, hub.release, hub.host = uid, release, SimpleNamespace(host_uid=host_uid)
    hub._restart_asked = None
    asked: list[bool] = []

    async def restart(*, force=False):
        asked.append(force)
        return {}

    hub.restart_runner = restart  # type: ignore[method-assign]
    return hub, asked


async def _settle(hub: RunnerHub) -> None:
    hub._restart_if_stale()
    await asyncio.sleep(0.05)


def test_a_stale_tier_b_runner_is_asked_once_per_release(monkeypatch) -> None:
    monkeypatch.setattr("orch.host.service._release_id", lambda: "new")
    hub, asked = _hub(uid=990, release="old")

    async def go() -> None:
        await _settle(hub)
        await _settle(hub)  # it came back on a release the host still calls stale: no loop

    asyncio.run(go())
    assert asked == [False]


@pytest.mark.parametrize(("uid", "release"), [(990, "new"), (1000, "old"), (None, "old")])
def test_no_restart_for_a_current_runner_a_tier_a_runner_or_no_runner(monkeypatch, uid, release) -> None:
    monkeypatch.setattr("orch.host.service._release_id", lambda: "new")
    hub, asked = _hub(uid=uid, release=release)
    asyncio.run(_settle(hub))
    assert asked == []


def test_a_runner_that_predates_the_release_field_is_asked_to_restart(monkeypatch) -> None:
    monkeypatch.setattr("orch.host.service._release_id", lambda: "new")
    hub, asked = _hub(uid=990, release=None)
    asyncio.run(_settle(hub))
    assert asked == [False]


def test_a_refused_restart_is_logged_not_raised(monkeypatch) -> None:
    hub, _ = _hub(uid=990, release="old")

    async def refuse(*, force=False):
        raise HostError("runner.unavailable", "no")

    hub.restart_runner = refuse  # type: ignore[method-assign]
    asyncio.run(hub._ask_restart(False))
