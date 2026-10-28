"""V3 G7 (#928): the host enforces the hub's marketplace policy. The pinned-snapshot verification is the D14 pair's
(``tests/hub``); here the policy is injected where the host reads it (``hubpolicy.stored``) and the fresh read is a
recording fake, so what is tested is the ORDER (a fresh read before any network access), the refusals at every stage,
the re-check at the grant under the store lock, the company marketplace (the pin replaces TOFU), the pause, and the
joined-but-unverifiable rule."""

# ruff: noqa: F811
from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from orch.crypto.encoding import b64u
from orch.host import hubpolicy, marketops
from orch.hubclient import HubRefused
from orch.market import MarketError, constants, policy
from tests.host import human_helpers
from tests.host import market_helpers as M  # noqa: E402
from tests.host.market_helpers import (
    add_market,
    addon_dir,  # noqa: F401
    err,
    install,
    mfiles,
    ok,
    person,
    stage,
)  # fmt: skip
from tests.market import helpers as H

net = M.net
lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
ACME = "https://git.acme.example/tools/orch-market"
OTHER_PRIV = ec.generate_private_key(ec.SECP256R1())
OTHER_B64 = b64u(OTHER_PRIV.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


def raw_policy(*, markets=None, allow=None, block=(), ceiling=None):
    if markets is None:
        markets = [{"url": constants.OFFICIAL_URL, "class": "official", "keys": []}]
    return {
        "markets": sorted(markets, key=lambda m: m["url"]),
        "addons": {"allow": allow, "block": sorted(block)},
        "ceiling": ceiling,
    }


class Hub:
    """The host's view of a joined hub: what ``stored`` returns, what the fresh read does, and the order it ran in."""

    def __init__(self, monkeypatch, net):
        self.net, self.pol, self.fresh_error, self.reads = net, None, None, []
        self.set()
        monkeypatch.setattr(marketops, "hub_joined", lambda service: True)
        monkeypatch.setattr(hubpolicy, "stored", self.stored)
        monkeypatch.setattr(hubpolicy, "refresh_now", self.refresh_now)

    def set(self, **kw):
        self.pol = policy.from_raw(raw_policy(**kw))

    def stored(self, service):
        if self.pol is None:
            raise MarketError("market.not_allowed")
        return self.pol

    def refresh_now(self, call, dev):
        self.reads.append((len(self.net.index_calls), len(self.net.fetches)))
        return self.fresh_error


@pytest.fixture
def ready(served, net):
    net.add("standup", "1.0.0")
    add_market(served, net)
    return net


@pytest.fixture
def hub(ready, monkeypatch):
    return Hub(monkeypatch, ready)


# ------------------------------------------------------------------------------------------------ order, fresh read
def test_the_fresh_snapshot_is_read_before_any_network_access(served, hub, ready):
    before = (len(ready.index_calls), len(ready.fetches))
    ok(stage(served))
    assert hub.reads == [before]  # one fresh read, with no index fetch and no package fetch behind it
    assert ready.fetches == ["standup"]


def test_a_failed_fresh_read_refuses_and_there_is_no_cached_fallback(served, hub, ready):
    hub.fresh_error = "unreachable"  # the stored snapshot would allow it, but P0 has no cached fallback
    assert err(stage(served)) == "market.not_allowed"
    assert err(person(served, "market.add", {"url": "https://github.com/acme/m"})) == "market.not_allowed"
    assert err(person(served, "market.refresh")) == "market.not_allowed"
    assert ready.fetches == []
    assert len(ready.index_calls) == 1  # the one of add_market in the fixture, none after


def test_standalone_never_calls_the_hub(served, ready, monkeypatch):
    called = []
    monkeypatch.setattr(hubpolicy, "refresh_now", lambda *a: called.append(a))
    ok(stage(served))
    assert called == []


# ------------------------------------------------------------------------------------------------ the marketplace list
def test_a_marketplace_the_hub_does_not_list_is_refused_before_any_network(served, hub, ready):
    calls = len(ready.index_calls)
    hub.set(markets=[])  # the official one included
    assert err(person(served, "market.add", {"url": constants.OFFICIAL_URL})) == "market.not_allowed"
    assert err(person(served, "market.refresh", {"url": constants.OFFICIAL_URL})) == "market.not_allowed"
    assert err(stage(served)) == "market.not_allowed"
    assert len(ready.index_calls) == calls and ready.fetches == []


def test_refresh_of_all_marketplaces_reports_the_unlisted_one_in_its_row(served, hub, ready):
    hub.set(markets=[])
    res = ok(person(served, "market.refresh"))["data"]["markets"]
    assert res == [{"url": constants.OFFICIAL_URL, "error": "market.not_allowed"}]


def test_an_unlisted_community_marketplace_is_not_allowed_not_unisolated(served, hub):
    assert err(person(served, "market.add", {"url": ACME})) == "market.not_allowed"


# ------------------------------------------------------------------------------------------------ block, allow, ceiling
@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"block": ["standup"]}, "market.blocked"),
        ({"block": ["standup@1.0.0"]}, "market.blocked"),
        ({"allow": ["other"]}, "market.addon_not_allowed"),
        ({"allow": []}, "market.addon_not_allowed"),
        ({"ceiling": []}, "market.capability_ceiling"),
        ({"ceiling": ["pty"]}, "market.capability_ceiling"),
        ({"block": ["standup"], "ceiling": []}, "market.blocked"),  # order: block before ceiling
        ({"allow": ["other"], "ceiling": []}, "market.addon_not_allowed"),
    ],
)
def test_the_stage_refuses_by_block_allow_and_ceiling_before_any_fetch(served, hub, ready, kw, code):
    hub.set(**kw)
    assert err(stage(served)) == code
    assert ready.fetches == []


