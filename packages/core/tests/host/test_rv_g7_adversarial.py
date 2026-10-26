"""Opus review G7 (#1213), adversarial. Each test asserts the SAFE behaviour; a failure documents a finding."""

# ruff: noqa: F811
from __future__ import annotations

from types import SimpleNamespace

import pytest

from orch.host import hubpolicy, publishing, worktrees
from orch.host.errors import HostError
from orch.market import ADDONS, MarketError, policy
from tests.host import human_helpers
from tests.host import market_helpers as M
from tests.host.market_helpers import install, mfiles, ok, person
from tests.host.test_market_hub_policy import Hub, raw_policy  # noqa: F401
from tests.market.test_policy_vectors import ACME, COMPANY, K1, _pol

net = M.net
lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture
def ready(served, net):
    net.add("standup", "1.0.0")
    M.add_market(served, net)
    return net


@pytest.fixture
def hub(ready, monkeypatch):
    return Hub(monkeypatch, ready)


def _row(served, name):
    rows = ok(person(served, "addon.list"))["data"]["addons"]
    return next(r for r in rows if r["name"] == name)


# F1: addon.list is what the dashboard (gateway.enabled / addon_on) reads; a paused add-on must not read as active.
def test_F1_addon_list_shows_a_blocked_addon_as_not_active(served, human, hub, ready):
    install(served, human)
    assert _row(served, "standup")["enabled"] is True
    hub.set(block=["standup"])
    assert M.on_worker(served, lambda: hubpolicy.running(served.service, "standup")) is False  # host features: off
    row = _row(served, "standup")
    assert row["enabled"] is False and row["state"] != "active", row  # the dashboard still sees it on


def test_F1_addon_list_on_an_unverifiable_hub_shows_nothing_active(served, human, hub, ready):
    install(served, human)
    hub.pol = None  # card expired: "every add-on is paused, the dashboard included"
    row = _row(served, "standup")
    assert row["enabled"] is False, row


# F2: the worktrees built-in gate (dashboard execution) is a sibling of publishing._gate and was not routed.
def _fake_call(name):
    hubstub = SimpleNamespace(runner_state=lambda ws: {"enabled": True}, connected=True, require_current=lambda: None)
    addons = {name: {"enabled": True, "purged": False, "version": "0.1.0", "capabilities": []}}
    service = SimpleNamespace(
        store=SimpleNamespace(state=SimpleNamespace(workspace=SimpleNamespace(addons=addons))),
        runners=hubstub, workspace_id="w",
    )  # fmt: skip
    return SimpleNamespace(service=service)


def test_F2_a_blocked_worktrees_builtin_is_off(monkeypatch):
    pol = _pol(block=["publish", "worktrees"])
    monkeypatch.setattr(hubpolicy, "stored", lambda service: pol)
    from orch.host import marketinstall

    monkeypatch.setattr(marketinstall, "_files", lambda s: SimpleNamespace(installs=lambda: {ADDONS: {}}))
    with pytest.raises(HostError):
        publishing._gate(_fake_call("publish"))  # routed: refused
    with pytest.raises(HostError):
        worktrees._gate(_fake_call("worktrees"))  # not routed: passes


# F3: the reconnect hint names a command the CLI does not have.
def test_F3_the_reconnect_hint_is_a_real_command():
    """The hint parses with the real ``orch hub`` parser (it has no --help, so the parser is asked directly)."""
    from orch.cli.hub import _parser

    for hint in (
        hubpolicy.view(hubpolicy.UNREADABLE)["reconnect"],
        hubpolicy.RECONNECT,
        hubpolicy.UNVERIFIED["reconnect"],
    ):
        words = hint.split()
        assert words[:2] == ["orch", "hub"], hint
        ns = _parser().parse_args(words[2:])
        assert ns.cmd == "status" and ns.refresh is True, hint


# F4: a marketplace add-on whose install record is gone is judged as a built-in (block + ceiling only).
def test_F4_a_deleted_install_record_does_not_turn_a_marketplace_addon_into_a_builtin(served, human, hub, ready):
    install(served, human)
    hub.set(markets=[])  # its origin is no longer listed: paused
    on = lambda: M.on_worker(served, lambda: hubpolicy.running(served.service, "standup"))  # noqa: E731
    assert on() is False
    f = mfiles(served)
    with f.commit_installs() as write:
        cur = f.installs()
        cur[ADDONS].pop("standup")
        write(cur)
    assert on() is False  # a non-built-in grant with no record must not run on a joined host


# F5: the shared run vector "company market listed ok" carries no publisher key; core refuses that shape.
def test_F5_run_stage_without_a_key_for_a_company_origin_is_refused_by_evaluate_itself():
    with pytest.raises(MarketError):
        policy.evaluate(
            "run", _pol(COMPANY),
            {"url": ACME, "id": "acme-tool", "version": "1.0.0", "capabilities": [], "publisher_key": None},
        )  # fmt: skip
    policy.check_run(_pol(COMPANY), name="acme-tool", version="1.0.0", capabilities=[], origin=ACME, key=K1)
