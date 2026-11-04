"""R2 T3 (#1590, D93): ``hub.switch redeem`` is refused for a workspace that runs at a customer's place (premises yes)
or whose hub is joined selectively, before the hub is asked, so the one-time ticket stays unused. The answer is read
from the model, never from the request."""

from __future__ import annotations

# ruff: noqa: F811 - pytest fixtures imported by name and used as arguments
import copy

import pytest

from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import err
from tests.host.test_hub_ops import CARD, connect_args, dev_workspace, join_args  # noqa: F401
from tests.host.test_hub_switch import ReplyHub, bound, hub  # noqa: F401 - the stub hub fixture
from tests.host.test_remote_policy import remote_args

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

REDEEM = {"kind": "redeem", "ticket": "w." + "b" * 32}
REPLY = {"switch_redeem": {"user_id": "u1", "name": "Ann", "return_to": "/", "kind": "open"}}


def redeems(h: ReplyHub) -> list:
    return [m for m in h.sent if m["o"]["op"] == "switch_redeem"]


def test_auth_switch_refused_selective(human, served, hub):
    bound(human, served, "selective")
    hub.replies = REPLY
    assert err(served.client().req("hub.switch", REDEEM, idem=False)) == "switch.refused"
    assert redeems(hub) == []


def test_auth_switch_refused_premises_yes(human, served, hub, monkeypatch):
    """Premises yes alone refuses, also once the hub is full (the full join ends remote access; the answer stays)."""
    from orch.host import orgstate

    monkeypatch.setattr(orgstate, "isolated", lambda service: True)
    bound(human, served, "selective")
    human.run("remote.set", remote_args(premises="yes"))
    human.run("hub.mode", {"hub_card": copy.deepcopy(CARD), "mode": "full"})
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    hub.replies = REPLY
    assert err(served.client().req("hub.switch", REDEEM, idem=False)) == "switch.refused"
    assert redeems(hub) == []


def test_ticket_not_used_when_refused(human, served, hub):
    """The refusal comes before the hub link: no ``switch_redeem`` is sent, so the hub's ticket is still unused and a
    later redeem (a full hub, premises no) goes through."""
    bound(human, served, "selective")
    hub.replies = REPLY
    n = len(hub.sent)
    assert err(served.client().req("hub.switch", REDEEM, idem=False)) == "switch.refused"
    assert len(hub.sent) == n  # nothing at all went to the hub, not even a status


def test_full_hub_premises_no_still_redeems(human, served, hub):
    bound(human, served, "full")
    hub.replies = REPLY
    r = ok(served.client().req("hub.switch", REDEEM, idem=False))
    assert r["data"]["user_id"] == "u1"
    assert len(redeems(hub)) == 1


@pytest.mark.parametrize("kind_args", [{"kind": "rail", "origin": "http://127.0.0.1:8000"}])
def test_rail_is_not_a_redeem(human, served, hub, kind_args):
    """Only a redeem opens the dashboard for a person; the rail list is unchanged by the D93 rule."""
    bound(human, served, "selective")
    hub.replies = {"rail_get": {"hub": {"name": "A", "origin": "https://h"}, "user_id": "u", "workspaces": []}}
    ok(served.client().req("hub.switch", kind_args, idem=False))