def test_another_version_in_the_block_list_and_an_allowed_id_pass(served, hub, ready):
    hub.set(block=["standup@0.9.0", "stand"], allow=["standup"], ceiling=["network"])
    ok(stage(served))


def test_the_grant_rechecks_block_allow_and_ceiling_under_the_lock(served, human, hub, ready):
    for kw, code in (
        ({"block": ["standup"]}, "market.blocked"),
        ({"allow": ["other"]}, "market.addon_not_allowed"),
        ({"ceiling": []}, "market.capability_ceiling"),
        ({"markets": []}, "market.not_allowed"),
    ):
        hub.set()
        st = ok(stage(served))["data"]
        hub.set(**kw)
        prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
        signed = human.sign(prepared)
        assert err(human.submit_res(prepared["request"]["id"], signed.events)) == code
        assert mfiles(served).installs() == {"addons": {}, "seen": {}, "keys": {}}
        assert not addon_dir(served).exists()
    hub.set()  # a refusal consumed its stage; a fresh review can now install
    st = ok(stage(served))["data"]
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
    assert addon_dir(served).exists()


# ------------------------------------------------------------------------------------------------ a company marketplace
@pytest.fixture
def company(served, net, monkeypatch):
    net.url = ACME
    net.add("standup", "1.0.0")
    h = Hub(monkeypatch, net)
    h.set(markets=[{"url": ACME, "class": "company", "keys": [H.KEY_B64]}, _official()])
    return h


def _official():
    return {"url": constants.OFFICIAL_URL, "class": "official", "keys": []}


def test_a_listed_company_marketplace_needs_no_isolation_and_its_pin_replaces_tofu(served, human, company, net):
    add = ok(person(served, "market.add", {"url": ACME}))["data"]
    assert add["class"] == "company" and add["verified"] is True
    st = ok(stage(served))["data"]
    assert st["market"]["class"] == "company" and st["market"]["publisher"]["tofu"] == "pinned"
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    assert prepared["material"]["market"]["publisher"]["tofu"] == "pinned"
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))
    rec = mfiles(served).installs()
    assert rec["addons"]["standup"]["origin"] == ACME and rec["addons"]["standup"]["publisher_key"] == H.KEY_B64
    assert rec["keys"] == {}  # no TOFU key is written for a pinned marketplace
    assert rec["seen"] == {f"{ACME}|standup": "1.0.0"}


def test_a_company_index_signed_by_a_key_the_hub_does_not_pin_is_refused(served, company):
    company.set(markets=[{"url": ACME, "class": "company", "keys": [OTHER_B64]}, _official()])
    assert err(person(served, "market.add", {"url": ACME})) == "market.index_unsigned"


def test_a_company_entry_signed_by_a_key_the_hub_does_not_pin_is_publisher_unpinned(served, company, net):
    ok(person(served, "market.add", {"url": ACME}))
    net.add("standup", "1.0.0", key=(OTHER_PRIV, OTHER_B64))
    net.bump()
    ok(person(served, "market.refresh"))
    assert err(stage(served)) == "market.publisher_unpinned"
    assert net.fetches == []
    net.add("standup", "1.0.0", signed=False)  # an unsigned entry is not pinned either
    net.bump()
    ok(person(served, "market.refresh"))
    assert err(stage(served)) == "market.publisher_unpinned"


