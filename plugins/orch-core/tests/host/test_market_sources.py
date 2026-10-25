"""#1237 B: ``market.list`` names the marketplaces that can be added (``sources``), decided by the same gate as
``market.add`` (never a second rule). The dashboard's "+ Add marketplace" picker reads it; the host stays the judge."""

# ruff: noqa: F811
from __future__ import annotations

from orch.host import hubpolicy
from orch.market import constants
from tests.host import human_helpers
from tests.host import market_helpers as M
from tests.host.dispatch_helpers import Served
from tests.host.market_helpers import agent, err, ok, person
from tests.host.test_market_dev import URL as DEV_URL
from tests.host.test_market_dev import fakes, sandbox  # noqa: F401
from tests.host.test_market_hub_policy import ACME, Hub, _official
from tests.market import helpers as H

net = M.net
lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def listing(served, who=person):
    return ok(who(served, "market.list"))["data"]


def test_standalone_lists_only_the_official_marketplace_and_no_free_address(served, net):
    got = listing(served)
    assert got["sources"] == [{"url": constants.OFFICIAL_URL, "class": "official", "added": False}]
    assert got["any_source"] is False and "reason" not in got
    assert listing(served, agent)["sources"] == got["sources"]  # a read for P and G alike
    assert err(person(served, "market.add", {"url": "https://example.org/x/y"})) == "market.community_unisolated"
    assert net.index_calls == []


def test_joined_lists_exactly_what_the_hub_lists_and_the_unlisted_url_is_refused_with_no_fetch(
    served, net, monkeypatch
):
    h = Hub(monkeypatch, net)
    h.set(markets=[{"url": ACME, "class": "company", "keys": [H.KEY_B64]}])  # the official one is NOT listed
    got = listing(served)
    assert got["sources"] == [{"url": ACME, "class": "company", "added": False}]
    assert err(person(served, "market.add", {"url": constants.OFFICIAL_URL})) == "market.not_allowed"
    assert net.index_calls == [] and net.fetches == []


def test_joined_with_the_official_listed_gives_both(served, net, monkeypatch):
    h = Hub(monkeypatch, net)
    h.set(markets=[{"url": ACME, "class": "company", "keys": [H.KEY_B64]}, _official()])
    got = listing(served)
    assert {(s["url"], s["class"]) for s in got["sources"]} == {(ACME, "company"), (constants.OFFICIAL_URL, "official")}


def test_an_unreadable_policy_lists_nothing_and_says_why(served, net, monkeypatch):
    h = Hub(monkeypatch, net)
    h.pol = None  # stored() raises market.not_allowed
    got = listing(served)
    assert got["sources"] == [] and got["any_source"] is False and got["reason"] == "market.not_allowed"
    assert isinstance(hubpolicy.UNREADABLE, hubpolicy._Unreadable)  # noqa: SLF001


def test_added_marks_a_source_that_is_already_added(served, net):
    net.add("hello", "1.0.0")
    ok(person(served, "market.add", {"url": H.OFFICIAL}))
    assert listing(served)["sources"][0]["added"] is True


def test_the_dev_source_exists_only_on_a_dev_host_sandbox_workspace(tmp_path, fakes):
    lab = sandbox(tmp_path)
    try:
        served = Served(lab, dev=lab.dev())
        try:
            sources = listing(served)["sources"]
            assert {"url": DEV_URL, "class": "dev", "added": False} in sources
            assert sources[0]["class"] == "official"
        finally:
            served.close()
    finally:
        lab.close()
    from tests.host.lifecycle_helpers import Lab

    real = Lab(tmp_path / "real")
    try:
        served = Served(real)
        try:
            assert all(s["class"] != "dev" for s in listing(served)["sources"])
        finally:
            served.close()
    finally:
        real.close()


def test_an_agent_cannot_add_a_listed_source(served, net):
    assert err(agent(served, "market.add", {"url": constants.OFFICIAL_URL})) == "market.actor_refused"
    assert net.index_calls == []
