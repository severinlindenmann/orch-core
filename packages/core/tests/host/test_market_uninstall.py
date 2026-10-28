# ruff: noqa: F811
"""V3 G3 (#921): ``orch addon remove`` host side (``market.uninstall``), and the signer's strict shape for the
marketplace block of a review (C4)."""

from __future__ import annotations

import copy

import pytest

from orch.host import signer
from tests.host import human_helpers
from tests.host import market_helpers as M  # noqa: E402
from tests.host.market_helpers import (  # noqa: F401
    add_market,
    addon_dir,
    err,
    files,
    mfiles,
    net,
    ok,
    person,
    stage,
)  # fmt: skip
from tests.host.market_helpers import (
    install as do_install,
)
from tests.market import helpers as H

net = M.net  # the fake network (a fixture)
lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture
def v1(served, human, net):
    net.add("standup", "1.0.0")
    add_market(served, net)
    return do_install(served, human)[0]


def disable(human):
    prepared = human.prepare("addon.disable", {"name": "standup"})
    ok(human.submit_res(prepared["request"]["id"], human.sign(prepared).events))


def test_remove_needs_the_addon_to_be_off_first(served, human, net, v1):
    assert err(person(served, "market.uninstall", {"name": "standup"})) == "market.still_enabled"
    assert addon_dir(served).exists()
    disable(human)
    assert ok(person(served, "market.uninstall", {"name": "standup"}))["data"] == {"name": "standup", "removed": True}
    assert not addon_dir(served).exists()
    rec = mfiles(served).installs()
    assert rec["addons"] == {}  # the origin is dropped...
    assert rec["seen"] == {f"{H.OFFICIAL}|standup": "1.0.0"} and rec["keys"] == {f"{H.OFFICIAL}|standup": H.KEY_B64}


def test_a_removed_addon_cannot_come_back_older_and_keeps_its_publisher(served, human, net, v1):
    net.add("standup", "1.1.0")
    net.version = 2
    ok(person(served, "market.refresh"))
    st2 = ok(stage(served, update=True))["data"]
    p = human.prepare(
        "addon.grant",
        {
            "name": "standup",
            "staged": st2["stage"],
            "digest": st2["package_sha256"],
            "caps": st2["grant"]["capabilities"],
        },
    )
    ok(human.submit_res(p["request"]["id"], human.sign(p).events))
    disable(human)
    ok(person(served, "market.uninstall", {"name": "standup"}))
    net.add("standup", "1.0.0")  # the old, still validly signed entry is listed again
    net.version = 3
    ok(person(served, "market.refresh"))
    assert err(stage(served)) == "market.rollback"  # the highest version seen is kept after the removal
    net.add("standup", "1.1.0")
    net.version = 4
    ok(person(served, "market.refresh"))
    assert ok(stage(served))["data"]["market"]["publisher"]["tofu"] == "known"  # the key was kept


def test_not_installed_and_builtin_are_not_removable(served, human, net, v1):
    assert err(person(served, "market.uninstall", {"name": "wiki"})) == "market.not_installed"
    assert err(person(served, "market.uninstall", {"name": "nothing"})) == "market.not_installed"
    assert addon_dir(served).exists()


# ------------------------------------------------------------------------------------------------ C4: the signer
def prepared_for(served, human):
    st = ok(stage(served))["data"]
    return human.prepare(
        "addon.grant",
        {"name": "standup", "staged": st["stage"], "digest": st["package_sha256"], "caps": st["grant"]["capabilities"]},
    )


def refused(prepared, human):
    with pytest.raises(signer.SignerRefused):
        human.check(prepared)


def test_the_signer_checks_the_market_block_strictly(served, human, net):
    net.add("standup", "1.0.0")
    add_market(served, net)
    good = prepared_for(served, human)
    human.check(good)
    for mutate in (
        lambda m: m.update(extra="x"),  # an unknown key
        lambda m: m.pop("summary"),  # a missing key
        lambda m: m.update(capabilities=["network"]),  # capabilities are never carried here
        lambda m: m.update(stale="no"),
        lambda m: m.update(index_version="1"),
        lambda m: m.update(commit="main"),
        lambda m: m.update(source_note="from github.com/acme"),
        lambda m: m.update(url="u" * 201),
        lambda m: m.update(summary="s" * 301),
        lambda m: m["publisher"].update(tofu="trusted"),
        lambda m: m["publisher"].update(extra=1),
        lambda m: m.update(publisher=None),
        lambda m: m.update({"class": "other"}),
    ):
        bad = copy.deepcopy(good)
        mutate(bad["material"]["market"])
        refused(bad, human)
    other = copy.deepcopy(good)  # a market block on a request that signs something else
    other["events"][0]["type"] = "addon.disabled"
    refused(other, human)


def test_the_market_block_is_rendered_only_as_per_host_not_signed(served, human, net):
    net.add("standup", "1.0.0", summary="Daily standups")
    add_market(served, net)
    prepared = prepared_for(served, human)
    text = signer.render(prepared, lambda w: ("ws", "ORCH"))
    market_lines = [
        line for line in text.splitlines() if "marketplace" in line or "publisher" in line or "summary" in line
    ]
    assert market_lines and all("(per host, not signed)" in line for line in market_lines)
    assert any(line.startswith("summary from the marketplace (per host, not signed)") for line in text.splitlines())