def test_a_company_key_the_hub_stops_pinning_stops_the_grant(served, human, company, net):
    ok(person(served, "market.add", {"url": ACME}))
    st = ok(stage(served))["data"]
    company.set(markets=[{"url": ACME, "class": "company", "keys": [OTHER_B64]}, _official()])
    prepared = human.prepare("addon.grant", {"name": "standup", "staged": st["stage"]})
    signed = human.sign(prepared)
    assert err(human.submit_res(prepared["request"]["id"], signed.events)) in (
        "market.index_unsigned",
        "market.publisher_unpinned",
    )


def test_the_hub_removing_a_company_marketplace_makes_stage_and_add_not_allowed(served, company):
    ok(person(served, "market.add", {"url": ACME}))
    company.set()  # official only
    assert err(stage(served)) == "market.not_allowed"


# ------------------------------------------------------------------------------------------------ pause (run time)
def _builtin_digest(name):
    from orch.addons.builtin import manifest_bytes
    from orch.addons.package import MANIFEST_FILE, package_digest

    return package_digest({MANIFEST_FILE: manifest_bytes(name)})


def installed(served):
    return ok(person(served, "market.installed"))["data"]


def test_a_policy_pauses_an_installed_addon_and_undoing_it_unpauses(served, human, hub, ready):
    install(served, human)
    assert installed(served)["addons"][0]["paused"] is None
    for kw, code in (
        ({"block": ["standup"]}, "market.blocked"),
        ({"allow": ["other"]}, "market.addon_not_allowed"),
        ({"ceiling": []}, "market.capability_ceiling"),
        ({"markets": []}, "market.not_allowed"),
    ):
        hub.set(**kw)
        row = installed(served)["addons"][0]
        assert row["paused"] == {"code": code, "cause": "policy", "reconnect": None}, kw
        assert row["state"] == "enabled"  # the grant is untouched
    hub.set()
    assert installed(served)["addons"][0]["paused"] is None


def test_an_unverifiable_hub_pauses_every_addon_and_names_the_way_back(served, human, hub, ready):
    install(served, human)
    hub.pol = None  # the card expired, or the hub record is gone: no verifiable snapshot
    data = installed(served)
    assert data["hub"] == {"state": "unverified", "code": "market.not_allowed", "reconnect": hubpolicy.RECONNECT}
    assert data["addons"][0]["paused"] == {
        "code": "market.not_allowed", "cause": "unverified", "reconnect": hubpolicy.RECONNECT,
    }  # fmt: skip


def test_the_run_gate_applies_block_allow_ceiling_and_the_pin_to_every_addon(served, human, hub, ready):
    install(served, human)
    service = served.service
    gate = hubpolicy.run_gate(service)
    mine = SimpleNamespace(name="standup", version="1.0.0", capabilities=("network",), package_sha256="sha256:x")
    builtin = SimpleNamespace(
        name="dashboard", version="1.0.0", capabilities=("serve_http",), package_sha256=_builtin_digest("dashboard")
    )
    gate(mine)
    gate(builtin)
    hub.set(allow=["other"])
    with pytest.raises(MarketError) as e:
        gate(mine)
    assert e.value.code == "market.addon_not_allowed"
    gate(builtin)  # allow is about marketplace add-ons; a built-in has no origin
    hub.set(block=["dashboard"])
    with pytest.raises(MarketError) as e:
        gate(builtin)
    assert e.value.code == "market.blocked"
    hub.set(ceiling=[])
    for a in (mine, builtin):
        with pytest.raises(MarketError) as e:
            gate(a)
        assert e.value.code == "market.capability_ceiling"
    hub.pol = None
    with pytest.raises(MarketError) as e:
        gate(builtin)  # unverifiable: the dashboard too
    assert e.value.code == "market.not_allowed"


# ------------------------------------------------------------------------------------------------ the signed-log rule
def test_stored_never_reads_a_missing_record_on_a_joined_host_as_standalone(served, monkeypatch):
    from orch.host import hubops
    from orch.hubclient import snapshot

    service = served.service
    on = lambda fn: M.on_worker(served, fn)  # noqa: E731
    assert on(lambda: hubpolicy.stored(service)) is None  # never joined: standalone
    monkeypatch.setattr(snapshot, "hub_market_policy", lambda *a, **k: None)  # "not joined" per the local file
    monkeypatch.setattr(marketops, "hub_joined", lambda s: True)  # but the signed log says joined
    with pytest.raises(MarketError) as e:
        on(lambda: hubpolicy.stored(service))
    assert e.value.code == "market.not_allowed"
    monkeypatch.setattr(hubops, "_load_state", lambda s: None)  # hub.json deleted
    with pytest.raises(MarketError):
        on(lambda: hubpolicy.stored(service))


