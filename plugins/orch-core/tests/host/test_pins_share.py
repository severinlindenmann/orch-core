"""H1 L4: ``AgentPins.share_for`` and the pin classes (spec 13.1, 13.3)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from orch.host._unix import UnsafePath
from orch.host.pins import AgentPins, AgentShare
from tests.host.tier_b_helpers import AGENT_PIN, AGENT_UID, UID, WS, Names, write_pin

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="Unix sockets")


def test_share_for_reads_the_root_pin_and_the_user_database(pins_dir):
    write_pin(pins_dir, AGENT_UID, AGENT_PIN)
    found = Names().share(AgentPins(pins_dir, owner=UID))
    assert found == AgentShare(
        UID,
        AGENT_UID,
        AGENT_UID,
        "orch-agent-sev",
        "sev",
        Path("/run/orch/agents/orch-agent-sev"),
        "orch-runner@sev.service",
    )


def test_share_for_is_tier_a_without_a_pin(pins_dir, tree):
    assert Names().share(AgentPins(pins_dir, owner=UID)) is None
    assert Names().share(AgentPins(tree / "nothing", owner=UID)) is None
    write_pin(pins_dir, 1234, {"class": "agent", "host_uid": UID + 1, "workspaces": "*"})  # another person's agent
    write_pin(pins_dir, 1235, {"class": "worker", "host_uid": UID, "workspaces": "*"})
    write_pin(pins_dir, 1236, {"class": "addon", "host_uid": UID, "workspaces": "*"})
    assert Names().share(AgentPins(pins_dir, owner=UID)) is None


@pytest.mark.parametrize(
    "names",
    [
        Names(agent_name="orch-agent-bob"),  # the pin names a user that is not orch-agent-<person>
        Names(agent_members=["sev"]),  # the person (or anyone) in the agent's group
        Names(other_in_group=True),  # another user with that primary group
    ],
)
def test_share_for_refuses_an_agent_that_is_not_exactly_the_naming_rule(pins_dir, names):
    write_pin(pins_dir, AGENT_UID, AGENT_PIN)
    with pytest.raises(UnsafePath):
        names.share(AgentPins(pins_dir, owner=UID))


def test_share_for_refuses_two_agents_one_unreadable_pin_or_a_missing_user(pins_dir):
    write_pin(pins_dir, AGENT_UID, AGENT_PIN)
    pins = AgentPins(pins_dir, owner=UID)
    write_pin(pins_dir, AGENT_UID + 1, AGENT_PIN)
    with pytest.raises(UnsafePath, match="more than one"):
        Names().share(pins)
    os.unlink(pins_dir / f"{AGENT_UID + 1}.json")
    write_pin(pins_dir, 4321, {"class": "agent", "host_uid": UID + 1, "workspaces": "*"}, mode=0o666)  # writable by all
    with pytest.raises(UnsafePath, match="trusted pin"):
        Names().share(pins)
    os.unlink(pins_dir / "4321.json")
    write_pin(pins_dir, 4322, {"class": "robot", "host_uid": UID + 1})  # an unknown class is never "no pin"
    with pytest.raises(UnsafePath, match="trusted pin"):
        Names().share(pins)
    os.unlink(pins_dir / "4322.json")
    os.unlink(pins_dir / f"{AGENT_UID}.json")
    write_pin(pins_dir, AGENT_UID + 7, AGENT_PIN)  # pinned UID that does not exist
    with pytest.raises(UnsafePath, match="user database"):
        Names().share(pins)


def test_addon_pin_is_pinned_but_names_no_workspace(pins_dir):
    write_pin(pins_dir, 777, {"class": "addon", "host_uid": UID, "workspaces": "*"})
    pins = AgentPins(pins_dir, owner=UID)
    assert pins.pinned(777) is True  # never a person
    assert pins.names_workspace(777, WS) is False  # and never an agent of a workspace
    write_pin(pins_dir, 778, {"class": "agent", "host_uid": UID, "workspaces": "*"})
    assert pins.names_workspace(778, WS) is True
