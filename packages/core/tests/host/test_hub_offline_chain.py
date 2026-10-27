# ruff: noqa: F811
"""A1 (D83): a workspace joined in Full mode keeps working with the hub down, and the hub sees the changes when it is
back. The hub is the test-side ``LogHub`` (the real ``log_sync`` rules), switched off and on."""

from __future__ import annotations

import pytest

from orch.host import hubops, logsyncops
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.test_hub_ops import connect_args, dev_workspace, join_args, keystore, stub  # noqa: F401
from tests.host.test_logsync_ops import local_summary, loghub, until  # noqa: F401

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture
def switchable(loghub):  # noqa: F811
    real = loghub.send
    state = {"down": False}

    def send(message):
        if state["down"]:
            raise OSError("hub is down")
        return real(message)

    loghub.send = send
    return state


def test_every_workspace_action_works_with_the_hub_down_and_the_hub_catches_up(human, served, stub, switchable, loghub):
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    uid, key = human.ticket("before the outage")
    assert until(lambda: loghub.logs() == local_summary(served))
    assert hubops.signing_mode(served.service) == "full"

    switchable["down"] = True  # the hub is stopped
    uid2, key2 = human.ticket("made while the hub is down")  # a new ticket: a local signature
    human.run("log", {"ref": key, "text": "a note while offline"})
    human.run("settings.set", {"set": {"grant_hours": 9}})  # a workspace setting
    logsyncops.wake(served.service)
    assert loghub.logs() != local_summary(served)  # the hub saw none of it

    switchable["down"] = False  # the hub is back
    logsyncops.wake(served.service)
    assert until(lambda: loghub.logs() == local_summary(served), timeout=30)
    assert uid2 in loghub.view.proj