def test_stored_maps_every_refusal_to_not_allowed_and_returns_the_verified_policy(served, monkeypatch):
    from orch.hubclient import snapshot

    service = served.service
    pol = policy.from_raw(raw_policy())
    monkeypatch.setattr(snapshot, "hub_market_policy", lambda *a, **k: (7, pol))
    assert M.on_worker(served, lambda: hubpolicy.stored(service)) is pol

    def refuse(*a, **k):
        raise HubRefused("card_expired")

    monkeypatch.setattr(snapshot, "hub_market_policy", refuse)
    with pytest.raises(MarketError) as e:
        M.on_worker(served, lambda: hubpolicy.stored(service))
    assert e.value.code == "market.not_allowed"


def test_the_view_says_standalone_verified_or_unverified():
    pol = policy.from_raw(raw_policy())
    assert hubpolicy.view(None)["state"] == "standalone"
    assert hubpolicy.view(pol)["state"] == "verified"
    assert hubpolicy.view(hubpolicy.UNREADABLE) == {
        "state": "unverified", "code": "market.not_allowed", "reconnect": hubpolicy.RECONNECT,
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ sibling paths
def test_a_plain_regrant_of_a_blocked_or_over_ceiling_addon_is_refused(served, human, hub, ready):
    install(served, human)
    for kw, code in (({"block": ["standup"]}, "market.blocked"), ({"ceiling": []}, "market.capability_ceiling"),
                     ({"allow": ["other"]}, "market.addon_not_allowed")):  # fmt: skip
        hub.set(**kw)
        prepared = human.prepare("addon.grant", {"name": "standup"})
        signed = human.sign(prepared)
        assert err(human.submit_res(prepared["request"]["id"], signed.events)) == code, kw
    hub.set()


def test_host_features_see_a_paused_addon_as_off(served, human, hub, ready, monkeypatch):
    from orch.host import agents, terms

    install(served, human)
    service = served.service
    on = lambda name: M.on_worker(served, lambda: hubpolicy.running(service, name))  # noqa: E731
    assert on("standup") is True
    hub.set(block=["standup"])
    assert on("standup") is False
    from orch.addons import install as addon_install
    from orch.model.types import Addon

    builtins = {n: Addon(n, "0.1.0", _builtin_digest(n), [], {}, [], []) for n in (terms.ADDON, "start-agent")}
    monkeypatch.setattr(addon_install, "as_addons", lambda view: builtins)
    assert M.on_worker(served, lambda: terms.addon_granted(service)) is True
    assert M.on_worker(served, lambda: agents._addon(service, "start-agent")) is True  # noqa: SLF001
    hub.set(block=[terms.ADDON, "start-agent"])  # a blocked built-in is off for the host features that use it
    assert M.on_worker(served, lambda: terms.addon_granted(service)) is False
    assert M.on_worker(served, lambda: agents._addon(service, "start-agent")) is False  # noqa: SLF001
    hub.pol = None  # an unverifiable hub pauses them all
    assert M.on_worker(served, lambda: terms.addon_granted(service)) is False
    hub.set()  # undone: on again
    assert M.on_worker(served, lambda: terms.addon_granted(service)) is True


def test_routing_refuses_a_blocked_choose_model_addon_and_never_asks_it(served, monkeypatch):
    """The only host caller of ``runner.call``: a paused choose_model add-on is not run and not remembered."""
    from orch.addons import install as addon_install
    from orch.addons import runner
    from orch.host import routing
    from orch.model.types import Addon

    g = Addon("model-routing", "1.0.0", "sha256:" + "0" * 64, ["choose_model"], {}, [], [])
    monkeypatch.setattr(addon_install, "as_addons", lambda view: {"model-routing": g})
    asked = []
    monkeypatch.setattr(runner, "call", lambda *a, **k: asked.append(k) or {"model": None})
    pol = policy.from_raw(raw_policy(block=["model-routing"]))
    monkeypatch.setattr(hubpolicy, "stored", lambda service: pol)
    got = M.on_worker(served, lambda: routing._choose(served.service, "claude-code", "plan"))  # noqa: SLF001
    assert got.problem == routing.PAUSED and asked == []
