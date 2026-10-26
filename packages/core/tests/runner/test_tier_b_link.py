"""H1 L4: the runner finds its host in tier B, and gives up after repeated refusals."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orch.host.pins import AgentPins
from tests.host.tier_b_helpers import UID, write_pin


def test_runner_endpoint_tier_a_is_its_own_run_directory(pins_dir, tree):
    from orch.runner.cli import _host_endpoint

    pins = AgentPins(pins_dir, owner=UID)
    assert _host_endpoint(UID, tree / "r", pins=pins) == (UID, tree / "r" / "runner" / "host.sock")


def test_runner_endpoint_tier_b_is_the_persons_runner_socket(pins_dir, monkeypatch):
    import pwd

    from orch.host.listener import default_run_directory
    from orch.runner.cli import _host_endpoint

    write_pin(pins_dir, UID, {"class": "agent", "host_uid": UID + 1, "workspaces": "*"})  # this process plays the agent
    pins = AgentPins(pins_dir, owner=UID)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="orch-agent-sev"))
    run = Path("/run/orch/agents/orch-agent-sev")
    assert _host_endpoint(UID, run, pins=pins) == (UID + 1, default_run_directory(UID + 1) / "runner" / "host.sock")
    for bad in (Path("/run/orch/agents/orch-agent-bob"), Path("/run/orch/1000"), Path("/tmp/run")):
        with pytest.raises(SystemExit):
            _host_endpoint(UID, bad, pins=pins)


@pytest.mark.parametrize(
    "doc",
    [
        {"class": "worker", "host_uid": 5},
        {"class": "agent"},
        {"class": "agent", "host_uid": "5"},
        {"class": "agent", "host_uid": 0},
    ],
)
def test_runner_endpoint_refuses_a_pin_that_is_not_a_usable_agent_pin(pins_dir, doc):
    from orch.runner.cli import _host_endpoint

    write_pin(pins_dir, UID, doc)
    with pytest.raises(SystemExit):
        _host_endpoint(UID, Path("/run/orch/agents/orch-agent-sev"), pins=AgentPins(pins_dir, owner=UID))


def test_runner_endpoint_refuses_when_the_pin_is_untrusted(pins_dir):
    from orch.runner.cli import _host_endpoint

    write_pin(pins_dir, UID, {"class": "agent", "host_uid": UID + 1}, mode=0o666)
    with pytest.raises(SystemExit):
        _host_endpoint(UID, Path("/run/orch/agents/orch-agent-sev"), pins=AgentPins(pins_dir, owner=UID))


def test_the_runner_gives_up_after_three_denials_in_a_row():
    from orch.host.client import Refused
    from orch.runner.link import DENIED_LIMIT, HostLink

    gave_up = []
    link = HostLink.__new__(HostLink)
    link._denied, link._on_denied = 0, lambda: gave_up.append(1)
    for _ in range(DENIED_LIMIT - 1):
        link._count_denial(Refused("peer.denied"))
    assert gave_up == []
    link._count_denial(ConnectionError("down"))  # another failure breaks the run
    for _ in range(DENIED_LIMIT - 1):
        link._count_denial(Refused("peer.denied"))
    assert gave_up == []
    link._count_denial(Refused("peer.denied"))
    assert gave_up == [1] and DENIED_LIMIT == 3
