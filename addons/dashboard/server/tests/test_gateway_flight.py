"""A page the person reloads leaves requests behind; the workspace list they ask for must not pile up (owner run 3)."""

import threading
import time

from orch_dashboard.gateway import NextFlight


def test_callers_during_a_run_share_one_next_run():
    started, release, runs = threading.Event(), threading.Event(), []

    def work():
        runs.append(len(runs) + 1)
        started.set()
        release.wait(5)
        return len(runs)

    flight = NextFlight(work)
    out = []
    first = threading.Thread(target=lambda: out.append(flight()))
    first.start()
    assert started.wait(5)
    waiters = [threading.Thread(target=lambda: out.append(flight())) for _ in range(30)]
    for t in waiters:
        t.start()
    time.sleep(0.2)
    release.set()
    for t in [first, *waiters]:
        t.join(5)
        assert not t.is_alive()
    assert len(runs) == 2  # the run under way, and ONE more for the 30 who arrived meanwhile
    assert sorted(out) == [1] + [2] * 30


def test_a_late_caller_never_gets_an_older_run_than_its_own():
    seen = []
    flight = NextFlight(lambda: seen.append(1) or len(seen))
    assert flight() == 1
    assert flight() == 2  # not the first run's answer


def test_an_error_reaches_everyone_who_shared_the_run_and_the_next_run_is_fresh():
    gate, calls = threading.Event(), []

    def work():
        calls.append(1)
        gate.wait(5)
        if len(calls) == 1:
            raise RuntimeError("host busy")
        return "fine"

    flight = NextFlight(work)
    errors = []

    def ask():
        try:
            flight()
        except RuntimeError as exc:
            errors.append(str(exc))

    t = threading.Thread(target=ask)
    t.start()
    time.sleep(0.1)
    gate.set()
    t.join(5)
    assert errors == ["host busy"]
    assert flight() == "fine"


def test_gateway_asks_the_host_through_the_flight(monkeypatch):
    from orch_dashboard.gateway import Gateway

    gateway = Gateway.__new__(Gateway)
    monkeypatch.setattr(Gateway, "_workspaces", lambda self: ["ws"])
    assert gateway.workspaces() == ["ws"]
    assert isinstance(gateway.__dict__["_workspace_flight"], NextFlight)
