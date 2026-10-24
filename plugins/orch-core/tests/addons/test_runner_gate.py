"""V3 G7 (#928): the runner calls the host's hub-policy gate before anything is staged or started, so a policy that
refuses an add-on pauses it ("paused by hub policy") and undoing the policy un-pauses it, with the grant untouched."""

from __future__ import annotations

import pytest

from orch.addons import runner
from orch.addons.package import read_package
from orch.addons.runner import RunnerError, propose
from orch.market import MarketError
from tests.addons.helpers import make_package
from tests.addons.test_runner import TICKET, granted_for


@pytest.fixture
def pkg(tmp_path):
    package = read_package(make_package(tmp_path))
    return package, granted_for(package)


def test_a_gate_that_refuses_pauses_the_run_before_anything_starts(pkg, monkeypatch):
    package, granted = pkg
    started = []
    monkeypatch.setattr(runner.tempfile, "mkdtemp", lambda *a, **k: started.append(a) or "/nonexistent")
    seen = []

    def gate(g):
        seen.append(g.name)
        raise MarketError("market.blocked")

    with pytest.raises(RunnerError) as e:
        propose(package, granted, trigger="manual", ticket=TICKET, gate=gate)
    assert (e.value.code, e.value.detail) == ("market.blocked", "paused by hub policy")
    assert seen == ["echo"] and started == []  # nothing was staged, nothing was started


def test_a_gate_that_allows_runs_as_before_and_no_gate_is_a_host_without_a_hub(pkg):
    package, granted = pkg
    calls = []
    propose(package, granted, trigger="manual", ticket=TICKET, gate=lambda g: calls.append(g.name))
    propose(package, granted, trigger="manual", ticket=TICKET)
    assert calls == ["echo"]


def test_the_gate_is_asked_on_every_run_not_once(pkg):
    package, granted = pkg
    state = {"blocked": True}

    def gate(g):
        if state["blocked"]:
            raise MarketError("market.capability_ceiling")

    with pytest.raises(RunnerError) as e:
        propose(package, granted, trigger="manual", ticket=TICKET, gate=gate)
    assert e.value.code == "market.capability_ceiling"
    state["blocked"] = False  # the policy change is undone: the same grant runs again
    propose(package, granted, trigger="manual", ticket=TICKET, gate=gate)
