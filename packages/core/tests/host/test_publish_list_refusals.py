"""Run 14 (P02, J08-01 / J07-03): the refusal codes `publish.list {live}` answers when sharing cannot be asked. The
dashboard's Shares page turns each of them into one plain line (orch_dashboard.shares), so they are pinned here."""

from __future__ import annotations

from orch.host import hubpolicy
from tests.host import human_helpers
from tests.host.dispatch_helpers import err, ok
from tests.host.test_hub_ops import connect_args, join_args
from tests.host.test_publishing import World

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def test_publish_off_is_runner_disabled(served, human, monkeypatch):
    bare = World(served, human, monkeypatch)
    assert err(bare.req("publish.list", {"live": True})) == "runner.disabled"


def test_a_hub_pause_is_the_same_code_with_the_reason_in_the_words(served, human, monkeypatch):
    w = World(served, human, monkeypatch)
    w.addon()
    w.configure()
    monkeypatch.setattr(hubpolicy, "running", lambda service, name: False)
    res = w.req("publish.list", {"live": True})
    assert err(res) == "runner.disabled"


def test_no_server_set_up_is_an_answer(served, human, monkeypatch):
    w = World(served, human, monkeypatch)
    w.addon()
    assert ok(w.req("publish.list", {"live": True}))["data"]["configured"] is False


def test_joined_and_not_connected_is_hub_not_joined(served, human, monkeypatch):
    w = World(served, human, monkeypatch, join=False)
    w.addon()
    w.configure()
    human.run("hub.join", join_args())
    assert err(w.req("publish.list", {"live": True})) == "hub.not_joined"


def test_joined_in_full_mode_is_hub_state_mismatch(served, human, monkeypatch):
    """The J08 situation: shared spaces need full mode, and publishing works in selective mode only."""
    w = World(served, human, monkeypatch, join=False)
    w.addon()
    w.configure()
    human.run("hub.join", join_args(mode="full"))
    ok(served.client().req("hub.connect", connect_args(), idem=True))
    assert err(w.req("publish.list", {"live": True})) == "hub.state_mismatch"
